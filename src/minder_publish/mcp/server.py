"""MCP 2.x tools over the same versioned HTTP client; no remote filesystem tools."""

from __future__ import annotations

import argparse
import inspect
import json
import os
import tempfile
import threading
from contextvars import ContextVar
from importlib.resources import files
from pathlib import Path
from typing import Callable

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict

from minder_publish import __version__
from minder_publish.client import config
from minder_publish.client.api import PublishClient, duration_seconds, expiry
from minder_publish.client.errors import ClientError

from .validation import PublishMCPServer

MAX_INLINE_BYTES = json.loads(
    files("minder_publish").joinpath("defaults.json").read_text(encoding="utf-8")
)["operations"]["mcp_inline_bytes"]
MAX_RPC_BYTES = 2 * MAX_INLINE_BYTES
MAX_REMOTE_REQUESTS = 4
_request_token: ContextVar[str | None] = ContextVar(
    "minder_publish_device_token", default=None
)


class HostFileReference(BaseModel):
    """Host-provided file input; the fetch adapter validates its network boundary."""

    model_config = ConfigDict(extra="forbid")
    download_url: str
    file_id: str
    file_name: str | None = None
    mime_type: str | None = None


def _metadata(
    *,
    ttl=None,
    password=None,
    title=None,
    summary=None,
    change_summary=None,
    access=None,
    expiry_choice=None,
    key=None,
    hint=None,
    policy=None,
) -> dict:
    allowed = {
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
        "entry_path",
    }
    if policy is not None and (not isinstance(policy, dict) or set(policy) - allowed):
        raise ClientError(
            "invalid_policy", "Policy contains unsupported fields.", field="policy"
        )
    data = dict(policy or {})
    if password is not None and access is None:
        access = "password"
    for name, value in (
        ("key", key),
        ("password", password),
        ("password_hint", hint),
        ("title", title),
        ("summary", summary),
        ("change_summary", change_summary),
        ("access", access),
    ):
        if value is not None:
            data[name] = value
    if ttl is not None and expiry_choice is not None:
        raise ClientError(
            "conflicting_expiry", "Provide either ttl or expiry, not both."
        )
    if expiry_choice is not None:
        data["expiry"] = expiry_choice
    elif ttl is not None:
        data["expiry"] = expiry(ttl)
    return data


def _inline_path(directory: str, content: str, filename: str) -> Path:
    if len(content.encode("utf-8")) > MAX_INLINE_BYTES:
        raise ClientError(
            "inline_content_too_large",
            "Inline content exceeds 1 MiB. Use the real file upload flow.",
            field="content",
            limit=MAX_INLINE_BYTES,
        )
    if (
        not filename
        or filename in (".", "..")
        or "/" in filename
        or "\\" in filename
        or ":" in filename
        or len(filename.encode("utf-8")) > 255
        or any(ord(character) < 32 for character in filename)
    ):
        raise ClientError(
            "invalid_filename",
            "Use one portable filename without a path.",
            field="filename",
        )
    path = Path(directory) / filename
    path.write_text(content, encoding="utf-8")
    return path


def create_mcp(
    *,
    local: bool = True,
    client_factory: Callable | None = None,
    resource_metadata: str | None = None,
    host_files=None,
) -> MCPServer:
    def default_factory():
        selected = config.connection()
        return PublishClient(selected.base_url, selected.token)

    factory = client_factory or default_factory
    server = PublishMCPServer(
        "minder-publish",
        version=__version__,
        instructions=(
            "Publish files or explicitly selected static sites to your own instance. Access defaults are server-owned. "
            "Never put device tokens in tool arguments or a skill. Inline content is limited to 1 MiB. "
            "Remote tools do not read server filesystem paths. Returned URLs are authoritative."
        ),
    )

    def auth_meta(scope):
        # Public SDK metadata serializes as _meta on tools/list.
        return (
            None
            if local
            else {"securitySchemes": [{"type": "oauth2", "scopes": [scope]}]}
        )

    def failure(error, scope):
        if (
            not local
            and resource_metadata
            and (
                error.status == 401
                or error.status == 403
                and error.code == "insufficient_scope"
            )
        ):
            code = "invalid_token" if error.status == 401 else "insufficient_scope"
            description = (
                "Sign in to this instance to continue."
                if error.status == 401
                else "Grant the required publication permission to continue."
            )
            challenge = f'Bearer resource_metadata="{resource_metadata}", error="{code}", error_description="{description}", scope="{scope}"'
            return CallToolResult(
                is_error=True,
                content=[TextContent(type="text", text=description)],
                meta={"mcp/www_authenticate": [challenge]},
            )
        raise ToolError(json.dumps(error.as_dict(), ensure_ascii=False)) from None

    def call(method, *args, **kwargs):
        try:
            with factory() as client:
                return getattr(client, method)(*args, **kwargs)
        except ClientError as error:
            return failure(
                error,
                "materials:read"
                if method in ("get", "list", "get_upload")
                else "materials:manage",
            )

    if not local and host_files is not None:

        @server.tool(
            meta={**auth_meta("materials:manage"), "openai/fileParams": ["file"]}
        )
        def publish_host_file(
            file: HostFileReference,
            kind: str = "file",
            title: str | None = None,
            summary: str | None = None,
            access: str | None = None,
            expiry: dict | None = None,
            entry_path: str = "index.html",
            ttl: str | None = None,
        ) -> dict:
            """Publish an actual host-provided attachment using this instance's approved file source.

            Use the host file picker/reference, never an arbitrary URL or a server path.
            kind=site requires an already-built static-site ZIP. Private/link access is
            supported; for a publication password use create_file_upload and let the
            owner choose it privately. Return only the server's confirmed stable URL.
            ttl accepts default/never/7d/RFC3339; use ttl or explicit expiry, not both.
            """
            try:
                metadata = _metadata(
                    title=title,
                    summary=summary,
                    access=access,
                    ttl=ttl,
                    expiry_choice=expiry,
                )
                with factory() as client:
                    return host_files.publish(
                        client,
                        file.model_dump(exclude_none=True),
                        kind=kind,
                        metadata=metadata,
                        entry_path=entry_path,
                    )
            except ClientError as error:
                return failure(error, "materials:manage")

    @server.tool(meta=auth_meta("materials:manage"))
    def publish_content(
        content: str,
        filename: str = "artifact.html",
        ttl: str | None = None,
        password: str | None = None,
        key: str | None = None,
        title: str | None = None,
        hint: str | None = None,
        summary: str = None,
        change_summary: str | None = None,
        access: str | None = None,
        expiry: dict | None = None,
        policy: dict | None = None,
    ) -> dict:
        """Publish bounded inline text/HTML; defaults come from your instance. Never send large binary data as text.

        Access is private, link or password. summary omission creates an empty summary.
        key optionally chooses the initial link name at creation.
        ttl accepts default/never/7d/RFC3339; expiry is the explicit API expiry object.
        """
        try:
            metadata = _metadata(
                ttl=ttl,
                password=password,
                key=key,
                title=title,
                hint=hint,
                summary=summary,
                change_summary=change_summary,
                access=access,
                expiry_choice=expiry,
                policy=policy,
            )
            with tempfile.TemporaryDirectory(prefix="minder-publish-mcp-") as temporary:
                path = _inline_path(temporary, content, filename)
                return call("publish", path, kind="file", metadata=metadata)
        except ClientError as error:
            raise ToolError(json.dumps(error.as_dict(), ensure_ascii=False)) from None

    @server.tool(meta=auth_meta("materials:read"))
    def get_upload(id: str) -> dict:
        """Inspect a known transport upload after an ambiguous commit response.

        Only your own authenticated upload IDs are visible. state=committed confirms
        the historical operation outcome; the returned link may have expired or been
        deleted since. Use get_artifact for current material availability. A missing
        or expired upload record does not prove the commit failed. Never create a
        duplicate when the outcome cannot be established. Browser jobs use get_file_upload.
        """
        return call("get_upload", id)

    @server.tool(meta=auth_meta("materials:manage"))
    def create_file_upload(
        kind: str = "file",
        title: str | None = None,
        summary: str | None = None,
        access: str | None = None,
        expiry: dict | None = None,
        entry_path: str | None = None,
        ttl: str | None = None,
    ) -> dict:
        """Request a browser upload for a PDF, image, other file, or complete static-site ZIP.

        Use when the chat cannot transfer the actual attachment bytes. Give the user
        upload_url: they sign into their own instance, choose the file, review and
        confirm publication. No server paths, tokens, passwords or download URLs.
        kind is file or site. Passwords are chosen privately in the browser.
        ttl accepts default/never/7d/RFC3339; use ttl or explicit expiry, not both.
        Then call get_file_upload with the returned id; never invent a result URL.
        """
        try:
            intent = {
                "kind": kind,
                **_metadata(
                    title=title,
                    summary=summary,
                    access=access,
                    ttl=ttl,
                    expiry_choice=expiry,
                ),
            }
            if entry_path is not None:
                intent["entry_path"] = entry_path
            return call("create_file_upload", intent)
        except ClientError as error:
            return failure(error, "materials:manage")

    @server.tool(meta=auth_meta("materials:manage"))
    def get_file_upload(id: str) -> dict:
        """Check a browser upload request. Only state=published confirms completion.

        Return stable_url only when the server supplies it. Pending/uploading means
        wait for the user; expired/failed/revoked requires a fresh request. Do not poll
        in a tight loop, duplicate a request after timeout, or claim a file was uploaded.
        """
        return call("get_file_upload", id)

    @server.tool(meta=auth_meta("materials:manage"))
    def cancel_file_upload(id: str) -> dict:
        """Cancel your pending browser request. This never deletes a published material."""
        return call("cancel_file_upload", id)

    if local:

        @server.tool()
        def publish_file(
            path: str,
            kind: str = "file",
            ttl: str | None = None,
            password: str | None = None,
            key: str | None = None,
            title: str | None = None,
            hint: str | None = None,
            entry: str = "index.html",
            summary: str = None,
            change_summary: str | None = None,
            access: str | None = None,
            expiry: dict | None = None,
            dry_run: bool = False,
            policy: dict | None = None,
            allow_sensitive: bool = False,
        ) -> dict:
            """LOCAL ONLY: upload a real local file, or choose kind=site for a built static directory.

            ZIP stays a file in file mode. Site mode is explicit. Preflight excludes sensitive
            names and rejects symlinks. dry_run reports the actual prepared manifest without uploading.
            key optionally chooses the initial link name at creation.
            """
            try:
                metadata = _metadata(
                    ttl=ttl,
                    password=password,
                    key=key,
                    title=title,
                    hint=hint,
                    summary=summary,
                    change_summary=change_summary,
                    access=access,
                    expiry_choice=expiry,
                    policy=policy,
                )
                return call(
                    "publish",
                    path,
                    kind=kind,
                    entry_path=entry,
                    metadata=metadata,
                    dry_run=dry_run,
                    allow_sensitive=allow_sensitive,
                )
            except ClientError as error:
                raise ToolError(
                    json.dumps(error.as_dict(), ensure_ascii=False)
                ) from None

        @server.tool()
        def update_file(
            id: str,
            path: str,
            kind: str,
            entry: str = None,
            expected_revision: str = None,
            dry_run: bool = False,
            expiry: dict = None,
            ttl: str = None,
            change_summary: str | None = None,
        ) -> dict:
            """LOCAL ONLY: replace from a local file/site while preserving omitted access, expiry and retention."""
            current = call("get", id) if entry is None else None
            changes = _metadata(ttl=ttl, expiry_choice=expiry, change_summary=change_summary)
            revision = (
                {"expected_revision": expected_revision}
                if expected_revision is not None
                else {}
            )
            return call(
                "publish",
                path,
                kind=kind,
                entry_path=entry
                if entry is not None
                else current.get("entry_path", "index.html"),
                publication_id=id,
                metadata=changes,
                dry_run=dry_run,
                **revision,
            )

    @server.tool(meta=auth_meta("materials:read"))
    def list_artifacts(
        include_inactive: bool = False,
        query: str = "",
        kind: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Find materials by title, summary or filename. Return authoritative stable URLs and pagination."""
        return call(
            "list",
            query=query,
            kind=kind,
            status=status if include_inactive or status else "active",
            offset=offset,
            limit=limit,
        )

    @server.tool(meta=auth_meta("materials:read"))
    def get_artifact(id: str) -> dict:
        """Get one material, current revision, lifecycle and manifest. No secret fields are returned."""
        return call("get", id)

    @server.tool(meta=auth_meta("materials:manage"))
    def update_content(
        id: str,
        content: str,
        filename: str = "artifact.html",
        expected_revision: str = None,
        expiry: dict = None,
        ttl: str = None,
        change_summary: str | None = None,
    ) -> dict:
        """Replace bounded inline text by publication ID returned by list_artifacts.

        Omitted metadata, password, expiry and retention are preserved.
        """
        try:
            with tempfile.TemporaryDirectory(prefix="minder-publish-mcp-") as temporary:
                path = _inline_path(temporary, content, filename)
                revision = (
                    {"expected_revision": expected_revision}
                    if expected_revision is not None
                    else {}
                )
                return call(
                    "publish",
                    path,
                    kind="file",
                    publication_id=id,
                    metadata=_metadata(ttl=ttl, expiry_choice=expiry, change_summary=change_summary),
                    **revision,
                )
        except ClientError as error:
            raise ToolError(json.dumps(error.as_dict(), ensure_ascii=False)) from None

    @server.tool(meta=auth_meta("materials:manage"))
    def set_meta(
        id: str,
        ttl: str | None = None,
        password: str | None = None,
        clear_password: bool = False,
        title: str = None,
        summary: str = None,
        hint: str | None = None,
        access: str | None = None,
        expiry: dict | None = None,
        purge_mode: str | None = None,
        purge_after_seconds: int | None = None,
        metadata_policy: str | None = None,
        metadata_after_purge_seconds: int | None = None,
        policy: dict | None = None,
    ) -> dict:
        """Patch only provided fields; empty summary clears it. Private→external requires explicit expiry.

        Removing password protection requires explicit access=private or link. No implicit public conversion.
        """
        try:
            if clear_password and access not in ("private", "link"):
                raise ClientError(
                    "access_required",
                    "Removing password protection requires explicit access=private or link.",
                )
            changes = _metadata(
                ttl=ttl,
                password=password,
                title=title,
                summary=summary,
                hint=hint,
                access=access,
                expiry_choice=expiry,
                policy=policy,
            )
            for name, value in (
                ("purge_mode", purge_mode),
                ("purge_after_seconds", purge_after_seconds),
                ("metadata_policy", metadata_policy),
                ("metadata_after_purge_seconds", metadata_after_purge_seconds),
            ):
                if value is not None:
                    changes[name] = value
            if not changes:
                raise ClientError(
                    "empty_patch", "Provide at least one field to change."
                )
            return call("patch", id, changes)
        except ClientError as error:
            raise ToolError(json.dumps(error.as_dict(), ensure_ascii=False)) from None

    @server.tool(meta=auth_meta("materials:manage"))
    def extend_artifact(id: str, duration: str) -> dict:
        """Add a duration such as 7d to max(now, current expiry). Manual pause remains enabled."""
        try:
            return call("extend", id, duration_seconds(duration))
        except ClientError as error:
            raise ToolError(json.dumps(error.as_dict(), ensure_ascii=False)) from None

    @server.tool(meta=auth_meta("materials:manage"))
    def delete_artifact(id: str) -> dict:
        """Permanently delete this material after explicit user authorization. Return actual deletion state."""
        return call("delete", id)

    @server.tool(meta=auth_meta("materials:manage"))
    def pause_artifact(id: str, paused: bool = True) -> dict:
        """Close or resume guest access without changing expiry or retention."""
        return call("pause", id, paused)

    @server.tool(meta=auth_meta("materials:manage"))
    def expire_artifact(id: str) -> dict:
        """Expire guest access now and start the material's saved cleanup policy; this can cause deletion."""
        return call("expire", id)

    return server


class AuthenticatedRemoteApp:
    """Bearer authentication and per-request credentials around the SDK ASGI app.

    Mounting servers must enter lifespan_context(inner_app) in their own lifespan;
    ASGI mounts do not automatically run the mounted application's lifespan.
    """

    def __init__(self, app, server: MCPServer, validator, resource_metadata=None):
        self.inner_app, self.mcp, self.validator = app, server, validator
        self.router = app.router
        self.lifespan_context = app.router.lifespan_context
        self.request_slots = threading.BoundedSemaphore(MAX_REMOTE_REQUESTS)
        self.resource_metadata = resource_metadata

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner_app(scope, receive, send)
        values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", [])
            if name.lower() == b"authorization"
        ]
        token = None
        if len(values) == 1:
            scheme, separator, value = values[0].partition(" ")
            if (
                separator
                and scheme.casefold() == "bearer"
                and value
                and len(value) <= 8192
                and not any(c.isspace() for c in value)
            ):
                token = value
        if token is None:
            return await self._reject(send, 401, "authentication_required")
        try:
            subject = self.validator(token)
            if inspect.isawaitable(subject):
                subject = await subject
        except Exception:
            return await self._reject(send, 503, "authentication_unavailable")
        if not isinstance(subject, dict) or not subject:
            return await self._reject(send, 401, "invalid_token")
        scopes = subject.get("scopes", [subject.get("scope")])
        if isinstance(scopes, str):
            scopes = scopes.split()
        if not isinstance(scopes, (list, tuple, set)) or not {
            "materials:read",
            "materials:manage",
        }.intersection(scopes):
            return await self._reject(send, 403, "insufficient_scope")
        # Leave shared AnyIO workers available to the colocated HTTP API.
        if not self.request_slots.acquire(blocking=False):
            return await self._reject(send, 429, "mcp_busy")
        marker = _request_token.set(token)
        try:
            return await self.inner_app(scope, receive, send)
        finally:
            _request_token.reset(marker)
            self.request_slots.release()

    async def _reject(self, send, status: int, code: str):
        body = json.dumps({"error": code}).encode()
        challenge = (
            'Bearer resource_metadata="' + self.resource_metadata + '"'
            if self.resource_metadata
            else 'Bearer realm="minder-publish"'
        )
        if status in (401, 403):
            # Publishing hosts may not perform per-tool scope escalation. Request
            # the publishing workflow at connection time; read grants stay valid.
            challenge += ', scope="materials:manage"'
        if status == 403:
            challenge += ', error="insufficient_scope", error_description="Publication access is required."'
        elif status == 401 and code != "authentication_required":
            challenge += ', error="invalid_token", error_description="Sign in to this instance to continue."'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"www-authenticate", challenge.encode()),
                    *(([(b"retry-after", b"1")]) if status == 429 else []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_remote_app(
    base_url: str,
    token_validator: Callable,
    *,
    allowed_hosts: list[str] | None = None,
    streamable_http_path: str = "/mcp",
    client_factory: Callable | None = None,
    resource_metadata: str | None = None,
    host_files=None,
) -> AuthenticatedRemoteApp:
    base_url = config.validate_base_url(base_url)
    if not callable(token_validator):
        raise ValueError("Remote MCP requires a token validator.")

    def factory():
        token = _request_token.get()
        if not token:
            raise ClientError(
                "authentication_required", "No authenticated MCP request.", status=401
            )
        return (
            client_factory(token) if client_factory else PublishClient(base_url, token)
        )

    server = create_mcp(
        local=False,
        client_factory=factory,
        resource_metadata=resource_metadata,
        host_files=host_files,
    )
    hosts = allowed_hosts or [
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    ]
    security = TransportSecuritySettings(
        allowed_hosts=hosts,
        allowed_origins=[f"https://{host}" for host in hosts if "*" not in host]
        + ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"],
    )
    app = server.streamable_http_app(
        streamable_http_path=streamable_http_path,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_RPC_BYTES,
        transport_security=security,
    )
    return AuthenticatedRemoteApp(app, server, token_validator, resource_metadata)


def main() -> None:
    parser = argparse.ArgumentParser(prog="minder-publish-mcp", description=__doc__)
    parser.add_argument(
        "--http",
        action="store_true",
        help="explicitly enable authenticated remote HTTP",
    )
    parser.add_argument(
        "--api-url", help="control API URL; no credential in this value"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--public-host", help="exact reverse-proxy MCP hostname")
    args = parser.parse_args()
    if not args.http:
        create_mcp().run(transport="stdio")
        return
    base_url = config.validate_base_url(
        args.api_url or os.environ.get("MINDER_PUBLISH_URL", "")
    )

    async def validate(token: str):
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(
                base_url + "/api/v1/capabilities",
                headers={"Authorization": f"Bearer {token}"},
            )
            return (
                {"scopes": ["materials:read"]} if response.status_code == 200 else None
            )

    app = create_remote_app(
        base_url,
        validate,
        allowed_hosts=[args.public_host] if args.public_host else None,
    )
    try:
        import uvicorn
    except ImportError:
        parser.error("HTTP mode requires minder-publish[server,mcp].")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
