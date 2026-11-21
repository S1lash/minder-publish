"""One versioned, streaming HTTP client shared by the CLI and MCP adapters."""

from __future__ import annotations

import re
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import validate_base_url
from .errors import ClientError
from .preflight import prepare

UNSET = object()
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
LINK_NAMES_UPLOAD_FIELD = "supports_link_names"


def link_name_upload_fields(capabilities: dict) -> dict:
    if capabilities.get("link_names") is True:
        return {LINK_NAMES_UPLOAD_FIELD: True}
    return {}


def identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ClientError(
            "invalid_id",
            "Use the publication identifier returned by the API.",
            field="id",
        )
    return quote(value, safe="")


def expiry(value: str | None) -> dict | None:
    if value is None:
        return None
    if value in ("default", "never"):
        return {"mode": value}
    try:
        duration = re.fullmatch(r"([1-9][0-9]*)([smhdw])", value)
        if duration:
            seconds = (
                int(duration[1])
                * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[duration[2]]
            )
            return {
                "mode": "at",
                "value": (
                    datetime.now(timezone.utc) + timedelta(seconds=seconds)
                ).isoformat(),
            }
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("timezone required")
        return {"mode": "at", "value": when.astimezone(timezone.utc).isoformat()}
    except (ValueError, OverflowError) as error:
        raise ClientError(
            "invalid_expiry",
            "Choose default, never, a duration such as 7d, or an RFC3339 date with a timezone.",
            field="expiry",
        ) from error


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([smhdw])", value)
    if not match:
        raise ClientError(
            "invalid_duration", "Use a positive duration such as 7d or 12h."
        )
    return (
        int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
    )


class PublishClient:
    def __init__(self, base_url: str, token: str, *, transport=None):
        self.base_url = validate_base_url(base_url)
        if (
            not token
            or len(token) > 8192
            or any(character.isspace() for character in token)
        ):
            raise ClientError(
                "invalid_token", "Provide a valid device token.", field="token"
            )
        self._capabilities = {}
        self.http = httpx.Client(
            base_url=self.base_url + "/",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=httpx.Timeout(connect=20, read=120, write=120, pool=20),
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.http.close()

    def request(self, method: str, path: str, **kwargs):
        try:
            response = self.http.request(method, "api/v1/" + path.lstrip("/"), **kwargs)
        except httpx.TimeoutException as error:
            raise ClientError(
                "timeout",
                "The instance did not respond within the operation deadline.",
                action="Check the connection and inspect the publication before retrying a mutation.",
            ) from error
        except httpx.HTTPError as error:
            raise ClientError(
                "transport_error",
                "Could not contact the configured instance.",
                action="Check its URL, TLS certificate and availability.",
            ) from error
        try:
            body = response.json()
        except ValueError:
            body = None
        if not 200 <= response.status_code < 300:
            problem = body if isinstance(body, dict) else {}
            raise ClientError(
                problem.get("code", "http_error"),
                problem.get("detail", "The instance rejected the request."),
                status=response.status_code,
                field=problem.get("field"),
                actual=problem.get("actual"),
                limit=problem.get("limit"),
                action=problem.get("action"),
            )
        if not isinstance(body, dict) or "data" not in body:
            raise ClientError(
                "invalid_response",
                "The instance returned an invalid API envelope.",
                status=502,
            )
        return body["data"]

    def create_file_upload(self, intent: dict) -> dict:
        result = self.request("POST", "upload-jobs", json=intent)
        result["upload_url"] = self.base_url + "/upload/" + identifier(result["id"])
        return result

    def get_file_upload(self, job_id: str) -> dict:
        return self.request("GET", "upload-jobs/" + identifier(job_id))

    def get_upload(self, upload_id: str) -> dict:
        return self.request("GET", "uploads/" + identifier(upload_id))

    def _abort_upload(self, upload_id: str):
        try:
            self.request("DELETE", f"uploads/{upload_id}")
        except Exception:
            pass  # Preserve the failure; server leases bound abandoned staging.

    def _matches_publication_url(self, link_name, value) -> bool:
        if not isinstance(link_name, str) or not IDENTIFIER.fullmatch(link_name):
            return False
        suffix = f"/p/{link_name}"
        if not isinstance(value, str) or not value.endswith(suffix):
            return False
        base = value.removesuffix(suffix)
        return (
            not base.endswith("/")
            and not any(
                character.isspace() or character < " " or character in "\x7f?#\\"
                for character in base
            )
            and validate_base_url(base) == self.base_url
        )

    def commit_upload(self, upload_id: str, *, timeout=None) -> dict:
        """Commit once. A lost outcome must be reconciled, never blindly recreated."""
        upload_id = identifier(upload_id)
        try:
            result = self.request(
                "POST",
                f"uploads/{upload_id}/commit",
                json={},
                **({"timeout": timeout} if timeout is not None else {}),
            )
            if not (
                isinstance(result, dict)
                and isinstance(result.get("id"), str)
                and IDENTIFIER.fullmatch(result["id"])
                and self._matches_publication_url(
                    result.get("link_name", result["id"]), result.get("stable_url")
                )
            ):
                raise ClientError(
                    "invalid_response", "Invalid commit response.", status=502
                )
            return result
        except ClientError as error:
            if (
                400 <= error.status < 500
                and isinstance(error.code, str)
                and error.code
                and error.code != "http_error"
            ):
                self._abort_upload(upload_id)
                raise
            raise ClientError(
                "unknown_outcome",
                "The commit outcome could not be established. The publication may have been created or updated.",
                status=502,
                actual={"upload_id": upload_id},
                action=(
                    f"Run minder-publish upload-status {upload_id} with the same --instance option or environment connection. "
                    f"For MCP, call get_upload with id={upload_id} on the same connection. "
                    "If the outcome cannot be established, stop and do not create duplicate publications."
                ),
            ) from error

    def cancel_file_upload(self, job_id: str) -> dict:
        return self.request("DELETE", "upload-jobs/" + identifier(job_id))

    def capabilities(self) -> dict:
        self._capabilities = self.request("GET", "capabilities")
        return self._capabilities

    def list(
        self,
        *,
        query: str = "",
        kind: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        parameters = {"q": query, "offset": offset, "limit": limit}
        if kind is not None:
            parameters["kind"] = kind
        if status is not None:
            parameters["status"] = status
        return self.request("GET", "publications", params=parameters)

    def get(self, publication_id: str) -> dict:
        return self.request("GET", f"publications/{identifier(publication_id)}")

    def patch(self, publication_id: str, changes: dict) -> dict:
        return self.request(
            "PATCH", f"publications/{identifier(publication_id)}", json=changes
        )

    def pause(self, publication_id: str, paused: bool = True) -> dict:
        return self.request(
            "POST",
            f"publications/{identifier(publication_id)}/pause",
            json={"paused": paused},
        )

    def expire(self, publication_id: str) -> dict:
        return self.request(
            "POST", f"publications/{identifier(publication_id)}/expire", json={}
        )

    def extend(self, publication_id: str, seconds: int) -> dict:
        return self.request(
            "POST",
            f"publications/{identifier(publication_id)}/extend",
            json={"seconds": seconds},
        )

    def delete(self, publication_id: str) -> dict:
        return self.request("DELETE", f"publications/{identifier(publication_id)}")

    def open(self, publication_id: str) -> dict:
        return self.request("GET", f"publications/{identifier(publication_id)}/open")

    @contextmanager
    def prepared(
        self,
        path: str | Path,
        *,
        kind: str,
        entry_path: str = "index.html",
        allow_sensitive: bool = False,
    ):
        capabilities = self.capabilities()
        limits = capabilities.get("limits")
        required = (
            "file_bytes",
            "upload_payload_bytes",
            "site_expanded_bytes",
            "site_files",
            "path_utf8_bytes",
            "path_depth",
        )
        if not isinstance(limits, dict) or any(
            not isinstance(limits.get(name), int) or limits[name] <= 0
            for name in required
        ):
            raise ClientError(
                "invalid_capabilities",
                "The instance returned incomplete upload limits.",
                status=502,
            )
        prepare_seconds = limits.get("prepare_seconds", 300)
        if type(prepare_seconds) is not int or prepare_seconds <= 0:
            raise ClientError(
                "invalid_capabilities",
                "The instance returned an invalid preparation limit.",
                status=502,
            )
        limits = {**limits, "prepare_seconds": prepare_seconds}
        with prepare(
            path,
            kind=kind,
            limits=limits,
            entry_path=entry_path,
            allow_sensitive=allow_sensitive,
        ) as content:
            yield content

    def publish(
        self,
        path: str | Path,
        *,
        kind: str,
        metadata: dict | None = None,
        entry_path: str = "index.html",
        allow_sensitive: bool = False,
        publication_id: str | None = None,
        expected_revision=UNSET,
        expected_manifest: str | None = None,
        dry_run: bool = False,
        on_preflight=None,
        on_inspect=None,
    ) -> dict:
        if expected_manifest is not None and (
            not isinstance(expected_manifest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_manifest) is None
        ):
            raise ClientError("invalid_manifest_digest", "Use the manifest_sha256 returned by preflight.", field="expect_manifest")
        with self.prepared(
            path, kind=kind, entry_path=entry_path, allow_sensitive=allow_sensitive
        ) as content:
            manifest, manifest_sha256 = content.manifest_report(allow_sensitive=allow_sensitive)
            if expected_manifest is not None and expected_manifest != manifest_sha256:
                raise ClientError(
                    "manifest_mismatch", "The prepared content differs from the reviewed preflight. No upload was created.",
                    field="expect_manifest", actual=manifest_sha256,
                    action="Run preflight again and review the changed files before publishing.",
                )
            report = {
                "kind": content.kind,
                "filename": content.filename,
                "size_bytes": content.size_bytes,
                "expanded_size_bytes": content.expanded_size_bytes,
                "files": content.files,
                "excluded": content.excluded,
                "sensitive_override": allow_sensitive,
                "manifest": manifest,
                "manifest_sha256": manifest_sha256,
                "dependencies": content.dependency_report(),
            }
            if on_preflight:
                on_preflight(report)
            if dry_run:
                return report
            plan = dict(metadata or {})
            forbidden = {
                "kind",
                "filename",
                "size_bytes",
                "expanded_size_bytes",
                "publication_id",
                "expected_revision",
                LINK_NAMES_UPLOAD_FIELD,
            } & plan.keys()
            if forbidden:
                raise ClientError(
                    "invalid_metadata",
                    "Upload control fields must not be supplied as metadata.",
                    field=sorted(forbidden)[0],
                )
            plan.update(
                kind=content.kind,
                filename=content.filename,
                size_bytes=content.size_bytes,
            )
            if kind == "site":
                plan.update(
                    entry_path=content.entry_path,
                    expanded_size_bytes=content.expanded_size_bytes,
                )
            if publication_id is not None:
                identifier(publication_id)
                if expected_revision is UNSET:
                    expected_revision = self.get(publication_id)["current_revision"]
                plan.update(
                    publication_id=publication_id, expected_revision=expected_revision
                )
            plan.update(link_name_upload_fields(self._capabilities))
            upload = self.request("POST", "uploads", json=plan)
            upload_id = identifier(upload["id"])
            try:
                # HTTPX does not close arbitrary iterators after a partial write failure.
                with closing(content.chunks()) as chunks:
                    self.request(
                        "PUT",
                        f"uploads/{upload_id}/content",
                        content=chunks,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(content.size_bytes),
                        },
                    )
                operations = self._capabilities.get("operations", {})
                prepare_seconds = self._capabilities.get("limits", {}).get(
                    "prepare_seconds", 300
                )
                deadline = (
                    prepare_seconds
                    + sum(
                        operations.get(name, default)
                        for name, default in (
                            ("prepare_queue_seconds", 600),
                            ("commit_seconds", 30),
                        )
                    )
                    + 60
                )
                timeout = httpx.Timeout(connect=20, read=deadline, write=120, pool=20)
                inspection = self.request(
                    "POST", f"uploads/{upload_id}/inspect", json={}, timeout=timeout
                )
                if (
                    not isinstance(inspection, dict)
                    or inspection.get("state") != "prepared"
                    or not isinstance(inspection.get("dependency_report"), dict)
                ):
                    raise ClientError(
                        "invalid_inspection",
                        "The instance returned an invalid prepared upload report.",
                        status=502,
                    )
                if on_inspect:
                    on_inspect(inspection)
            except BaseException:
                self._abort_upload(upload_id)
                raise
            result = self.commit_upload(upload_id, timeout=timeout)
            result["dependency_report"] = inspection["dependency_report"]
            result["manifest_sha256"] = manifest_sha256
            return result

    def download(
        self, publication_id: str, destination: str | Path, *, overwrite: bool = False
    ) -> dict:
        import os
        import tempfile

        target = Path(destination).expanduser()
        if target.exists() and not overwrite:
            raise ClientError(
                "destination_exists",
                "The destination exists; explicitly choose another path or allow overwrite.",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".publish-download-", dir=target.parent
        )
        count = 0
        try:
            try:
                with self.http.stream(
                    "GET", f"api/v1/publications/{identifier(publication_id)}/download"
                ) as response:
                    if response.status_code != 200:
                        response.read()
                        body = (
                            response.json()
                            if response.headers.get("content-type", "").startswith(
                                "application/"
                            )
                            else {}
                        )
                        raise ClientError(
                            body.get("code", "download_failed"),
                            body.get("detail", "Download failed."),
                            status=response.status_code,
                        )
                    with os.fdopen(descriptor, "wb") as output:
                        descriptor = -1
                        for chunk in response.iter_bytes():
                            output.write(chunk)
                            count += len(chunk)
                if not overwrite and target.exists():
                    raise ClientError(
                        "destination_exists",
                        "The destination was created during download; it was not overwritten.",
                    )
                if overwrite:
                    os.replace(temporary, target)
                else:
                    try:
                        os.link(temporary, target)
                    except FileExistsError as error:
                        raise ClientError(
                            "destination_exists",
                            "The destination appeared during download and was not overwritten.",
                        ) from error
                    except OSError as error:
                        raise ClientError(
                            "atomic_destination_unavailable",
                            "This filesystem cannot create the destination atomically without overwrite. Choose another destination or explicitly allow overwrite.",
                        ) from error
                    Path(temporary).unlink()
                return {"path": str(target), "size_bytes": count}
            except httpx.HTTPError as error:
                raise ClientError(
                    "transport_error",
                    "Download interrupted; the destination was preserved.",
                ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            Path(temporary).unlink(missing_ok=True)
