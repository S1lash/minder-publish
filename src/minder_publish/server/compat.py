"""Bounded compatibility API for legacy clients during migration."""

from __future__ import annotations

import contextlib
import re
import tempfile
from datetime import datetime, timedelta, timezone

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse

from minder_publish.errors import PublishError

from .publication_links import stable_url


def expiry(spec, base=None):
    if not isinstance(spec, str):
        raise PublishError("invalid_ttl", 400, field="ttl")
    spec = spec.strip().lower()
    if spec in ("never", "none", "0", "infinite", ""):
        return {"mode": "never"}
    match = re.fullmatch(r"(\d+)([smhdw])", spec)
    try:
        if match:
            seconds = (
                int(match[1])
                * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
            )
            value = (base or datetime.now(timezone.utc)) + timedelta(seconds=seconds)
        else:
            value = datetime.fromisoformat(spec)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        raise PublishError("invalid_ttl", 400, field="ttl") from None
    return {"mode": "at", "value": value.isoformat()}


def legacy(config, value):
    return {
        "id": value["id"],
        "url": stable_url(config.base_url, value["link_name"]),
        "title": value["title"],
        "summary": value["summary"],
        "kind": "site_dir" if value["kind"] == "site" else "single_file",
        "entry_path": value["entry_path"],
        "content_type": value["content_type"],
        "size_bytes": value["size_bytes"],
        "created_at": value["created_at"],
        "expires_at": value["expires_at"],
        "has_password": value["has_password"],
        "password_hint": value.get("password_hint"),
        "status": value["status"],
        "view_count": value["view_count"],
        "last_viewed": None,
    }


def install_compat(app, config, core):
    @contextlib.asynccontextmanager
    async def form(request, upload=False):
        reservation = None
        parsed = None
        subject = request.state.principal.subject
        try:
            if upload:
                if not request.headers.get("content-type", "").startswith(
                    "multipart/form-data;"
                ):
                    raise PublishError("multipart_required", 415)
                if "content-length" not in request.headers:
                    raise PublishError("content_length_required", 411)
                maximum = (
                    (await run_in_threadpool(core.policy_settings))["limits"]["upload_payload_bytes"]
                    + config.limits["multipart_overhead_bytes"]
                )
                length = int(request.headers["content-length"])
                if length > maximum:
                    raise PublishError("body_too_large", 413, limit=maximum)
                reservation = await run_in_threadpool(
                    core.reserve_external, length, subject, "multipart"
                )
                directory = config.data_dir / "parser-temp"
                directory.mkdir(mode=0o700, exist_ok=True)

                class Parser(MultiPartParser):
                    header_bytes = 0

                    def on_header_field(self, data, start, end):
                        self.header_bytes += end - start
                        if self.header_bytes > 16384:
                            raise MultiPartException("Multipart headers too large")
                        super().on_header_field(data, start, end)

                    def on_header_value(self, data, start, end):
                        self.header_bytes += end - start
                        if self.header_bytes > 16384:
                            raise MultiPartException("Multipart headers too large")
                        super().on_header_value(data, start, end)

                    def on_headers_finished(self):
                        super().on_headers_finished()
                        if self._current_part.file is not None:
                            self._current_part.file.file.close()
                            replacement = tempfile.TemporaryFile(dir=directory)
                            self._current_part.file.file = replacement
                            self._files_to_close_on_error[-1] = replacement

                parsed = await Parser(
                    request.headers,
                    request.stream(),
                    max_files=1,
                    max_fields=20,
                    max_part_size=65536,
                ).parse()
            else:
                parsed = await request.form(
                    max_files=0, max_fields=20, max_part_size=65536
                )
            if len(parsed.multi_items()) != len(parsed):
                raise PublishError("duplicate_form_field", 400)
            yield parsed
        except MultiPartException:
            raise PublishError(
                "invalid_multipart", 400, "Check the multipart file and field limits."
            ) from None
        finally:
            if parsed:
                await parsed.close()
            if reservation:
                await run_in_threadpool(core.release_external, reservation, subject)

    async def uploaded(request, values, identifier=None):
        file = values.get("file")
        if not isinstance(file, UploadFile) or not file.filename:
            raise PublishError("file_required", 400)
        filename = file.filename
        if "/" in filename or "\\" in filename:
            raise PublishError("invalid_filename", 400)
        kind = values.get("kind", "single_file")
        if kind not in ("single_file", "site_dir"):
            raise PublishError("invalid_kind", 400)
        plan = {
            "kind": "site" if kind == "site_dir" else "file",
            "filename": filename,
            "size_bytes": file.size,
            "entry_path": values.get("entry", "index.html"),
        }
        if plan["kind"] == "file":
            plan.pop("entry_path")
        existing = None
        if identifier:
            existing = await run_in_threadpool(core.get_publication, identifier)
        elif values.get("key"):
            try:
                existing = await run_in_threadpool(core.get_publication, values["key"])
            except PublishError as error:
                if error.status != 404:
                    raise
            if existing and values.get("overwrite", "").lower() != "true":
                raise PublishError("id_exists", 409)
        if existing:
            plan.update(
                publication_id=existing["id"],
                expected_revision=existing["current_revision"],
                supports_link_names=True,
            )
            if kind == "site_dir" and "entry" not in values:
                plan["entry_path"] = existing["entry_path"]
        else:
            # Legacy publishing is explicitly link-oriented; normal v1 defaults remain private.
            plan["access"] = "password" if values.get("password") else "link"
            plan["expiry"] = (
                expiry(values["ttl"]) if "ttl" in values else {"mode": "default"}
            )
        for key in ("title", "summary"):
            if key in values:
                plan[key] = values[key]
        if "hint" in values:
            plan["password_hint"] = values["hint"]
        if "ttl" in values:
            plan["expiry"] = expiry(values["ttl"])
        if values.get("password"):
            plan.update(access="password", password=values["password"])
        subject = request.state.principal.subject
        upload = await run_in_threadpool(
            core.create_upload,
            plan,
            subject,
            requested_id=(values.get("key") or None) if not existing else None,
        )
        try:

            async def chunks():
                while chunk := await file.read(1024**2):
                    yield chunk

            await core.receive_upload(upload["id"], chunks(), subject)
            value = await run_in_threadpool(core.commit_upload, upload["id"], subject)
            return legacy(config, value)
        except BaseException:
            await run_in_threadpool(core.abort_upload, upload["id"], subject)
            raise

    @app.post("/api/publish")
    async def publish(request: Request):
        async with form(request, upload=True) as values:
            return JSONResponse(await uploaded(request, values), status_code=201)

    @app.put("/api/artifact/{identifier}/content")
    async def replace(identifier: str, request: Request):
        async with form(request, upload=True) as values:
            return JSONResponse(await uploaded(request, values, identifier))

    @app.get("/api/artifact")
    async def listing(request: Request):
        result = []
        offset = 0
        while True:
            page = await run_in_threadpool(
                core.list_publications, offset=offset, limit=200
            )
            result.extend(legacy(config, value) for value in page["items"])
            if len(result) >= page["total"]:
                break
            offset += 200
        return JSONResponse(result)

    @app.get("/api/artifact/{identifier}")
    async def detail(identifier: str, request: Request):
        return JSONResponse(
            legacy(config, await run_in_threadpool(core.get_publication, identifier))
        )

    @app.patch("/api/artifact/{identifier}")
    async def patch(identifier: str, request: Request):
        async with form(request) as values:
            previous = await run_in_threadpool(core.get_publication, identifier)
            patch = {key: values[key] for key in ("title", "summary") if key in values}
            if "hint" in values:
                patch["password_hint"] = values["hint"]
            if "ttl" in values:
                base = None
                if (
                    values.get("extend", "").lower() == "true"
                    and previous["expires_at"]
                ):
                    base = max(
                        datetime.now(timezone.utc),
                        datetime.fromisoformat(previous["expires_at"]),
                    )
                patch["expiry"] = expiry(values["ttl"], base)
            if values.get("clear_password", "").lower() == "true":
                patch.update(access="link", password_hint="")
            elif values.get("password"):
                patch.update(access="password", password=values["password"])
            return JSONResponse(
                legacy(
                    config,
                    await run_in_threadpool(core.patch_publication, identifier, patch),
                )
            )

    @app.delete("/api/artifact/{identifier}")
    async def delete(identifier: str, request: Request):
        value = await run_in_threadpool(core.delete_publication, identifier)
        return JSONResponse({**value, "deleted": False})
