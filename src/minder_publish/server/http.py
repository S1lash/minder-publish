"""HTTP admission before parsing and consistent, secret-free responses."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from http.cookies import SimpleCookie

from starlette.requests import Request
from starlette.responses import JSONResponse

from minder_publish.errors import PublishError

logger = logging.getLogger("minder_publish.http")
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def problem(error: PublishError, request_id: str = "") -> JSONResponse:
    return JSONResponse(
        {
            "type": "about:blank",
            "title": error.code.replace("_", " "),
            "status": error.status,
            "code": error.code,
            "detail": error.detail,
            "object": "publication",
            "field": error.field,
            "actual": error.actual,
            "limit": error.limit,
            "action": error.action,
            "params": {},
            "request_id": request_id,
        },
        status_code=error.status,
        headers={"Retry-After": str(error.retry_after)} if error.retry_after is not None else {},
        media_type="application/problem+json",
    )


def success(request: Request, data, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"data": data, "meta": {"request_id": request.state.request_id}},
        status_code=status,
    )


async def json_body(request: Request) -> dict:
    if (
        request.headers.get("content-type", "").split(";")[0].strip()
        != "application/json"
    ):
        raise PublishError("json_required", 415)
    try:
        value = json.loads(await request.body())
    except (ValueError, UnicodeError):
        raise PublishError("invalid_json", 400) from None
    if not isinstance(value, dict):
        raise PublishError("json_object_required", 400)
    return value


def fields(value: dict, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if set(value) - allowed or required - set(value):
        raise PublishError(
            "invalid_fields", 400, "Check the required and supported fields."
        )
    return value


class MCPControlRoute:
    """Exact control routes without mounting over revision author directories."""

    def __init__(self, remote, application):
        self.remote, self.application = remote, application

    async def __call__(self, scope, receive, send):
        state = scope.get("state", {})
        if state.get("revision_id") and scope["method"] in ("GET", "HEAD"):
            response = await self.application.state.content(
                Request(scope, receive), scope["path"].lstrip("/")
            )
            return await response(scope, receive, send)
        if state.get("revision_id") or state.get("legacy_host"):
            return await problem(PublishError("not_found", 404))(scope, receive, send)
        # Both explicit routes reach the SDK's canonical path. This changes only
        # routing, retaining original headers, query string and body stream.
        canonical = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
        return await self.remote(canonical, receive, send)


class Admission:
    def __init__(self, app, config, auth):
        self.app, self.config, self.auth = app, config, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {
            k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]
        }
        path, method = scope["path"], scope["method"]
        request_id = headers.get("x-request-id", "")
        if not SAFE_REQUEST_ID.fullmatch(request_id):
            request_id = uuid.uuid4().hex
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        started, status = time.monotonic(), 500
        response_started = False

        async def secured_send(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                result = list(message.get("headers", []))
                names = {k.lower() for k, v in result}
                for key, value in (
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-request-id", request_id.encode()),
                    (
                        b"permissions-policy",
                        b"camera=(), microphone=(), geolocation=()",
                    ),
                ):
                    if key not in names:
                        result.append((key, value))
                message["headers"] = result
            await send(message)

        try:
            if len(scope.get("query_string", b"")) > 8192:
                raise PublishError("query_too_large", 414, limit=8192)
            for name in (
                b"host",
                b"authorization",
                b"content-length",
                b"origin",
                b"x-csrf-token",
            ):
                if sum(key.lower() == name for key, value in scope["headers"]) > 1:
                    raise PublishError("ambiguous_header", 400)
            host_value = headers.get("host", "").lower()
            # A single literal authority is required; forwarding headers are not trusted here.
            if any(c in host_value for c in "/\\@, \t\r\n"):
                raise PublishError("invalid_host", 400)
            host = host_value.split(":", 1)[0]
            revision = re.fullmatch(
                r"([rg])-([a-f0-9]{32})\." + re.escape(self.config.content_domain), host
            )
            control = host == self.config.control_host
            legacy = host in self.config.legacy_hosts
            if not control and not revision and not legacy:
                raise PublishError("unknown_host", 421)
            state["revision_id"] = revision.group(2) if revision else None
            state["content_mode"] = (
                ("guest" if revision.group(1) == "g" else "normal")
                if revision
                else None
            )
            state["legacy_host"] = legacy and not control
            if legacy and (path.startswith("/api/") or path.startswith("/static/")):
                raise PublishError("not_found", 404)
            if (path == "/mcp" or path.startswith("/mcp/")) and (
                legacy or revision and method not in ("GET", "HEAD")
            ):
                raise PublishError("not_found", 404)
            if (
                headers.get("service-worker", "").lower() == "script"
                or headers.get("sec-fetch-dest", "").lower() == "serviceworker"
            ):
                raise PublishError("service_workers_disabled", 403)
            cookies = SimpleCookie()
            try:
                cookies.load(headers.get("cookie", ""))
            except Exception:
                raise PublishError("invalid_cookie", 400) from None
            owner_raw = cookies.get(self.config.owner_cookie)
            owner_raw = owner_raw.value if owner_raw else None
            principal = None
            if revision:
                content_path = path.lstrip("/")
                if method not in ("GET", "HEAD") or (
                    (content_path == "__mp" or content_path.startswith("__mp/"))
                    and path not in ("/__mp/start", "/__mp/accept")
                ):
                    raise PublishError("not_found", 404)
            public_asset = control and method in ("GET", "HEAD") and path.startswith("/static/")
            if control and not public_asset:
                resolver = scope.get("minder_publish.internal_mcp_principal")
                authorization = headers.get("authorization")
                if resolver is not None:
                    from .auth import Principal

                    principal = resolver()
                    if (
                        not isinstance(principal, Principal)
                        or principal.owner
                        or principal.kind not in ("device", "oauth")
                    ):
                        raise PublishError("invalid_credentials", 401)
                elif authorization and not (
                    path.startswith("/.well-known/")
                    or path in ("/oauth/token", "/oauth/revoke", "/oauth/register")
                    or path == "/mcp"
                    or path.startswith("/mcp/")
                ):
                    if not authorization.startswith("Bearer "):
                        raise PublishError("invalid_credentials", 401)
                    principal = self.auth.token(authorization[7:])
                    if not principal:
                        raise PublishError("invalid_credentials", 401)
                else:
                    principal = self.auth.session(owner_raw)
            state["principal"] = principal
            protected = (
                control
                and path.startswith("/api/")
                and path
                not in (
                    "/api/v1/session",
                    "/api/v1/auth/login",
                )
            )
            if protected:
                if not principal:
                    raise PublishError("authentication_required", 401)
                operation = "read" if method in ("GET", "HEAD") else "manage"
                if not principal.can(operation):
                    raise PublishError("insufficient_scope", 403)
                if (
                    path.startswith(
                        ("/api/v1/settings", "/api/v1/tokens", "/api/v1/auth/")
                    )
                    and not principal.owner
                ):
                    raise PublishError("owner_required", 403)
                if method not in ("GET", "HEAD", "OPTIONS") and principal.owner:
                    if headers.get(
                        "origin"
                    ) != self.config.control_origin or not self.auth.validate_csrf(
                        owner_raw, headers.get("x-csrf-token")
                    ):
                        raise PublishError("csrf_rejected", 403)
            if (
                control
                and path == "/api/v1/auth/login"
                and headers.get("origin")
                not in (
                    None,
                    self.config.control_origin,
                )
            ):
                raise PublishError("csrf_rejected", 403)
            maximum = int(self.config.limits["metadata_body_bytes"])
            if control and (path == "/mcp" or path.startswith("/mcp/")):
                maximum = 2 * 1024**2
            if (
                control
                and re.fullmatch(r"/api/v1/uploads/[a-f0-9]{32}/content", path)
                and method == "PUT"
            ):
                maximum = int(self.config.ceilings["upload_payload_bytes"])
            if control and (
                path == "/api/publish"
                or re.fullmatch(r"/api/artifact/[^/]+/content", path)
            ):
                maximum = int(self.config.ceilings["upload_payload_bytes"]) + int(
                    self.config.limits["multipart_overhead_bytes"]
                )
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise PublishError("content_encoding_unsupported", 415)
            if "content-length" in headers:
                try:
                    length = int(headers["content-length"])
                except ValueError:
                    raise PublishError("invalid_content_length", 400) from None
                if length < 0 or length > maximum:
                    raise PublishError("body_too_large", 413, limit=maximum)
            consumed = 0

            async def bounded_receive():
                nonlocal consumed
                message = await receive()
                if message["type"] == "http.request":
                    consumed += len(message.get("body", b""))
                    if consumed > maximum:
                        raise PublishError("body_too_large", 413, limit=maximum)
                return message

            if revision:
                # Starlette sets scope["app"] to the root application before
                # admission. Dispatch by validated host before any control route
                # or mount can claim an author's file path.
                request = Request(scope, bounded_receive)
                response = await request.app.state.content(request, path.lstrip("/"))
                await response(scope, bounded_receive, secured_send)
            else:
                await self.app(scope, bounded_receive, secured_send)
        except PublishError as error:
            if response_started:
                raise
            await problem(error, request_id)(scope, receive, secured_send)
        except (sqlite3.Error, OSError):
            if response_started:
                raise
            await problem(
                PublishError(
                    "storage_unavailable", 503, "Storage is temporarily unavailable."
                ),
                request_id,
            )(scope, receive, secured_send)
        finally:
            principal = state.get("principal")
            actor = "anonymous"
            if principal and principal.owner:
                actor = "owner"
            elif (
                principal
                and principal.kind in ("device", "oauth")
                and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", principal.id)
            ):
                actor = principal.kind + ":" + principal.id
            logger.info(
                json.dumps(
                    {
                        "log_schema_version": "1.0.0",
                        "event": "http_request",
                        "actor": actor,
                        "request_id": request_id,
                        "method": method,
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    }
                )
            )
