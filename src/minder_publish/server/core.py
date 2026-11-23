"""Publication domain orchestration with durable admission and immutable revisions."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from argon2 import PasswordHasher

from minder_publish.errors import PublishError

from . import publication_links
from .password_vault import MAX_PASSWORD_BYTES, PasswordVault
from .repository import Repository
from .storage import (
    CHUNK_BYTES,
    Storage,
    custom_key,
    iter_bytes,
    normalize_path,
    safe_id,
)

_HASHER = PasswordHasher()
_TERMINAL = ("committed", "aborted", "failed")
_KEY_LENGTH_FIELDS = (
    "publication_key_min_characters",
    "publication_key_max_characters",
)
_POLICY_KEYS = {
    "title",
    "summary",
    "password_hint",
    "access",
    "password",
    "expiry",
    "purge_mode",
    "purge_after_seconds",
    "metadata_policy",
    "metadata_after_purge_seconds",
    "site_routing",
    "external_resources",
    "preview",
    "indexing",
    "guest_counts",
    "owner_counts",
}
_PLAN_KEYS = _POLICY_KEYS | {
    "key",
    "kind",
    "filename",
    "size_bytes",
    "entry_path",
    "expanded_size_bytes",
    "publication_id",
    "expected_revision",
    "change_summary",
    "supports_link_names",
}


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise PublishError("invalid_datetime", 400)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Timezone is required")
        return result.astimezone(timezone.utc)
    except ValueError as exc:
        raise PublishError("invalid_datetime", 400, field="expiry") from exc


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def metadata_deadline(publication, timestamp):
    """Schedule retained metadata without inventing a historical purge time."""
    if publication["metadata_policy"] == "keep":
        return None
    if publication["metadata_policy"] == "with_content":
        return iso(timestamp)
    recorded = publication.get("purged_at") or publication.get("missing_on_import_at")
    anchor = parse(recorded) if recorded else timestamp
    return iso(anchor + timedelta(seconds=publication["metadata_after_purge_seconds"]))


def guest_available(publication):
    """Lifecycle eligibility shared by guest opening and credential-free previews."""
    return (
        publication["content_state"] == "present"
        and not publication.get("deletion_state")
        and not publication["paused"]
        and not (
            publication["expires_at"] and parse(publication["expires_at"]) <= now()
        )
    )


def subject_key(subject):
    return subject if isinstance(subject, str) else dumps(subject)


def _integer(value, field, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PublishError(
            "invalid_integer", 400, field=field, actual=value, limit=maximum
        )
    if maximum is not None and value > maximum:
        raise PublishError(
            "limit_exceeded", 413, field=field, actual=value, limit=maximum
        )
    return value


class Core:
    def __init__(self, config):
        self.config = config
        self.repository = Repository(config.db_path)
        self.password_vault = PasswordVault(config.data_dir)
        self.storage = Storage(config)
        self._thumbnail_threads = set()
        self._thumbnail_lock = threading.Lock()
        self._closed = False
        self._thumbnail_slots = threading.BoundedSemaphore(
            config.thumbnails["concurrency"]
        )

    def initialize(self):
        if (Path(self.config.data_dir) / ".restore-in-progress").exists():
            raise PublishError(
                "incomplete_restore",
                503,
                "Complete recovery into a fresh empty target before starting.",
            )
        self.storage.initialize()
        self.repository.initialize()
        with self.repository.connect(immediate=True) as conn:
            self.password_vault.initialize(conn)
            saved = conn.execute("SELECT data FROM core_settings WHERE id=1").fetchone()
            if not saved:
                data = {
                    "defaults": copy.deepcopy(self.config.defaults),
                    "limits": copy.deepcopy(self.config.limits),
                }
                self._validate_settings(data)
                conn.execute("INSERT INTO core_settings VALUES (1,?)", (dumps(data),))
            else:
                data = json.loads(saved["data"])
                if "password_hint_characters" not in data["limits"]:
                    data["limits"]["password_hint_characters"] = self.config.limits[
                        "password_hint_characters"
                    ]
                    conn.execute(
                        "UPDATE core_settings SET data=? WHERE id=1", (dumps(data),)
                    )
                self._validate_settings(data, allow_budget_reduction=True)
        return self

    def _settings(self, conn):
        row = conn.execute("SELECT data FROM core_settings WHERE id=1").fetchone()
        data = json.loads(row["data"])
        # New defaults apply without rewriting a restored instance on startup.
        for name in (
            *_KEY_LENGTH_FIELDS,
            *self.config.operations["publication_rate_bounds"],
            "history_previous_versions",
            "history_retention_days",
            "history_budget_percent",
            "owner_counts",
            "link_previews",
        ):
            data["defaults"].setdefault(name, self.config.defaults[name])
        return data

    def opening_budget(self, connection):
        defaults = self._settings(connection)["defaults"]
        return defaults["publication_open_attempts_per_minute"], 60

    def publication_password_budget(self, connection):
        defaults = self._settings(connection)["defaults"]
        return defaults["publication_password_attempts"], defaults["publication_password_window_minutes"] * 60

    def policy_settings(self):
        """Read effective policies without performing filesystem accounting."""
        with self.repository.connect() as conn:
            result = self._settings(conn)
        return self._settings_metadata(result)

    def _settings_metadata(self, result):
        result.update(
            ceilings=copy.deepcopy(self.config.ceilings),
            operations=copy.deepcopy(self.config.operations),
            thumbnails=copy.deepcopy(self.config.thumbnails),
        )
        result["ceilings"].update(self._history_ceilings())
        return result

    def settings(self):
        with self.repository.connect() as conn:
            result = self._settings(conn)
            storage = self._snapshot(conn, result["limits"])
            storage["publication_count"] = conn.execute("SELECT COUNT(*) FROM publication").fetchone()[0]
        result["storage"] = storage
        return self._settings_metadata(result)

    def _history_ceilings(self):
        return {
            "history_previous_versions": self.config.operations["history_max_previous_versions"],
            "history_retention_days": self.config.operations["history_max_retention_days"],
            "history_budget_percent": 100,
        }

    def _validate_settings(self, data, *, allow_budget_reduction=False):
        defaults, limits = data["defaults"], data["limits"]
        defaults.setdefault("owner_counts", self.config.defaults["owner_counts"])
        defaults.setdefault("link_previews", self.config.defaults["link_previews"])
        bounds = self.config.operations["publication_key_bounds"]
        for name, allowed in self.config.operations["publication_rate_bounds"].items():
            _integer(defaults.get(name, self.config.defaults[name]), name, allowed["minimum"], allowed["maximum"])
        key_lengths = [
            _integer(
                defaults.get(name, self.config.defaults[name]),
                name,
                bounds["minimum"],
                bounds["maximum"],
            )
            for name in _KEY_LENGTH_FIELDS
        ]
        if key_lengths[0] > key_lengths[1]:
            raise PublishError(
                "invalid_key_length_range",
                400,
                "The minimum link length must not exceed the maximum.",
                field="publication_key_min_characters",
            )
        for name, maximum in self._history_ceilings().items():
            _integer(defaults.get(name, self.config.defaults[name]), name, 0, maximum)
        for name, value in limits.items():
            _integer(
                value,
                name,
                minimum=0 if name == "disk_reserve_percent" else 1,
                maximum=self.config.ceilings.get(name),
            )
        if limits["disk_reserve_percent"] >= 100:
            raise PublishError("invalid_reserve", 400, field="disk_reserve_percent")
        for name, choices in {
            "access": ("private", "link", "password"),
            "purge_mode": ("immediate", "after", "never"),
            "metadata_policy": ("keep", "with_content", "after_purge"),
            "preview": ("auto", "download"),
            "site_routing": ("strict", "spa"),
            "external_resources": ("allow", "self_only"),
            "theme": ("system", "light", "dark"),
            "language": ("en", "ru", "es", "fr"),
        }.items():
            if defaults.get(name) not in choices:
                raise PublishError("invalid_setting", 400, field=name)
        # Password cannot be a create default without a separately supplied password.
        for field in ("owner_auto_open", "indexing", "guest_counts", "owner_counts", "link_previews"):
            if type(defaults.get(field)) is not bool:
                raise PublishError("invalid_boolean", 400, field=field)
        for field in ("private_ttl_seconds", "external_ttl_seconds"):
            if defaults.get(field) is not None:
                _integer(
                    defaults[field],
                    field,
                    1,
                    self.config.ceilings["retention_days"] * 86400,
                )
        for field in ("purge_after_seconds", "metadata_after_purge_seconds"):
            _integer(
                defaults[field],
                field,
                0,
                self.config.ceilings["retention_days"] * 86400,
            )
        reserve = self._reserve(limits)
        maximum = min(
            self.config.ceilings["content_quota_bytes"],
            self.config.storage_budget_bytes - reserve,
        )
        if not allow_budget_reduction and (
            maximum <= 0 or limits["content_quota_bytes"] > maximum
        ):
            raise PublishError(
                "storage_budget_invalid",
                400,
                field="content_quota_bytes",
                limit=max(0, maximum),
            )
        overhead = (
            self.config.operations["export_metadata_bytes"]
            + self.config.thumbnails["output_bytes"]
        )
        if (
            not allow_budget_reduction
            and max(
                limits["file_bytes"] * 2,
                limits["upload_payload_bytes"] + limits["site_expanded_bytes"],
            )
            + overhead
            > limits["content_quota_bytes"]
        ):
            raise PublishError(
                "operation_budget_invalid",
                400,
                field="content_quota_bytes",
                detail="The quota cannot fit one maximum operation and its staging allocation.",
            )
        if not isinstance(defaults.get("timezone"), str):
            raise PublishError("invalid_timezone", 400, field="timezone")
        if defaults["timezone"] != "browser":
            try:
                ZoneInfo(defaults["timezone"])
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise PublishError("invalid_timezone", 400, field="timezone") from exc
        normalize_path(defaults["site_entry"], limits)

    def update_settings(self, patch):
        if not isinstance(patch, dict) or set(patch) - {"defaults", "limits"}:
            raise PublishError("invalid_settings_patch", 400)
        with self.repository.connect(immediate=True) as conn:
            data = self._settings(conn)
            for section, values in patch.items():
                if (
                    section == "limits"
                    and isinstance(values, dict)
                    and set(values)
                    - {
                        "file_bytes",
                        "upload_payload_bytes",
                        "site_expanded_bytes",
                        "site_files",
                        "publication_count",
                        "content_quota_bytes",
                        "concurrent_uploads",
                    }
                ):
                    raise PublishError(
                        "operator_setting_required",
                        400,
                        "This operational guard is configured by the server operator.",
                        field="limits",
                    )
                if not isinstance(values, dict) or set(values) - set(data[section]):
                    raise PublishError("unknown_setting", 400, field=section)
                data[section].update(values)
            self._validate_settings(data)
            conn.execute("UPDATE core_settings SET data=? WHERE id=1", (dumps(data),))
        return self.settings()

    def _reserve(self, limits):
        return max(
            limits["disk_reserve_bytes"],
            math.ceil(
                self.config.storage_budget_bytes * limits["disk_reserve_percent"] / 100
            ),
        )

    def _snapshot(self, conn, limits):
        # One filesystem observation feeds both usage and its corresponding reservation remainder.
        # Separate scans could undercount quota if a receiver writes between those observations.
        revision_prefix = (
            Path(self.config.revisions_dir).relative_to(self.config.data_dir).as_posix()
            + "/"
        )
        staging_prefix = (
            Path(self.config.staging_dir).relative_to(self.config.data_dir).as_posix()
            + "/"
        )
        active = []
        for row in conn.execute(
            "SELECT * FROM upload WHERE state NOT IN ('committed','aborted','failed')"
        ):
            plan = json.loads(row["data"])
            active.append(
                {
                    "prefixes": (
                        staging_prefix + row["id"] + "/",
                        revision_prefix + plan["revision_id"] + "/",
                    ),
                    "reserved": row["reservation_bytes"],
                    "allocated": 0,
                }
            )
        historical_ids = {r["id"] for r in conn.execute("SELECT id FROM revision WHERE state != 'current'")}
        history_bytes = 0
        material_used = parser_bytes = total_used = 0
        for path, size in iter_bytes(Path(self.config.data_dir)):
            total_used += size
            if path.startswith(revision_prefix) and path[len(revision_prefix):].split("/", 1)[0] in historical_ids:
                history_bytes += size
            if path.startswith((revision_prefix, staging_prefix)):
                material_used += size
            if path.startswith("parser-temp/"):
                parser_bytes += size
            for allocation in active:
                if path.startswith(allocation["prefixes"]):
                    allocation["allocated"] += size
        external_reserved = conn.execute(
            "SELECT COALESCE(SUM(size_bytes),0) FROM external_reservation"
        ).fetchone()[0]
        parser_represented = min(parser_bytes, external_reserved)
        used = total_used - parser_represented
        remaining = external_reserved + sum(
            max(0, item["reserved"] - item["allocated"]) for item in active
        )
        # Export has no disk spool, but its metadata/buffer allocations are reserved explicitly.
        exports = conn.execute(
            "SELECT COUNT(*) FROM read_lease WHERE kind='export' AND deadline>?",
            (iso(now()),),
        ).fetchone()[0]
        export_reserved = exports * (
            self.config.operations["export_buffer_bytes"]
            + self.config.operations["export_metadata_bytes"]
        )
        remaining += export_reserved
        thumbnails = conn.execute(
            "SELECT COUNT(*) FROM read_lease WHERE kind='thumbnail' AND deadline>?",
            (iso(now()),),
        ).fetchone()[0]
        remaining += thumbnails * self.config.thumbnails["output_bytes"]
        operational_used = max(0, used - material_used)
        reserve = self._reserve(limits)
        material_maximum = max(
            0,
            min(
                self.config.ceilings["content_quota_bytes"],
                self.config.storage_budget_bytes - reserve,
            ),
        )
        effective_quota = min(limits["content_quota_bytes"], material_maximum)
        free = shutil.disk_usage(Path(self.config.data_dir)).free
        blocked = (
            material_used + remaining > effective_quota
            or used + remaining > self.config.storage_budget_bytes
        )
        return {
            "storage_budget_bytes": self.config.storage_budget_bytes,
            "configured_bytes": self.config.storage_budget_bytes,
            "used_bytes": material_used,
            "material_used_bytes": material_used,
            "history_bytes": history_bytes,
            "total_used_bytes": used,
            "operational_used_bytes": operational_used,
            "parser_reserved_bytes": parser_represented,
            "parser_unreserved_bytes": parser_bytes - parser_represented,
            "reserved_bytes": remaining,
            "operational_reserve_bytes": reserve,
            "effective_material_max_bytes": material_maximum,
            "maximum_bytes": material_maximum,
            "maximum_material_bytes": material_maximum,
            "quota_bytes": effective_quota,
            "free_bytes": free,
            "remaining_bytes": max(
                0,
                min(
                    effective_quota - material_used - remaining,
                    self.config.storage_budget_bytes - used - remaining,
                ),
            ),
            "write_blocked": blocked,
            "capacity_source": "operator_configured",
        }

    def storage_snapshot(self):
        with self.repository.connect() as conn:
            return self._snapshot(conn, self._settings(conn)["limits"])

    def _admit(self, conn, limits, amount):
        snapshot = self._snapshot(conn, limits)
        if snapshot["write_blocked"] or amount > snapshot["remaining_bytes"]:
            raise PublishError(
                "quota_exceeded",
                409,
                field="storage",
                actual=amount,
                limit=snapshot["remaining_bytes"],
                action="Free storage or ask the operator to increase the configured budget.",
            )
        if (
            snapshot["free_bytes"] - snapshot["reserved_bytes"] - amount
            < snapshot["operational_reserve_bytes"]
        ):
            raise PublishError(
                "disk_space_low",
                507,
                field="storage",
                action="Free physical disk space.",
            )

    def _publication(self, conn, publication_id):
        safe_id(publication_id)
        row = conn.execute(
            """SELECT publication.data, link.name AS link_name
               FROM publication LEFT JOIN publication_link link
               ON link.publication_id=publication.id AND link.is_primary=1
               WHERE publication.id=?""",
            (publication_id,),
        ).fetchone()
        if not row:
            raise PublishError("publication_not_found", 404)
        result = json.loads(row["data"])
        result["link_name"] = row["link_name"] or publication_id
        return result

    def _save_publication(self, conn, publication):
        conn.execute(
            "UPDATE publication SET data=? WHERE id=?",
            (dumps({key: value for key, value in publication.items() if key != "link_name"}), publication["id"]),
        )

    def _decorate(self, publication, manifest=False, *, conn=None):
        result = copy.deepcopy(publication)
        result["purge_reason"] = result.get("purge_reason") or (
            "unknown" if result["content_state"] != "present" else None
        )
        result.setdefault("missing_on_import_at", None)
        result["password_hint"] = result.get("password_hint") or ""
        result["has_password"] = bool(result.get("password_hash"))
        result["password_copy_available"] = bool(
            result["access"] == "password"
            and result.get("password_sealed")
            and not result.get("deletion_state")
        )
        result["expired"] = bool(
            result["expires_at"] and parse(result["expires_at"]) <= now()
        )
        result["status"] = (
            "purged"
            if result["content_state"] == "purged"
            else (
                "paused"
                if result["paused"]
                else ("expired" if result["expired"] else "active")
            )
        )
        result["library_status"] = (
            "deleted"
            if result.get("deletion_state")
            or result["content_state"] in ("purged", "purging", "purge_pending")
            else "expired"
            if result["expired"]
            else "paused"
            if result["paused"]
            else "active"
        )
        result["thumbnail_state"] = (
            "ready"
            if result.get("current_revision")
            and (
                self.storage.revision(result["current_revision"]) / "thumbnail.jpg"
            ).is_file()
            else "fallback"
        )
        if manifest:
            revision = None
            if result.get("current_revision"):
                revision = (
                    self._revision(conn, result["current_revision"])
                    if conn is not None
                    else self.get_revision(result["current_revision"])
                )
            result["files"] = revision["files"] if revision else []
            result["dependency_report"] = (
                revision.get("dependency_report") if revision else None
            )
        return result

    def get_publication(self, publication_id, manifest=False):
        with self.repository.connect() as conn:
            result = self._publication(conn, publication_id)
            return self._decorate(result, manifest, conn=conn)

    def resolve_link_name(self, name):
        with self.repository.connect() as conn:
            return publication_links.resolve(conn, name)

    def get_linked_publication(self, name):
        with self.repository.connect() as conn:
            address = publication_links.resolve_address(conn, name)
            publication = self._publication(conn, address["publication_id"])
            enabled = self._preview_mode_enabled(conn, address["preview_mode"])
        return {**self._decorate(publication), "link_preview_enabled": enabled}

    def _preview_mode_enabled(self, connection, mode):
        return mode == "on" or (
            mode == "inherit" and self._settings(connection)["defaults"]["link_previews"]
        )

    def link_preview_enabled(
        self, name, *, publication_id=None, sequence=None, connection=None
    ):
        """Check the named address policy in the caller's database snapshot."""
        if connection is None:
            with self.repository.connect() as conn:
                return self.link_preview_enabled(
                    name, publication_id=publication_id, sequence=sequence, connection=conn
                )
        if sequence is not None:
            from .history import shared_preview_mode

            if not publication_id or name not in (None, publication_id):
                return False
            mode = shared_preview_mode(self, connection, publication_id, sequence)
        else:
            try:
                address = publication_links.resolve_address(connection, name)
            except PublishError:
                return False
            if publication_id is not None and address["publication_id"] != publication_id:
                return False
            mode = address["preview_mode"]
        return self._preview_mode_enabled(connection, mode)

    def _link_snapshot(self, connection, publication):
        return publication_links.snapshot(
            connection, publication, self.config.base_url,
            self._settings(connection)["defaults"]["link_previews"],
        )

    def publication_link(self, publication_id):
        with self.repository.connect() as conn:
            return self._link_snapshot(conn, self._link_publication(conn, publication_id))

    def _link_publication(self, conn, publication_id):
        publication = self._publication(conn, publication_id)
        if publication.get("deletion_state"):
            raise PublishError("publication_not_found", 404)
        return publication

    def rename_publication_link(
        self, publication_id, name, keep_previous, expected_name, *,
        authorize, preview_mode=publication_links.UNCHANGED,
    ):
        if preview_mode is not publication_links.UNCHANGED:
            publication_links.validate_preview_mode(preview_mode)
        try:
            safe_id(expected_name)
        except PublishError as exc:
            raise PublishError("invalid_id", 400, field="expected_name") from exc
        if not isinstance(name, str) or len(name) > 128:
            raise PublishError("invalid_key", 400, field="name")
        if type(keep_previous) is not bool:
            raise PublishError("invalid_boolean", 400, field="keep_previous")
        with self.repository.connect(immediate=True) as conn:
            authorize(conn)
            publication = self._link_publication(conn, publication_id)
            if publication["link_name"] != expected_name:
                raise PublishError("publication_link_changed", 409, field="expected_name")
            if name == expected_name:
                if preview_mode is not publication_links.UNCHANGED:
                    conn.execute(
                        "UPDATE publication_link SET preview_mode=? WHERE name=?",
                        (preview_mode, name),
                    )
                return self._link_snapshot(conn, publication)
            reserved = conn.execute(
                "SELECT publication_id,preview_mode FROM publication_link WHERE name=?",
                (name,),
            ).fetchone()
            if reserved is None:
                try:
                    custom_key(name)
                except PublishError as exc:
                    raise PublishError(exc.code, exc.status, field="name") from exc
            if (
                reserved is not None and reserved["publication_id"] != publication_id
            ) or (
                reserved is None and publication_links.occupied(conn, name)
            ):
                raise PublishError("publication_exists", 409, field="name")
            if preview_mode is publication_links.UNCHANGED:
                preview_mode = (
                    reserved if reserved is not None else publication_links.resolve_address(conn, expected_name)
                )["preview_mode"]
            timestamp = iso(now())
            conn.execute(
                "UPDATE publication_link SET is_primary=0,retired_at=?,changed_at=? WHERE name=?",
                (None if keep_previous else timestamp, timestamp, expected_name),
            )
            if reserved:
                conn.execute(
                    "UPDATE publication_link SET is_primary=1,retired_at=NULL,changed_at=?,preview_mode=? WHERE name=?",
                    (timestamp, preview_mode, name),
                )
            else:
                publication_links.add_primary(conn, name, publication_id, timestamp, preview_mode)
            publication["link_name"] = name
            publication["metadata_version"] = publication.get("metadata_version", 1) + 1
            self._save_publication(conn, publication)
            return self._link_snapshot(conn, publication)

    def set_alias_preview(self, publication_id, name, preview_mode, *, authorize):
        safe_id(name)
        publication_links.validate_preview_mode(preview_mode)
        with self.repository.connect(immediate=True) as conn:
            authorize(conn)
            publication = self._link_publication(conn, publication_id)
            changed = conn.execute(
                """UPDATE publication_link SET preview_mode=?
                   WHERE name=? AND publication_id=? AND is_primary=0 AND retired_at IS NULL""",
                (preview_mode, name, publication_id),
            ).rowcount
            if not changed:
                raise PublishError("publication_link_not_found", 404)
            return self._link_snapshot(conn, publication)

    def disable_publication_alias(self, publication_id, name, *, authorize):
        safe_id(name)
        with self.repository.connect(immediate=True) as conn:
            authorize(conn)
            publication = self._link_publication(conn, publication_id)
            timestamp = iso(now())
            changed = conn.execute(
                """UPDATE publication_link SET retired_at=?,changed_at=?
                   WHERE name=? AND publication_id=? AND is_primary=0 AND retired_at IS NULL""",
                (timestamp, timestamp, name, publication_id),
            ).rowcount
            if not changed:
                raise PublishError("publication_link_not_found", 404)
            return self._link_snapshot(conn, publication)

    def list_publications(
        self, query="", kind=None, status=None, offset=0, limit=50, *, states=None
    ):
        _integer(offset, "offset", 0)
        _integer(limit, "limit", 1, 200)
        if query is None:
            query = ""
        if not isinstance(query, str) or len(query) > 2000:
            raise PublishError("invalid_query", 400, field="q")
        if states is not None:
            if not isinstance(states, str):
                raise PublishError("invalid_library_states", 400, field="states")
            states = set(states.split(",")) if states else set()
            if states - {"active", "paused", "expired", "deleted"}:
                raise PublishError("invalid_library_states", 400, field="states")
        needle = unicodedata.normalize("NFKC", query).casefold()
        with self.repository.connect() as conn:
            publications = [
                self._decorate({**json.loads(row["data"]), "link_name": row["link_name"] or row["id"]})
                for row in conn.execute(
                    """SELECT publication.id, publication.data, link.name AS link_name
                       FROM publication LEFT JOIN publication_link link
                       ON link.publication_id=publication.id AND link.is_primary=1"""
                )
            ]
        items = [
            p
            for p in publications
            if (not kind or p["kind"] == kind)
            and (not status or p["status"] == status)
            and (states is None or p["library_status"] in states)
            and (
                not needle
                or any(
                    needle in unicodedata.normalize("NFKC", p.get(k) or "").casefold()
                    for k in ("title", "summary", "filename")
                )
            )
        ]
        items.sort(key=lambda p: (p["updated_at"], p["id"]), reverse=True)
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "offset": offset,
            "limit": limit,
        }

    def _expiry(self, choice, access, defaults):
        if (
            not isinstance(choice, dict)
            or set(choice) - {"mode", "value"}
            or choice.get("mode") not in ("default", "never", "at")
        ):
            raise PublishError("invalid_expiry", 400, field="expiry")
        if choice["mode"] == "never":
            if "value" in choice:
                raise PublishError("invalid_expiry", 400, field="expiry")
            return None
        if choice["mode"] == "default":
            if "value" in choice:
                raise PublishError("invalid_expiry", 400, field="expiry")
            seconds = defaults[
                "private_ttl_seconds" if access == "private" else "external_ttl_seconds"
            ]
            return (
                iso(now() + timedelta(seconds=seconds)) if seconds is not None else None
            )
        value = parse(choice.get("value"))
        if value > now() + timedelta(days=self.config.ceilings["retention_days"]):
            raise PublishError("retention_limit", 400, field="expiry")
        return iso(value)

    def _apply_policy(self, publication, patch, defaults, limits, *, creating=False):
        if not isinstance(patch, dict) or set(patch) - _POLICY_KEYS:
            raise PublishError("unknown_publication_field", 400)
        result = copy.deepcopy(publication)
        for field in ("title", "summary", "password_hint"):
            if field in patch:
                value = patch[field]
                if not isinstance(value, str):
                    raise PublishError("invalid_text", 400, field=field)
                try:
                    value.encode("utf-8")
                except UnicodeError as exc:
                    raise PublishError("invalid_text", 400, field=field) from exc
                if len(value) > limits[field + "_characters"]:
                    raise PublishError(
                        "text_limit",
                        400,
                        field=field,
                        actual=len(value),
                        limit=limits[field + "_characters"],
                    )
                result[field] = value
        previous_access = result.get("access", "private")
        if "access" in patch:
            if patch["access"] not in ("private", "link", "password"):
                raise PublishError("invalid_access", 400, field="access")
            result["access"] = patch["access"]
        if (
            not creating
            and previous_access == "private"
            and result["access"] != "private"
            and "expiry" not in patch
        ):
            raise PublishError("external_expiry_required", 400, field="expiry")
        if "password" in patch:
            value = patch["password"]
            try:
                valid = (
                    isinstance(value, str)
                    and 1 <= len(value.encode("utf-8")) <= MAX_PASSWORD_BYTES
                )
            except UnicodeError:
                valid = False
            if not valid:
                raise PublishError("invalid_password", 400, field="password")
            result["password_hash"] = _HASHER.hash(value)
            result["password_sealed"] = self.password_vault.seal(result, value)
        if result["access"] == "password" and not result.get("password_hash"):
            raise PublishError("password_required", 400, field="password")
        if result["access"] != "password":
            result["password_hash"] = None
            result["password_sealed"] = None
            if previous_access == "password":
                result["password_hint"] = ""
        if "expiry" in patch or creating:
            result["expires_at"] = self._expiry(
                patch.get("expiry", {"mode": "default"}), result["access"], defaults
            )
        choices = {
            "purge_mode": ("immediate", "after", "never"),
            "metadata_policy": ("keep", "with_content", "after_purge"),
            "site_routing": ("strict", "spa"),
            "external_resources": ("allow", "self_only"),
            "preview": ("auto", "download"),
        }
        for field, values in choices.items():
            if field in patch:
                if patch[field] not in values:
                    raise PublishError("invalid_policy", 400, field=field)
                result[field] = patch[field]
        if (
            result["purge_mode"] == "never"
            and not self.config.operations["allow_never_purge"]
        ):
            raise PublishError("never_purge_disabled", 400, field="purge_mode")
        for field in ("purge_after_seconds", "metadata_after_purge_seconds"):
            if field in patch:
                result[field] = _integer(
                    patch[field],
                    field,
                    0,
                    self.config.ceilings["retention_days"] * 86400,
                )
        for field in ("indexing", "guest_counts", "owner_counts"):
            if field in patch:
                if type(patch[field]) is not bool:
                    raise PublishError("invalid_boolean", 400, field=field)
                result[field] = patch[field]
        if result["indexing"] and result["access"] != "link":
            if patch.get("indexing") is True:
                raise PublishError("indexing_requires_link", 400, field="indexing")
            result["indexing"] = False
        changed_access = any(
            result.get(k) != publication.get(k) for k in ("access", "password_hash")
        )
        if not creating and changed_access:
            result["access_version"] += 1
        if result.get("expires_at") and result["purge_mode"] != "never":
            result["purge_due_at"] = iso(
                parse(result["expires_at"])
                + timedelta(
                    seconds=result["purge_after_seconds"]
                    if result["purge_mode"] == "after"
                    else 0
                )
            )
        else:
            result["purge_due_at"] = None
        if result.get("content_state") == "purge_pending" and (
            not result["purge_due_at"] or parse(result["purge_due_at"]) > now()
        ):
            result["content_state"] = "present"
            result["purge_reason"] = None
        if result.get("content_state") == "purged" and {
            "metadata_policy", "metadata_after_purge_seconds"
        } & patch.keys():
            # Repeated identical requests must not extend an unknown purge's timer.
            preserve_unknown_deadline = (
                result["metadata_policy"] == publication.get("metadata_policy") == "after_purge"
                and result["metadata_after_purge_seconds"] == publication.get("metadata_after_purge_seconds")
                and not (result.get("purged_at") or result.get("missing_on_import_at"))
                and publication.get("metadata_due_at")
            )
            if not preserve_unknown_deadline:
                result["metadata_due_at"] = metadata_deadline(result, now())
        return result

    def reserve_external(self, size_bytes, subject, kind="multipart"):
        if kind != "multipart":
            raise PublishError("invalid_reservation_kind", 400, field="kind")
        with self.repository.connect(immediate=True) as conn:
            limits = self._settings(conn)["limits"]
            maximum = (
                limits["upload_payload_bytes"] + limits["multipart_overhead_bytes"]
            )
            _integer(size_bytes, "size_bytes", 0, maximum)
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM external_reservation WHERE kind='multipart'"
                ).fetchone()[0]
                >= limits["concurrent_uploads"]
            ):
                raise PublishError("uploads_busy", 429)
            self._admit(conn, limits, size_bytes)
            reservation_id = secrets.token_hex(16)
            deadline = iso(now() + timedelta(seconds=limits["upload_total_seconds"]))
            conn.execute(
                "INSERT INTO external_reservation VALUES (?,?,?,?,?)",
                (reservation_id, subject_key(subject), kind, size_bytes, deadline),
            )
        return reservation_id

    def release_external(self, reservation_id, subject):
        with self.repository.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT subject FROM external_reservation WHERE id=?",
                (safe_id(reservation_id),),
            ).fetchone()
            if row and row["subject"] != subject_key(subject):
                raise PublishError("reservation_not_found", 404)
            conn.execute(
                "DELETE FROM external_reservation WHERE id=?", (reservation_id,)
            )

    def create_upload(self, plan, subject, *, requested_id=None, on_created=None):
        if not isinstance(plan, dict) or set(plan) - _PLAN_KEYS:
            raise PublishError("unknown_upload_field", 400)
        supports_link_names = plan.get("supports_link_names", False)
        if type(supports_link_names) is not bool:
            raise PublishError("invalid_boolean", 400, field="supports_link_names")
        change_summary = plan.get("change_summary", "")
        if not isinstance(change_summary, str) or len(change_summary) > self.config.operations["change_summary_characters"]:
            raise PublishError("invalid_change_summary", 400, field="change_summary")
        if "key" in plan:
            if requested_id is not None:
                raise PublishError("invalid_requested_id", 400, field="key")
            requested_id = custom_key(plan["key"])
        if requested_id is not None:
            custom_key(requested_id)
            if "publication_id" in plan:
                raise PublishError("invalid_requested_id", 400)
        if plan.get("kind") not in ("file", "site"):
            raise PublishError("invalid_kind", 400, field="kind")
        with self.repository.connect(immediate=True) as conn:
            settings = self._settings(conn)
            limits, defaults = settings["limits"], settings["defaults"]
            maximum = (
                min(limits["upload_payload_bytes"], limits["file_bytes"])
                if plan["kind"] == "file"
                else limits["upload_payload_bytes"]
            )
            size = _integer(plan.get("size_bytes"), "size_bytes", 0, maximum)
            filename = normalize_path(plan.get("filename"), limits)
            if "/" in filename:
                raise PublishError("invalid_filename", 400, field="filename")
            publication_id = plan.get("publication_id")
            existing = (
                self._publication(conn, publication_id) if publication_id else None
            )
            if existing:
                self._check_upload_link(existing, supports_link_names)
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM upload WHERE state NOT IN ('committed','aborted','failed')"
                ).fetchone()[0]
                >= limits["concurrent_uploads"]
            ):
                raise PublishError("uploads_busy", 429)
            if existing:
                if (
                    "expected_revision" not in plan
                    or (
                        not isinstance(plan["expected_revision"], str)
                        and not (
                            plan["expected_revision"] is None
                            and existing["content_state"] == "purged"
                            and existing["current_revision"] is None
                        )
                    )
                    or plan["expected_revision"] != existing["current_revision"]
                ):
                    raise PublishError(
                        "revision_conflict", 409, field="expected_revision"
                    )
                if existing["content_state"] == "purging" or existing.get(
                    "deletion_state"
                ):
                    raise PublishError("purging", 409)
                publication = self._apply_policy(
                    existing,
                    {k: v for k, v in plan.items() if k in _POLICY_KEYS},
                    defaults,
                    limits,
                )
                if existing["content_state"] == "purged" and "expiry" not in plan:
                    raise PublishError("expiry_required", 400, field="expiry")
                retired = conn.execute(
                    "SELECT COUNT(*) FROM revision WHERE publication_id=? AND state='retired'",
                    (publication_id,),
                ).fetchone()[0]
                if retired >= self.config.operations["transient_retired_revision_cap"] + defaults["history_previous_versions"]:
                    raise PublishError("retired_revisions_busy", 409)
            else:
                if "expected_revision" in plan:
                    raise PublishError("invalid_expected_revision", 400)
                if (
                    conn.execute("SELECT COUNT(*) FROM publication").fetchone()[0]
                    >= limits["publication_count"]
                ):
                    raise PublishError("publication_count_limit", 409)
                publication_id = self._publication_key(conn, requested_id, defaults)
                timestamp = iso(now())
                publication = {
                    "id": publication_id,
                    "title": "",
                    "summary": "",
                    "kind": plan["kind"],
                    "filename": filename,
                    "entry_path": filename,
                    "content_type": "application/octet-stream",
                    "size_bytes": 0,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "expires_at": None,
                    "access": defaults["access"],
                    "password_hash": None,
                    "password_hint": "",
                    "paused": False,
                    "content_state": "present",
                    "current_revision": None,
                    "access_version": 1,
                    "metadata_version": 1,
                    "view_count": 0,
                    "last_viewed": None,
                    "purge_due_at": None,
                    "purged_at": None,
                    "purge_reason": None,
                    "missing_on_import_at": None,
                    "metadata_due_at": None,
                    "defaults_snapshot": copy.deepcopy(defaults),
                }
                for field in (
                    "purge_mode",
                    "purge_after_seconds",
                    "metadata_policy",
                    "metadata_after_purge_seconds",
                    "site_routing",
                    "external_resources",
                    "preview",
                    "indexing",
                    "guest_counts",
                    "owner_counts",
                ):
                    publication[field] = defaults[field]
                publication = self._apply_policy(
                    publication,
                    {k: v for k, v in plan.items() if k in _POLICY_KEYS},
                    defaults,
                    limits,
                    creating=True,
                )
            entry = (
                normalize_path(
                    plan.get(
                        "entry_path",
                        existing["entry_path"]
                        if existing and existing["kind"] == "site"
                        else defaults["site_entry"],
                    ),
                    limits,
                )
                if plan["kind"] == "site"
                else filename
            )
            expanded = (
                _integer(
                    plan.get("expanded_size_bytes", limits["site_expanded_bytes"]),
                    "expanded_size_bytes",
                    0,
                    limits["site_expanded_bytes"],
                )
                if plan["kind"] == "site"
                else size
            )
            # Single-file prepare copies bytes before transport removal; sites retain compressed+expanded.
            reservation = (
                size
                + expanded
                + self.config.operations["export_metadata_bytes"]
                + self.config.thumbnails["output_bytes"]
            )
            self._admit(conn, limits, reservation)
            upload_id = secrets.token_hex(16)
            hard = now() + timedelta(
                seconds=self.config.operations["reservation_max_seconds"]
            )
            deadline = min(
                hard, now() + timedelta(seconds=limits["upload_total_seconds"])
            )
            data = {
                "change_summary": change_summary,
                "revision_id": secrets.token_hex(16),
                "publication": publication,
                "kind": plan["kind"],
                "filename": filename,
                "entry_path": entry,
                "size_bytes": size,
                "expanded_size_bytes": expanded,
                "expected_revision": plan.get("expected_revision"),
                "supports_link_names": supports_link_names,
                "expected_link_name": existing["link_name"] if existing else None,
                "expected_access_version": existing["access_version"]
                if existing
                else None,
                "expected_metadata_version": existing.get("metadata_version", 1)
                if existing
                else None,
                "updating": bool(existing),
                "limits": copy.deepcopy(limits),
            }
            conn.execute(
                "INSERT INTO upload VALUES (?,?,?,?,?,?,?,0)",
                (
                    upload_id,
                    subject_key(subject),
                    dumps(data),
                    "planned",
                    iso(deadline),
                    iso(hard),
                    reservation,
                ),
            )
            if on_created is not None:
                on_created(conn, upload_id)
        return {
            "id": upload_id,
            "state": "planned",
            "max_bytes": size,
            "expires_at": iso(deadline),
            "received_bytes": 0,
        }

    def _publication_key(self, connection, requested, defaults):
        # The caller holds BEGIN IMMEDIATE through creation of the upload reservation.
        attempts = (
            1 if requested else self.config.operations["publication_key_max_attempts"]
        )
        for attempt in range(attempts):
            length = min(
                defaults["publication_key_min_characters"] + attempt,
                defaults["publication_key_max_characters"],
            )
            candidate = requested or "".join(
                secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789")
                for _ in range(length)
            )
            try:
                custom_key(candidate)
            except PublishError:
                if requested:
                    raise
                continue
            if not publication_links.occupied(connection, candidate):
                return candidate
            if requested:
                raise PublishError("publication_exists", 409)
        raise PublishError(
            "publication_key_exhausted",
            409,
            "No unused random name was found within the attempt budget. "
            "Try again, choose a custom name or increase the maximum length.",
            field="key",
            action="change_key",
        )

    def _upload(self, conn, upload_id, subject, states=None):
        safe_id(upload_id)
        row = conn.execute("SELECT * FROM upload WHERE id=?", (upload_id,)).fetchone()
        if not row or row["subject"] != subject_key(subject):
            raise PublishError("upload_not_found", 404)
        if states and row["state"] not in states:
            raise PublishError("upload_state_conflict", 409, actual=row["state"])
        if row["state"] not in _TERMINAL and (
            parse(row["deadline"]) <= now() or parse(row["hard_deadline"]) <= now()
        ):
            raise PublishError("upload_expired", 410)
        return row, json.loads(row["data"])

    @staticmethod
    def _check_upload_link(publication, supported, expected_name=None):
        if not supported and publication["link_name"] != publication["id"]:
            raise PublishError("publication_link_upgrade_required", 409, action="upgrade_client")
        if expected_name is not None and publication["link_name"] != expected_name:
            raise PublishError("publication_link_changed", 409)

    def _guard_upload_link(self, conn, data):
        if data["updating"]:
            publication = self._publication(conn, data["publication"]["id"])
            self._check_upload_link(
                publication,
                data.get("supports_link_names", False),
                data.get("expected_link_name", publication["id"]),
            )

    def upload_status(self, upload_id, subject):
        """Return a historical operation outcome without exposing its saved plan or bytes."""
        with self.repository.connect() as conn:
            row, data = self._upload(conn, upload_id, subject)
            result = {
                "id": upload_id,
                "state": row["state"],
                "expires_at": min(row["deadline"], row["hard_deadline"]),
            }
            if row["state"] == "committed":
                publication_id = data["publication"]["id"]
                primary = conn.execute(
                    "SELECT name FROM publication_link WHERE publication_id=? AND is_primary=1",
                    (publication_id,),
                ).fetchone()
                link_name = primary["name"] if primary else publication_id
                result.update(
                    publication_id=publication_id,
                    link_name=link_name,
                    stable_url=publication_links.stable_url(self.config.base_url, link_name),
                )
            return result

    async def receive_upload(self, upload_id, chunks, subject):
        data = None
        try:
            with self.repository.connect(immediate=True) as conn:
                row, data = self._upload(conn, upload_id, subject, ("planned",))
                self._guard_upload_link(conn, data)
                conn.execute("UPDATE upload SET state='receiving' WHERE id=?", (upload_id,))
        except PublishError:
            if data is not None:
                self._fail_upload(upload_id)
            raise
        stage = self.storage.stage(upload_id)
        received = 0
        iterator = chunks.__aiter__()
        try:
            stage.mkdir(parents=True, exist_ok=False, mode=0o700)
            with (stage / "payload").open("xb") as output:
                (stage / "payload").chmod(0o600)
                while True:
                    remaining = (
                        min(parse(row["deadline"]), parse(row["hard_deadline"])) - now()
                    ).total_seconds()
                    if remaining <= 0:
                        raise PublishError("upload_timeout", 408)
                    try:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=min(
                                data["limits"]["upload_idle_seconds"], remaining
                            ),
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError as exc:
                        raise PublishError("upload_timeout", 408) from exc
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise PublishError("invalid_upload_chunk", 400)
                    if len(chunk) > CHUNK_BYTES:
                        raise PublishError("upload_chunk_limit", 413, limit=CHUNK_BYTES)
                    received += len(chunk)
                    if received > data["size_bytes"]:
                        raise PublishError(
                            "upload_size_limit",
                            413,
                            actual=received,
                            limit=data["size_bytes"],
                        )
                    with self.repository.connect() as conn:
                        self._upload(conn, upload_id, subject, ("receiving",))
                    output.write(chunk)
                    with self.repository.connect(immediate=True) as conn:
                        self._upload(conn, upload_id, subject, ("receiving",))
                        conn.execute(
                            "UPDATE upload SET received_bytes=? WHERE id=?",
                            (received, upload_id),
                        )
                if received != data["size_bytes"]:
                    raise PublishError(
                        "upload_size_mismatch",
                        400,
                        actual=received,
                        limit=data["size_bytes"],
                    )
                output.flush()
                os.fsync(output.fileno())
            deadline = min(
                parse(row["hard_deadline"]),
                now()
                + timedelta(seconds=self.config.operations["prepare_queue_seconds"]),
            )
            with self.repository.connect(immediate=True) as conn:
                self._upload(conn, upload_id, subject, ("receiving",))
                conn.execute(
                    "UPDATE upload SET state='uploaded',deadline=? WHERE id=?",
                    (iso(deadline), upload_id),
                )
            return {"id": upload_id, "state": "uploaded", "received_bytes": received}
        except BaseException:
            self._fail_upload(upload_id)
            raise

    def _fail_upload(self, upload_id):
        with self.repository.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT state FROM upload WHERE id=?", (upload_id,)
            ).fetchone()
            if row and row["state"] != "committed":
                conn.execute(
                    "UPDATE upload SET state=?,reservation_bytes=0 WHERE id=?",
                    (
                        "aborted"
                        if row["state"] in ("aborting", "aborted")
                        else "failed",
                        upload_id,
                    ),
                )
        self.storage.remove_stage(upload_id)

    def abort_upload(self, upload_id, subject):
        with self.repository.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM upload WHERE id=?", (safe_id(upload_id),)
            ).fetchone()
            if not row or row["subject"] != subject_key(subject):
                raise PublishError("upload_not_found", 404)
            if row["state"] in ("receiving", "preparing", "committing", "aborting"):
                # Keep its reservation until the bounded phase owner observes cancellation and exits.
                conn.execute(
                    "UPDATE upload SET state='aborting' WHERE id=?", (upload_id,)
                )
                return {"id": upload_id, "state": "aborting"}
            if row["state"] == "committed":
                return {"id": upload_id, "state": "committed"}
            conn.execute(
                "UPDATE upload SET state='aborted',reservation_bytes=0 WHERE id=?",
                (upload_id,),
            )
        self.storage.remove_stage(upload_id)
        return {"id": upload_id, "state": "aborted"}

    def inspect_upload(self, upload_id, subject):
        with self.repository.connect(immediate=True) as conn:
            row, data = self._upload(conn, upload_id, subject, ("uploaded", "prepared"))
            if row["state"] == "prepared":
                return self._inspection_result(upload_id, row, data)
            limit = data["limits"]["concurrent_preparations"]
            count = conn.execute(
                "SELECT COUNT(*) FROM upload WHERE state IN ('preparing','committing')"
            ).fetchone()[0]
            if count >= limit:
                raise PublishError("preparations_busy", 429)
            deadline = min(
                parse(row["hard_deadline"]),
                now() + timedelta(seconds=data["limits"]["prepare_seconds"]),
            )
            conn.execute(
                "UPDATE upload SET state='preparing',deadline=? WHERE id=?",
                (iso(deadline), upload_id),
            )
        last_heartbeat = 0

        def heartbeat():
            nonlocal last_heartbeat
            if time.monotonic() - last_heartbeat < min(
                1, self.config.operations["phase_heartbeat_seconds"]
            ):
                return
            with self.repository.connect() as conn:
                self._upload(conn, upload_id, subject, ("preparing",))
            last_heartbeat = time.monotonic()

        try:
            manifest, size = self.storage.prepare(
                upload_id,
                data,
                data["limits"],
                time.monotonic() + max(0, (deadline - now()).total_seconds()),
                heartbeat,
            )
            from minder_publish.dependencies import scan_dependencies

            content = self.storage.stage(upload_id) / "prepared" / "content"
            remaining = (deadline - now()).total_seconds()
            if remaining <= 0:
                raise PublishError("prepare_timeout", 408)
            report = scan_dependencies(
                manifest,
                lambda path: (content / path).open("rb"),
                base_origin=self.config.content_origin(data["revision_id"]),
                entry_path=data["entry_path"],
                limits={"seconds": min(5, remaining)},
            )
            data["prepared_size"] = size
            data["dependency_report"] = report
            with self.repository.connect(immediate=True) as conn:
                current, _ = self._upload(conn, upload_id, subject, ("preparing",))
                review_deadline = min(
                    parse(current["hard_deadline"]),
                    now()
                    + timedelta(
                        seconds=self.config.operations["prepare_queue_seconds"]
                    ),
                )
                conn.execute(
                    "UPDATE upload SET state='prepared',data=?,deadline=? WHERE id=?",
                    (dumps(data), iso(review_deadline), upload_id),
                )
                row = conn.execute(
                    "SELECT * FROM upload WHERE id=?", (upload_id,)
                ).fetchone()
            return self._inspection_result(upload_id, row, data, manifest)
        except BaseException:
            self._fail_upload(upload_id)
            raise

    def _prepared_manifest(self, upload_id):
        path = self.storage.stage(upload_id) / "prepared" / "manifest.json"
        if path.stat().st_size > self.config.operations["export_metadata_bytes"]:
            raise PublishError("manifest_metadata_limit", 413)
        return json.loads(path.read_text(encoding="utf-8"))

    def _inspection_result(self, upload_id, row, data, manifest=None):
        return {
            "id": upload_id,
            "state": "prepared",
            "expires_at": row["deadline"],
            "files": manifest
            if manifest is not None
            else self._prepared_manifest(upload_id),
            "dependency_report": data["dependency_report"],
        }

    def commit_upload(self, upload_id, subject, *, authorize=None):
        promoted = False
        data = None
        try:
            with self.repository.connect() as conn:
                row, data = self._upload(
                    conn, upload_id, subject, ("uploaded", "prepared", "committed")
                )
                if row["state"] == "committed":
                    receipt = self._publication(conn, data["publication"]["id"])
                    if not data.get("supports_link_names", False):
                        receipt["link_name"] = receipt["id"]
                    return self._decorate(receipt, manifest=True, conn=conn)
                self._guard_upload_link(conn, data)
            if row["state"] == "uploaded":
                self.inspect_upload(upload_id, subject)
            with self.repository.connect() as conn:
                row, data = self._upload(conn, upload_id, subject, ("prepared",))
            manifest = self._prepared_manifest(upload_id)
            size = data["prepared_size"]
            timestamp = iso(now())
            with self.repository.connect(immediate=True) as conn:
                current_row, current_data = self._upload(
                    conn, upload_id, subject, ("prepared",)
                )
                if authorize is not None:
                    authorize(conn, upload_id)
                existing = (
                    self._publication(conn, data["publication"]["id"])
                    if data["updating"]
                    else None
                )
                if existing:
                    self._check_upload_link(
                        existing,
                        data.get("supports_link_names", False),
                        data.get("expected_link_name", existing["id"]),
                    )
                if (
                    existing
                    and existing["current_revision"] != data["expected_revision"]
                ):
                    raise PublishError(
                        "revision_conflict", 409, field="expected_revision"
                    )
                if existing and (
                    existing["content_state"] == "purging"
                    or existing.get("deletion_state")
                ):
                    raise PublishError("purging", 409)
                # Apply only supplied changes to current metadata, preserving concurrent metadata edits.
                publication = data["publication"]
                if existing:
                    # A metadata edit after upload planning must never be silently reverted.
                    if (
                        existing["updated_at"] != publication["updated_at"]
                        or existing["access_version"]
                        != data.get(
                            "expected_access_version", publication["access_version"]
                        )
                        or existing.get("metadata_version", 1)
                        != data.get(
                            "expected_metadata_version",
                            publication.get("metadata_version", 1),
                        )
                    ):
                        raise PublishError(
                            "publication_changed",
                            409,
                            action="Retry the update against current metadata.",
                        )
                if (
                    not existing
                    and conn.execute("SELECT COUNT(*) FROM publication").fetchone()[0]
                    >= self._settings(conn)["limits"]["publication_count"]
                ):
                    raise PublishError("publication_count_limit", 409)
                if (
                    not existing
                    and publication_links.occupied(conn, publication["id"], exclude_upload=upload_id)
                ):
                    raise PublishError("publication_exists", 409)
                # Verify physical envelope again; reservation already includes this prepared allocation.
                snapshot = self._snapshot(conn, self._settings(conn)["limits"])
                if (
                    snapshot["write_blocked"]
                    or snapshot["free_bytes"] < snapshot["operational_reserve_bytes"]
                ):
                    raise PublishError("storage_budget_exceeded", 507)
                conn.execute(
                    "UPDATE upload SET state='committing',deadline=? WHERE id=?",
                    (
                        iso(
                            min(
                                parse(row["hard_deadline"]),
                                now()
                                + timedelta(
                                    seconds=self.config.operations["commit_seconds"]
                                ),
                            )
                        ),
                        upload_id,
                    ),
                )
                self.storage.promote(upload_id, data["revision_id"])
                promoted = True
                if existing and existing["current_revision"]:
                    conn.execute(
                        "UPDATE revision SET state='retired',retired_at=?,drain_until=?,recovery_until=? WHERE id=?",
                        (
                            timestamp,
                            iso(
                                now()
                                + timedelta(
                                    seconds=self.config.operations[
                                        "revision_drain_seconds"
                                    ]
                                )
                            ),
                            iso(
                                now()
                                + timedelta(
                                    days=self._settings(conn)["defaults"]["history_retention_days"]
                                )
                            ),
                            existing["current_revision"],
                        ),
                    )
                publication.update(
                    kind=data["kind"],
                    filename=data["filename"],
                    entry_path=data["entry_path"],
                    size_bytes=size,
                    current_revision=data["revision_id"],
                    updated_at=timestamp,
                    content_type=next(
                        f["content_type"]
                        for f in manifest
                        if f["path"] == data["entry_path"]
                    ),
                    content_state="present",
                    purged_at=None,
                    purge_reason=None,
                    missing_on_import_at=None,
                    metadata_due_at=None,
                )
                if existing:
                    publication["metadata_version"] = (
                        existing.get("metadata_version", 1) + 1
                    )
                    publication["access_version"] = max(
                        publication["access_version"], existing["access_version"]
                    )
                    publication["view_count"] = existing["view_count"]
                    publication["last_viewed"] = existing.get("last_viewed")
                if existing and existing["content_state"] == "purged":
                    publication["access_version"] += 1
                revision = {
                    "id": data["revision_id"],
                    "publication_id": publication["id"],
                    "size_bytes": size,
                    "created_at": timestamp,
                    "entry_path": data["entry_path"],
                    "kind": data["kind"],
                    "filename": data["filename"],
                    "dependency_report": data["dependency_report"],
                }
                conn.execute(
                    "INSERT INTO revision VALUES (?,?,?,?,NULL,NULL,NULL)",
                    (revision["id"], publication["id"], dumps(revision), "current"),
                )
                conn.executemany(
                    "INSERT INTO revision_file VALUES (?,?,?)",
                    [(revision["id"], f["path"], dumps(f)) for f in manifest],
                )
                if existing:
                    self._save_publication(conn, publication)
                else:
                    conn.execute(
                        "INSERT INTO publication VALUES (?,?)",
                        (publication["id"], dumps(publication)),
                    )
                    publication_links.add_initial(conn, publication)
                from .history import apply_retention, record_revision

                record_revision(self, conn, publication, revision, data.get("change_summary", ""))
                apply_retention(self, conn, now())
                conn.execute(
                    "UPDATE upload SET state='committed',reservation_bytes=0 WHERE id=?",
                    (upload_id,),
                )
                receipt = self._decorate(
                    self._publication(conn, publication["id"]), manifest=True, conn=conn
                )
            self.storage.remove_stage(upload_id)
            self._thumbnail(publication, manifest)
            return receipt
        except BaseException:
            if promoted:
                # Never remove a revision that the transaction might have committed.
                with self.repository.connect() as conn:
                    referenced = conn.execute(
                        "SELECT 1 FROM revision WHERE id=?", (data["revision_id"],)
                    ).fetchone()
                if not referenced:
                    shutil.rmtree(
                        self.storage.revision(data["revision_id"]), ignore_errors=True
                    )
            if data is not None and row["state"] != "committed":
                self._fail_upload(upload_id)
            raise

    def close(self):
        """Finish bounded preview work before releasing the runtime/offline-operation lock."""
        self._closed = True
        with self._thumbnail_lock:
            threads = list(self._thumbnail_threads)
        for thread in threads:
            thread.join(timeout=self.config.thumbnails["decoder_seconds"] + 3)
        if any(thread.is_alive() for thread in threads):
            raise PublishError("thumbnail_shutdown_timeout", 503)

    def _thumbnail(self, publication, manifest):
        from .thumbnails import generate_thumbnail, thumbnail_source

        if self._closed:
            return
        source_path = thumbnail_source(publication["kind"], manifest)
        if source_path is None or not self._thumbnail_slots.acquire(blocking=False):
            return
        revision_id = publication["current_revision"]
        try:
            lease = self.acquire_lease(revision_id, kind="thumbnail")
        except Exception:
            self._thumbnail_slots.release()
            return

        def run():
            try:
                source, _ = self.resolve_file(revision_id, source_path)
                generate_thumbnail(
                    source,
                    self.storage.revision(revision_id) / "thumbnail.jpg",
                    self.config.thumbnails,
                )
            except Exception:
                pass
            finally:
                try:
                    self.release_lease(lease)
                finally:
                    self._thumbnail_slots.release()
                    with self._thumbnail_lock:
                        self._thumbnail_threads.discard(threading.current_thread())

        thread = threading.Thread(target=run, name="publish-thumbnail", daemon=True)
        with self._thumbnail_lock:
            if not self._closed:
                self._thumbnail_threads.add(thread)
                try:
                    thread.start()
                except RuntimeError:
                    self._thumbnail_threads.discard(thread)
                else:
                    return
        try:
            self.release_lease(lease)
        finally:
            self._thumbnail_slots.release()

    def record_view(self, publication_id):
        with self.repository.connect(immediate=True) as conn:
            publication = self._publication(conn, publication_id)
            if publication["guest_counts"] and not publication.get("deletion_state"):
                publication["view_count"] += 1
                publication["last_viewed"] = iso(now())
                self._save_publication(conn, publication)
        return {
            "id": publication_id,
            "view_count": publication["view_count"],
            "last_viewed": publication.get("last_viewed"),
        }

    def copy_publication_password(self, publication_id, authorize):
        with self.repository.connect(immediate=True) as conn:
            authorize(conn)
            publication = self._publication(conn, publication_id)
            if (
                publication.get("deletion_state")
                or conn.execute(
                    "SELECT 1 FROM deletion_tombstone WHERE publication_id=?",
                    (publication_id,),
                ).fetchone()
            ):
                raise PublishError("publication_deleted", 409)
            if publication["access"] != "password":
                raise PublishError("password_copy_unavailable", 409)
            return {"password": self.password_vault.open(publication)}

    def patch_publication(self, publication_id, patch):
        with self.repository.connect(immediate=True) as conn:
            publication = self._publication(conn, publication_id)
            if publication["content_state"] == "purging" or publication.get(
                "deletion_state"
            ):
                raise PublishError("purging", 409)
            settings = self._settings(conn)
            result = self._apply_policy(
                publication, patch, settings["defaults"], settings["limits"]
            )
            result["updated_at"] = iso(now())
            result["metadata_version"] = result.get("metadata_version", 1) + 1
            self._save_publication(conn, result)
        return self._decorate(result)

    def pause(self, publication_id, paused):
        if type(paused) is not bool:
            raise PublishError("invalid_boolean", 400, field="paused")
        with self.repository.connect(immediate=True) as conn:
            result = self._publication(conn, publication_id)
            if result.get("deletion_state"):
                raise PublishError("publication_deleted", 409)
            if result["paused"] != paused:
                result["paused"] = paused
                result["access_version"] += 1
                result["updated_at"] = iso(now())
                result["metadata_version"] = result.get("metadata_version", 1) + 1
                self._save_publication(conn, result)
        return self._decorate(result)

    def expire(self, publication_id):
        with self.repository.connect(immediate=True) as conn:
            previous = self._publication(conn, publication_id)
            if previous["content_state"] == "purging" or previous.get("deletion_state"):
                raise PublishError("purging", 409)
            settings = self._settings(conn)
            result = self._apply_policy(
                previous,
                {"expiry": {"mode": "at", "value": iso(now())}},
                settings["defaults"],
                settings["limits"],
            )
            result["access_version"] += 1
            result["metadata_version"] = previous.get("metadata_version", 1) + 1
            result["updated_at"] = iso(now())
            self._save_publication(conn, result)
        return self._decorate(result)

    def extend(self, publication_id, seconds):
        _integer(seconds, "seconds", 1, self.config.ceilings["retention_days"] * 86400)
        with self.repository.connect(immediate=True) as conn:
            result = self._publication(conn, publication_id)
            if result["content_state"] in ("purging", "purged") or result.get(
                "deletion_state"
            ):
                raise PublishError("content_unavailable", 409)
            if result["expires_at"] is None:
                raise PublishError(
                    "no_expiry",
                    409,
                    "This publication never expires. Set an explicit expiry instead.",
                    field="expiry",
                )
            base = max(now(), parse(result["expires_at"]))
            settings = self._settings(conn)
            result = self._apply_policy(
                result,
                {
                    "expiry": {
                        "mode": "at",
                        "value": iso(base + timedelta(seconds=seconds)),
                    }
                },
                settings["defaults"],
                settings["limits"],
            )
            result["updated_at"] = iso(now())
            result["metadata_version"] = result.get("metadata_version", 1) + 1
            self._save_publication(conn, result)
        return self._decorate(result)

    def delete_publication(self, publication_id):
        with self.repository.connect(immediate=True) as conn:
            try:
                result = self._publication(conn, publication_id)
            except PublishError as error:
                if (
                    error.status == 404
                    and conn.execute(
                        "SELECT 1 FROM deletion_tombstone WHERE publication_id=?",
                        (publication_id,),
                    ).fetchone()
                ):
                    return {"id": publication_id, "deletion_state": "complete"}
                raise
            result["paused"] = True
            result["access_version"] += 1
            result["deletion_state"] = "pending"
            result["content_state"] = "purge_pending"
            result["purge_reason"] = "owner_delete"
            result["purge_due_at"] = iso(now())
            result["updated_at"] = iso(now())
            result["metadata_version"] = result.get("metadata_version", 1) + 1
            self._save_publication(conn, result)
            publication_links.record_deletion(conn, publication_id, iso(now()))
        return {"id": publication_id, "deletion_state": "pending"}

    def history(self, publication_id):
        from .history import list_history

        return list_history(self, publication_id)

    def history_revision(self, publication_id, revision_id):
        from .history import retained_revision

        return retained_revision(self, publication_id, revision_id)

    def share_revision(self, publication_id, revision_id, enabled, *, authorize):
        from .history import set_sharing

        return set_sharing(self, publication_id, revision_id, enabled, authorize=authorize)

    def set_revision_preview(self, publication_id, revision_id, preview_mode, *, authorize):
        from .history import set_sharing

        return set_sharing(
            self, publication_id, revision_id, authorize=authorize, preview_mode=preview_mode
        )

    def restore_revision(self, publication_id, revision_id, expected_revision, subject, *, authorize=None):
        from .history import restore_revision

        return restore_revision(self, publication_id, revision_id, expected_revision, subject, authorize=authorize)

    def _revision(self, conn, revision_id, *, manifest=True):
        safe_id(revision_id)
        row = conn.execute(
            "SELECT * FROM revision WHERE id=?", (revision_id,)
        ).fetchone()
        if not row:
            raise PublishError("revision_not_found", 404)
        result = json.loads(row["data"])
        result.update(
            {
                key: row[key]
                for key in ("state", "retired_at", "drain_until", "recovery_until")
            }
        )
        if manifest:
            result["files"] = [
                json.loads(f["data"])
                for f in conn.execute(
                    "SELECT data FROM revision_file WHERE revision_id=? ORDER BY path",
                    (revision_id,),
                )
            ]
        result["path"] = self.storage.revision(revision_id)
        return result

    def get_revision(self, revision_id, *, manifest=True):
        with self.repository.connect() as conn:
            return self._revision(conn, revision_id, manifest=manifest)

    def resolve_file(self, revision_id, path):
        if not isinstance(path, str):
            raise PublishError("unsafe_path", 400)
        with self.repository.connect() as conn:
            revision = self._revision(conn, revision_id, manifest=False)
            self._publication(conn, revision["publication_id"])  # Reject a dangling revision.
            normalized = normalize_path(
                path or revision["entry_path"],
                {"path_utf8_bytes": 1 << 20, "path_depth": 4096},
            )
            row = conn.execute(
                "SELECT data FROM revision_file WHERE revision_id=? AND path=?",
                (revision_id, normalized),
            ).fetchone()
            if not row:
                raise PublishError("file_not_found", 404)
            info = json.loads(row["data"])
        target = self.storage.revision(revision_id) / "content" / normalized
        if not target.is_file() or target.is_symlink():
            raise PublishError("file_not_found", 404)
        return target, info

    def acquire_lease(self, revision_id, kind="read"):
        if kind not in ("read", "export", "backup", "thumbnail"):
            raise PublishError("invalid_lease_kind", 400)
        timestamp = now()
        with self.repository.connect(immediate=True) as conn:
            revision = conn.execute(
                "SELECT * FROM revision WHERE id=?", (safe_id(revision_id),)
            ).fetchone()
            if not revision or revision["state"] == "collecting":
                raise PublishError("revision_not_found", 404)
            publication = self._publication(conn, revision["publication_id"])
            if publication["content_state"] not in (
                "present",
                "purge_pending",
            ) or publication.get("deletion_state"):
                raise PublishError("content_unavailable", 410)
            settings = self._settings(conn)
            limits = settings["limits"]
            if kind == "export":
                count = conn.execute(
                    "SELECT COUNT(*) FROM read_lease WHERE kind='export' AND deadline>?",
                    (iso(timestamp),),
                ).fetchone()[0]
                if count >= limits["concurrent_exports"]:
                    raise PublishError("exports_busy", 429)
                self._admit(
                    conn,
                    limits,
                    self.config.operations["export_buffer_bytes"]
                    + self.config.operations["export_metadata_bytes"],
                )
            if kind == "thumbnail":
                if (
                    conn.execute(
                        "SELECT COUNT(*) FROM read_lease WHERE kind='thumbnail' AND deadline>?",
                        (iso(timestamp),),
                    ).fetchone()[0]
                    >= self.config.thumbnails["concurrency"]
                ):
                    raise PublishError("thumbnails_busy", 429)
                self._admit(conn, limits, self.config.thumbnails["output_bytes"])
            hard = timestamp + timedelta(
                seconds=limits["export_total_seconds"]
                if kind in ("read", "export", "backup")
                else self.config.thumbnails["decoder_seconds"] + 5
            )
            deadline = min(
                hard, timestamp + timedelta(seconds=limits["export_idle_seconds"])
            )
            lease_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO read_lease VALUES (?,?,?,?,?)",
                (lease_id, revision_id, kind, iso(deadline), iso(hard)),
            )
        return lease_id

    def renew_lease(self, lease_id):
        with self.repository.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM read_lease WHERE id=?", (safe_id(lease_id),)
            ).fetchone()
            if (
                not row
                or parse(row["deadline"]) <= now()
                or parse(row["hard_deadline"]) <= now()
            ):
                raise PublishError("lease_expired", 410)
            limits = self._settings(conn)["limits"]
            deadline = min(
                parse(row["hard_deadline"]),
                now() + timedelta(seconds=limits["export_idle_seconds"]),
            )
            conn.execute(
                "UPDATE read_lease SET deadline=? WHERE id=?", (iso(deadline), lease_id)
            )
        return {"id": lease_id, "expires_at": iso(deadline)}

    def release_lease(self, lease_id):
        with self.repository.connect(immediate=True) as conn:
            conn.execute("DELETE FROM read_lease WHERE id=?", (safe_id(lease_id),))

    def sweep(self):
        from .lifecycle import sweep

        return sweep(self)
