"""Operator configuration and the packaged default policy.

Publication defaults are persisted by the domain core. Operator envelope and secret
configuration are never writable through the publication API.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from minder_publish.errors import PublishError


def packaged_defaults() -> dict:
    return json.loads(
        files("minder_publish").joinpath("defaults.json").read_text(encoding="utf-8")
    )


def policy_section(name: str) -> dict:
    return copy.deepcopy(packaged_defaults()[name])


@dataclass
class Config:
    data_dir: Path
    base_url: str = "http://publish.localhost:8080"
    content_domain: str = ""
    secret: str = ""
    storage_budget_bytes: int = 20 * 1024**3
    legacy_hosts: tuple[str, ...] = ()
    remote_mcp_enabled: bool = False
    mcp_file_hosts: tuple[str, ...] = ()
    client_wheel: Path | None = None
    source_url: str = ""
    testing: bool = False
    defaults: dict = field(default_factory=lambda: policy_section("defaults"))
    limits: dict = field(default_factory=lambda: policy_section("limits"))
    ceilings: dict = field(default_factory=lambda: policy_section("ceilings"))
    operations: dict = field(default_factory=lambda: policy_section("operations"))
    thumbnails: dict = field(default_factory=lambda: policy_section("thumbnails"))
    project_url: str = ""

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        if self.client_wheel is not None:
            self.client_wheel = Path(self.client_wheel).expanduser().resolve()
        self._validate_public_link(self.source_url, "source", "repository")
        self._validate_public_link(self.project_url, "project", "project site")
        try:
            parsed = urlsplit(self.base_url.rstrip("/"))
            hostname = parsed.hostname
            port = parsed.port
            if (
                parsed.scheme not in ("http", "https")
                or not hostname
                or len(hostname) > 253
                or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in hostname.split(".")
                )
                or not self.base_url.isascii()
                or any(
                    ord(character) < 33 or character in "?#\\\x7f"
                    for character in self.base_url
                )
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.netloc.endswith(":")
                or port == 0
            ):
                raise ValueError
        except ValueError as error:
            raise PublishError(
                "invalid_base_url",
                detail=(
                    "Set an absolute HTTP(S) origin with an ASCII DNS hostname, "
                    "without credentials or a path."
                ),
                field="base_url",
            ) from error
        if (
            parsed.scheme == "http"
            and not self.testing
            and not self._local_host(hostname)
        ):
            raise PublishError(
                "https_required",
                detail="A public instance requires HTTPS.",
                field="base_url",
            )
        self.content_domain = (
            (self.content_domain or hostname).lower().rstrip(".")
        )
        if any(character in self.content_domain for character in "/:@?*# "):
            raise PublishError("invalid_content_domain", field="content_domain")
        if self.content_domain != hostname:
            raise PublishError(
                "content_domain_must_match",
                detail="Use the control hostname as the content DNS suffix. Revision subdomains keep previews on the same site.",
                field="content_domain",
            )
        if not self.testing:
            try:
                ipaddress.ip_address(hostname)
                bare_local = True
            except ValueError:
                bare_local = hostname == "localhost"
            if bare_local:
                raise PublishError(
                    "browser_hostname_required",
                    detail="Use publish.localhost for local browser use, or an HTTPS DNS hostname for a public instance.",
                    field="base_url",
                )
        default_port = 443 if parsed.scheme == "https" else 80
        authority = f"{hostname}:{port}" if port not in (None, default_port) else hostname
        self.base_url = f"{parsed.scheme}://{authority}"
        if (
            not isinstance(self.storage_budget_bytes, int)
            or self.storage_budget_bytes <= self.operational_reserve_bytes
        ):
            raise PublishError(
                "invalid_storage_budget",
                detail="The configured budget must exceed the operational reserve.",
                field="storage_budget_bytes",
            )
        if self.secret and len(self.secret.encode("utf-8")) < 32:
            raise PublishError(
                "weak_secret",
                detail="Use at least 32 random bytes for the server secret.",
                field="secret",
            )

    @staticmethod
    def _validate_public_link(url: str, setting: str, purpose: str) -> None:
        if not url:
            return
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if (
                parsed.scheme != "https"
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or "\\" in url
                or any(character.isspace() or ord(character) < 32 for character in url)
            ):
                raise ValueError
            if setting == "project" and (
                not url.startswith("https://")
                or "?" in url
                or "#" in url
                or "\x7f" in url
                or parsed.port == 0
                or parsed.netloc.endswith(":")
                or len(host) > 253
                or not re.fullmatch(
                    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
                    host,
                )
                or host.endswith(".localhost")
            ):
                raise ValueError
        except ValueError as error:
            raise PublishError(
                f"invalid_{setting}_url",
                detail=f"Use a public HTTPS {purpose} URL without credentials, query or fragment.",
                field=f"PUBLISH_{setting.upper()}_URL",
            ) from error

    @staticmethod
    def _local_host(host: str) -> bool:
        if host == "localhost" or host.endswith(".localhost"):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "publish.sqlite3"

    @property
    def runtime_lock_path(self) -> Path:
        return self.data_dir / ".runtime.lock"

    @property
    def revisions_dir(self) -> Path:
        return self.data_dir / "revisions"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def operational_reserve_bytes(self) -> int:
        return max(
            int(self.limits["disk_reserve_bytes"]),
            (self.storage_budget_bytes * int(self.limits["disk_reserve_percent"]) + 99)
            // 100,
        )

    @property
    def maximum_material_bytes(self) -> int:
        return min(
            int(self.ceilings["content_quota_bytes"]),
            self.storage_budget_bytes - self.operational_reserve_bytes,
        )

    @property
    def control_host(self) -> str:
        return str(urlsplit(self.base_url).hostname).lower()

    @property
    def control_origin(self) -> str:
        return self.base_url

    @property
    def cookie_secure(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def owner_cookie(self) -> str:
        return "__Host-mp-owner" if self.cookie_secure else "mp-dev-owner"

    @property
    def viewer_cookie(self) -> str:
        return "__Host-mp-viewer" if self.cookie_secure else "mp-dev-viewer"

    @property
    def probe_viewer_cookie(self) -> str:
        return "__Host-mp-probe-viewer" if self.cookie_secure else "mp-dev-probe-viewer"

    @property
    def reader_cookie(self) -> str:
        return "__Host-mp-read" if self.cookie_secure else "mp-dev-read"

    @property
    def bridge_cookie(self) -> str:
        return "__Host-mp-bridge" if self.cookie_secure else "mp-dev-bridge"

    def content_origin(self, revision_id: str, guest: bool = False) -> str:
        parsed = urlsplit(self.base_url)
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{'g' if guest else 'r'}-{revision_id}.{self.content_domain}{port}"

    def require_secret(self) -> None:
        if not self.secret:
            raise PublishError(
                "missing_secret",
                detail="Initialize the instance before starting the server.",
                field="PUBLISH_SECRET",
            )
        secret_file = self.data_dir / "server.secret"
        if (
            secret_file.is_file()
            and secret_file.read_text(encoding="utf-8").strip() != self.secret
        ):
            raise PublishError(
                "secret_override_mismatch",
                503,
                "Update or remove PUBLISH_SECRET after restoring or rotating the instance secret.",
            )

    @classmethod
    def from_env(cls) -> "Config":
        if os.getenv("PUBLISH_LEGACY_API_KEY"):
            raise PublishError(
                "legacy_api_key_removed",
                503,
                "Remove PUBLISH_LEGACY_API_KEY and use a scoped device token for each client.",
            )
        operations = policy_section("operations")
        for key in (
            "owner_session_idle_days",
            "owner_session_absolute_days",
            "owner_session_refresh_seconds",
            "owner_recent_auth_seconds",
        ):
            name = "PUBLISH_" + key.upper()
            supplied = os.environ.get(name)
            if supplied is None:
                continue
            try:
                supplied = supplied.strip()
                if not supplied.isascii() or not supplied.isdecimal():
                    raise ValueError("A positive integer is required.")
                value = int(supplied)
                if value <= 0:
                    raise ValueError("A positive integer is required.")
            except ValueError as error:
                raise PublishError(
                    "invalid_session_policy",
                    400,
                    "Set the owner session setting to a positive integer.",
                    field=name,
                ) from error
            operations[key] = value
        data_dir = Path(
            os.getenv(
                "PUBLISH_DATA_DIR",
                str(Path.home() / ".local" / "share" / "minder-publish" / "server"),
            )
        )
        secret = os.getenv("PUBLISH_SECRET", "")
        secret_file = data_dir.expanduser() / "server.secret"
        if secret_file.is_file():
            stored_secret = secret_file.read_text(encoding="utf-8").strip()
            if secret and secret != stored_secret:
                raise PublishError(
                    "secret_override_mismatch",
                    503,
                    "Update or remove PUBLISH_SECRET after restoring or rotating the instance secret.",
                )
            secret = stored_secret
        return cls(
            data_dir=data_dir,
            base_url=os.getenv("PUBLISH_BASE_URL", "http://publish.localhost:8080"),
            content_domain=os.getenv("PUBLISH_CONTENT_DOMAIN", ""),
            secret=secret,
            storage_budget_bytes=int(
                os.getenv("PUBLISH_STORAGE_BUDGET_BYTES", str(20 * 1024**3))
            ),
            legacy_hosts=tuple(
                host.strip().lower()
                for host in os.getenv("PUBLISH_LEGACY_HOSTS", "").split(",")
                if host.strip()
            ),
            remote_mcp_enabled=os.getenv("PUBLISH_REMOTE_MCP", "false").lower()
            in ("true", "1"),
            client_wheel=os.getenv("PUBLISH_CLIENT_WHEEL") or None,
            source_url=os.getenv("PUBLISH_SOURCE_URL", ""),
            project_url=os.getenv("PUBLISH_PROJECT_URL", ""),
            mcp_file_hosts=tuple(
                host.strip().lower()
                for host in os.getenv("PUBLISH_MCP_FILE_HOSTS", "").split(",")
                if host.strip()
            ),
            operations=operations,
        )
