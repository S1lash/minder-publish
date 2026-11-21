"""Control-plane API and packaged owner interface."""

from __future__ import annotations

import asyncio
import contextlib
import html
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from minder_publish.errors import PublishError

from .auth import AuthStore, Principal
from .brand import inline_marks
from .config import Config
from .core import Core
from .distribution import Distribution
from .http import Admission, MCPControlRoute, fields, json_body, problem, success
from .previews import metadata
from .publication_links import stable_url
from .source_footer import source_footer
from .static import PackagedStaticFiles
from .statistics import Statistics
from .upload_jobs import UploadJobs

PUBLICATION_FIELDS = frozenset(
    "id link_name title summary password_hint kind filename entry_path content_type size_bytes created_at updated_at expires_at access has_password password_copy_available paused content_state library_status purge_mode purge_after_seconds metadata_policy metadata_after_purge_seconds site_routing external_resources preview indexing guest_counts owner_counts current_revision access_version view_count purge_due_at files thumbnail_state purged_at purge_reason missing_on_import_at dependency_report".split()
)


def token_dates(value):
    return {
        key: datetime.fromtimestamp(item, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        if key.endswith("_at") and isinstance(item, (int, float))
        else item
        for key, item in value.items()
    }


def publication(config: Config, value: dict) -> dict:
    result = {key: value[key] for key in PUBLICATION_FIELDS if key in value}
    result["stable_url"] = stable_url(config.base_url, value["link_name"])
    return result


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    config.require_secret()
    core, auth = Core(config), AuthStore(config)
    statistics = Statistics(core)
    jobs = UploadJobs(core)
    distribution = Distribution(config.base_url, client_wheel=config.client_wheel)
    remote = None
    oauth = None
    if config.remote_mcp_enabled:
        from minder_publish.client.api import PublishClient
        from minder_publish.mcp.host_files import HostFiles
        from minder_publish.mcp.internal_api import InternalApiTransport
        from minder_publish.mcp.server import create_remote_app

        from .oauth.routes import OAuthService

        oauth = OAuthService(config, auth)

        def resolve_token(raw):
            principal = auth.token(raw)
            if principal:
                return principal
            grant = oauth.store.principal(raw, config.base_url + "/mcp")
            return Principal("oauth", grant["id"], grant["scope"]) if grant else None

        async def validate_token(raw):
            principal = await run_in_threadpool(resolve_token, raw)
            return {"scope": principal.scope, "id": principal.id} if principal else None

        def remote_client(raw):
            return PublishClient(
                config.base_url,
                raw,
                transport=InternalApiTransport(app, lambda: resolve_token(raw)),
            )

        remote = create_remote_app(
            config.base_url,
            validate_token,
            allowed_hosts=[urlsplit(config.base_url).netloc],
            streamable_http_path="/mcp",
            client_factory=remote_client,
            host_files=HostFiles(config.mcp_file_hosts)
            if config.mcp_file_hosts
            else None,
            resource_metadata=config.base_url
            + "/.well-known/oauth-protected-resource/mcp",
        )

    @contextlib.asynccontextmanager
    async def lifespan(app):
        from .backup import instance_lock

        cleanup_tasks = [
            ("materials", core.sweep),
            ("auth", auth.cleanup),
            ("upload_jobs", jobs.cleanup),
        ]
        if oauth:
            cleanup_tasks.append(("oauth", oauth.store.cleanup))

        stop_housekeeping = asyncio.Event()

        async def housekeeping():
            while not stop_housekeeping.is_set():
                try:
                    await asyncio.wait_for(
                        stop_housekeeping.wait(),
                        timeout=config.operations["sweeper_interval_seconds"],
                    )
                except TimeoutError:
                    pass
                for name, operation in cleanup_tasks:
                    if stop_housekeeping.is_set():
                        return
                    try:
                        await run_in_threadpool(operation)
                    except Exception:
                        # One failed task must not suppress credential or upload cleanup.
                        import logging

                        logging.getLogger("minder_publish.lifecycle").error(
                            '{"log_schema_version":"1.0.0","event":"cleanup_failed","task":"%s"}',
                            name,
                        )

        with instance_lock(config):
            await run_in_threadpool(core.initialize)
            await run_in_threadpool(auth.initialize)
            await run_in_threadpool(jobs.initialize)
            if oauth:
                await run_in_threadpool(oauth.initialize)
            async with contextlib.AsyncExitStack() as stack:
                if remote:
                    await stack.enter_async_context(
                        remote.lifespan_context(remote.inner_app)
                    )
                task = asyncio.create_task(housekeeping())
                try:
                    yield
                finally:
                    stop_housekeeping.set()
                    await task
                    await run_in_threadpool(core.close)

    app = FastAPI(
        title="Minder Publish",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config, app.state.core, app.state.auth = config, core, auth
    app.state.oauth = oauth
    app.state.jobs, app.state.distribution = jobs, distribution
    app.add_middleware(Admission, config=config, auth=auth)

    @app.exception_handler(PublishError)
    async def domain_error(request, error):
        return problem(error, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return problem(
            PublishError("invalid_request", 422, "Check the request fields and types."),
            request.state.request_id,
        )

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def storage_error(request, error):
        return problem(
            PublishError(
                "storage_unavailable",
                503,
                "Storage is unavailable. Check the server volume and its database.",
            ),
            request.state.request_id,
        )

    @app.get("/healthz")
    async def health(request: Request):
        if request.state.revision_id or request.state.legacy_host:
            raise PublishError("not_found", 404)

        def ready():
            from .repository import SCHEMA_VERSION

            try:
                with sqlite3.connect(
                    config.db_path.as_uri() + "?mode=ro", uri=True, timeout=2
                ) as connection:
                    core_version = connection.execute(
                        "SELECT version FROM core_schema_version"
                    ).fetchone()
                    auth_version = connection.execute(
                        "SELECT version FROM auth_schema_version WHERE id=1"
                    ).fetchone()
                    return bool(
                        core_version
                        and core_version[0] == SCHEMA_VERSION
                        and auth_version
                        and auth_version[0] == AuthStore.schema_version
                    )
            except sqlite3.Error:
                return False

        healthy = await run_in_threadpool(ready)
        return Response(
            '{"status":"ok"}' if healthy else '{"status":"degraded"}',
            status_code=200 if healthy else 503,
            media_type="application/json",
        )

    @app.get("/api/v1/session")
    async def session(request: Request):
        principal = request.state.principal
        data = {
            "authenticated": bool(principal and principal.owner),
            "setup_required": not auth.initialized(),
        }
        if data["authenticated"]:
            data.update(
                username=auth.username(),
                **auth.session_info(request.cookies[config.owner_cookie]),
            )
        return success(request, data)

    @app.get("/metrics")
    async def metrics(request: Request):
        if request.state.revision_id or request.state.legacy_host:
            raise PublishError("not_found", 404)
        principal = request.state.principal
        if not principal or not principal.can("read"):
            raise PublishError("authentication_required", 401)
        snapshot = await run_in_threadpool(core.storage_snapshot)
        lines = []
        for key in (
            "used_bytes",
            "reserved_bytes",
            "quota_bytes",
            "storage_budget_bytes",
            "free_bytes",
        ):
            if key in snapshot:
                lines.extend(
                    [
                        f"# TYPE minder_publish_{key} gauge",
                        f"minder_publish_{key} {int(snapshot[key])}",
                    ]
                )
        with core.repository.connect() as connection:
            count = connection.execute("SELECT count(*) FROM publication").fetchone()[0]
        lines.extend(
            [
                "# TYPE minder_publish_publications gauge",
                f"minder_publish_publications {count}",
            ]
        )
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.post("/api/v1/auth/login")
    async def login(request: Request):
        value = fields(
            await json_body(request), {"username", "password"}, {"username", "password"}
        )
        if not all(isinstance(value[k], str) for k in value):
            raise PublishError("invalid_credentials", 401)
        token, data = await run_in_threadpool(
            auth.login,
            value["username"],
            value["password"],
            request.client.host if request.client else "unknown",
        )
        response = success(
            request,
            {
                "authenticated": True,
                "username": data["username"],
                "csrf_token": data["csrf_token"],
            },
        )
        response.set_cookie(
            config.owner_cookie,
            token,
            max_age=data["expires_in"],
            secure=config.cookie_secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    def owner_session_response(request, data):
        response = success(request, data)
        if data.get("renewed"):
            response.set_cookie(
                config.owner_cookie,
                request.cookies[config.owner_cookie],
                max_age=data["expires_in"],
                secure=config.cookie_secure,
                httponly=True,
                samesite="lax",
                path="/",
            )
        return response

    @app.post("/api/v1/auth/refresh")
    async def refresh_session(request: Request):
        fields(await json_body(request), set())
        data = await run_in_threadpool(
            auth.refresh_session, request.cookies.get(config.owner_cookie)
        )
        return owner_session_response(request, data)

    @app.post("/api/v1/auth/reauth")
    async def reauthenticate(request: Request):
        value = fields(await json_body(request), {"password"}, {"password"})
        if not isinstance(value["password"], str):
            raise PublishError("invalid_credentials", 401)
        data = await run_in_threadpool(
            auth.reauthenticate,
            request.cookies.get(config.owner_cookie),
            value["password"],
            request.client.host if request.client else "unknown",
        )
        return owner_session_response(request, data)

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request):
        await run_in_threadpool(auth.logout, request.cookies.get(config.owner_cookie))
        response = success(request, {"authenticated": False})
        response.delete_cookie(
            config.owner_cookie,
            path="/",
            secure=config.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/api/v1/settings")
    async def settings(request: Request):
        return success(request, await run_in_threadpool(core.settings))

    @app.patch("/api/v1/settings")
    async def update_settings(request: Request):
        return success(
            request,
            await run_in_threadpool(core.update_settings, await json_body(request)),
        )

    @app.get("/api/v1/capabilities")
    async def capabilities(request: Request):
        value = await run_in_threadpool(core.policy_settings)
        return success(
            request,
            {
                **{
                    k: value[k]
                    for k in ("defaults", "limits", "ceilings", "operations")
                },
                "supported_kinds": ["file", "site"],
                "ui": {"languages": ["en", "ru", "es", "fr"]},
                "remote_mcp": config.remote_mcp_enabled,
                "browser_upload_jobs": True,
                "link_names": True,
                "remote_file_input": bool(
                    config.remote_mcp_enabled and config.mcp_file_hosts
                ),
            },
        )

    @app.get("/api/v1/distribution")
    async def distribution_metadata(request: Request):
        return success(request, distribution.metadata())

    @app.get("/downloads/{name}")
    async def download_code_asset(name: str, request: Request):
        if request.state.revision_id:
            return await app.state.content(request, "downloads/" + name)
        if request.state.legacy_host:
            from .content import legacy_redirect

            return legacy_redirect(config, "downloads/" + name, request)
        content, media_type = distribution.asset(name)
        return Response(
            content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/v1/connections")
    async def connections(request: Request):
        if not request.state.principal.owner:
            raise PublishError("owner_required", 403)
        values = await run_in_threadpool(oauth.store.connections) if oauth else []
        return success(
            request,
            {
                "enabled": bool(oauth),
                "connections": [token_dates(item) for item in values],
            },
        )

    @app.delete("/api/v1/connections/{identifier}")
    async def revoke_connection(identifier: str, request: Request):
        if not request.state.principal.owner:
            raise PublishError("owner_required", 403)
        if not oauth:
            raise PublishError("remote_mcp_disabled", 409)
        await run_in_threadpool(oauth.store.revoke_connection, identifier)
        return success(request, {"id": identifier, "revoked": True})

    @app.post("/api/v1/upload-jobs")
    async def create_upload_job(request: Request):
        intent = await json_body(request)
        value = await run_in_threadpool(jobs.create, intent, request.state.principal)
        return success(request, value, 201)

    @app.get("/api/v1/upload-jobs/{identifier}")
    async def upload_job(identifier: str, request: Request):
        return success(
            request,
            await run_in_threadpool(jobs.get, identifier, request.state.principal),
        )

    @app.post("/api/v1/upload-jobs/{identifier}/start")
    async def start_upload_job(identifier: str, request: Request):
        plan = await json_body(request)
        return success(
            request,
            await run_in_threadpool(
                jobs.start, identifier, request.state.principal, plan
            ),
            201,
        )

    @app.delete("/api/v1/upload-jobs/{identifier}")
    async def cancel_upload_job(identifier: str, request: Request):
        return success(
            request,
            await run_in_threadpool(jobs.cancel, identifier, request.state.principal),
        )

    @app.get("/api/v1/tokens")
    async def tokens(request: Request):
        return success(
            request,
            [token_dates(value) for value in await run_in_threadpool(auth.list_tokens)],
        )

    @app.post("/api/v1/tokens")
    async def create_token(request: Request):
        owner_token = request.cookies.get(config.owner_cookie)
        await run_in_threadpool(auth.require_recent_auth, owner_token)
        value = fields(await json_body(request), {"name", "scope"}, {"name", "scope"})
        if not isinstance(value["name"], str) or not isinstance(value["scope"], str):
            raise PublishError("invalid_device_token", 400)
        return success(
            request,
            token_dates(
                await run_in_threadpool(
                    auth.create_token,
                    **value,
                    authorize=lambda connection: auth.require_recent_auth(
                        owner_token, connection=connection
                    ),
                )
            ),
            201,
        )

    @app.delete("/api/v1/tokens/{identifier}")
    async def revoke_token(identifier: str, request: Request):
        await run_in_threadpool(auth.revoke_token, identifier)
        return success(request, {"id": identifier, "revoked": True})

    @app.get("/api/v1/publications")
    async def publications(
        request: Request,
        q: str = "",
        kind: str = "",
        status: str = "",
        states: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ):
        if len(q) > 2000:
            raise PublishError("invalid_query", 400, field="q", limit=2000)
        value = await run_in_threadpool(
            core.list_publications,
            query=q,
            kind=kind or None,
            status=status or None,
            states=states,
            offset=offset,
            limit=limit,
        )
        value["items"] = [publication(config, item) for item in value["items"]]
        if request.state.principal and request.state.principal.owner:
            totals = await run_in_threadpool(statistics.totals, [item["id"] for item in value["items"]])
            for item in value["items"]:
                item.update(totals.get(item["id"], {"guest_open_count": 0, "owner_open_count": 0}))
        return success(request, value)

    @app.get("/api/v1/publications/{identifier}")
    async def detail(identifier: str, request: Request):
        value = publication(config, await run_in_threadpool(core.get_publication, identifier, manifest=True))
        if request.state.principal and request.state.principal.owner:
            totals = await run_in_threadpool(statistics.totals, [identifier])
            value.update(totals.get(identifier, {"guest_open_count": 0, "owner_open_count": 0}))
        return success(request, value)

    @app.post("/api/v1/publications/{identifier}/password-copy")
    async def copy_password(identifier: str, request: Request):
        if not request.state.principal.owner:
            raise PublishError("owner_required", 403)
        if await request.body():
            raise PublishError("unexpected_body", 400)
        return success(
            request,
            await run_in_threadpool(
                core.copy_publication_password,
                identifier,
                lambda connection: auth.require_recent_auth(
                    request.cookies.get(config.owner_cookie), connection=connection
                ),
            ),
        )

    def require_owner(request):
        if not request.state.principal or not request.state.principal.owner:
            raise PublishError("owner_required", 403)

    def require_live_owner(request, connection):
        if not auth.session(request.cookies.get(config.owner_cookie), connection=connection):
            raise PublishError("authentication_required", 401)

    @app.get("/api/v1/publications/{identifier}/link")
    async def current_publication_link(identifier: str, request: Request):
        require_owner(request)
        return success(request, await run_in_threadpool(core.publication_link, identifier))

    @app.patch("/api/v1/publications/{identifier}/link")
    async def rename_publication_link(identifier: str, request: Request):
        require_owner(request)
        keys = {"name", "keep_previous", "expected_name"}
        value = fields(await json_body(request), keys, keys)
        result = await run_in_threadpool(
            core.rename_publication_link,
            identifier,
            value["name"],
            value["keep_previous"],
            value["expected_name"],
            authorize=lambda connection: require_live_owner(request, connection),
        )
        return success(request, result)

    @app.delete("/api/v1/publications/{identifier}/link/{name}")
    async def disable_publication_alias(identifier: str, name: str, request: Request):
        require_owner(request)
        if await request.body():
            raise PublishError("unexpected_body", 400)
        result = await run_in_threadpool(
            core.disable_publication_alias,
            identifier,
            name,
            authorize=lambda connection: require_live_owner(request, connection),
        )
        return success(request, result)

    @app.get("/api/v1/publications/{identifier}/revisions")
    async def revisions(identifier: str, request: Request):
        require_owner(request)
        return success(request, await run_in_threadpool(core.history, identifier))

    @app.post("/api/v1/publications/{identifier}/revisions/{revision_id}/restore")
    async def restore_revision(identifier: str, revision_id: str, request: Request):
        require_owner(request)
        value = await json_body(request)
        fields(value, {"expected_revision"})
        expected = value.get("expected_revision")
        if not isinstance(expected, str) or not expected:
            raise PublishError("expected_revision_required", 400, field="expected_revision")
        await run_in_threadpool(auth.require_recent_auth, request.cookies.get(config.owner_cookie))
        result = await run_in_threadpool(
            core.restore_revision, identifier, revision_id, expected,
            request.state.principal.subject,
            authorize=lambda connection, upload_id: auth.require_recent_auth(request.cookies.get(config.owner_cookie), connection=connection),
        )
        return success(request, publication(config, result))

    @app.post("/api/v1/publications/{identifier}/revisions/{revision_id}/sharing")
    async def revision_sharing(identifier: str, revision_id: str, request: Request):
        require_owner(request)
        value = fields(await json_body(request), {"enabled"}, {"enabled"})
        if not isinstance(value["enabled"], bool):
            raise PublishError("invalid_boolean", 400, field="enabled")
        result = await run_in_threadpool(
            core.share_revision, identifier, revision_id, value["enabled"],
            authorize=lambda connection: auth.require_recent_auth(
                request.cookies.get(config.owner_cookie), connection=connection
            ),
        )
        return success(request, result)

    @app.get("/api/v1/publications/{identifier}/statistics")
    async def publication_statistics(identifier: str, request: Request, days: int = 30):
        require_owner(request)
        return success(request, await run_in_threadpool(statistics.read, identifier, days))

    @app.delete("/api/v1/publications/{identifier}/statistics")
    async def clear_statistics(identifier: str, request: Request):
        require_owner(request)
        await run_in_threadpool(statistics.clear, identifier, lambda connection: auth.require_recent_auth(request.cookies.get(config.owner_cookie), connection=connection))
        return success(request, {"cleared": True})

    @app.patch("/api/v1/publications/{identifier}")
    async def patch(identifier: str, request: Request):
        return success(
            request,
            publication(
                config,
                await run_in_threadpool(
                    core.patch_publication, identifier, await json_body(request)
                ),
            ),
        )

    @app.post("/api/v1/publications/{identifier}/pause")
    async def pause(identifier: str, request: Request):
        value = fields(await json_body(request), {"paused"}, {"paused"})
        return success(
            request,
            publication(
                config, await run_in_threadpool(core.pause, identifier, value["paused"])
            ),
        )

    @app.post("/api/v1/publications/{identifier}/expire")
    async def expire(identifier: str, request: Request):
        return success(
            request,
            publication(config, await run_in_threadpool(core.expire, identifier)),
        )

    @app.post("/api/v1/publications/{identifier}/extend")
    async def extend(identifier: str, request: Request):
        value = fields(await json_body(request), {"seconds"}, {"seconds"})
        return success(
            request,
            publication(
                config,
                await run_in_threadpool(core.extend, identifier, value["seconds"]),
            ),
        )

    @app.delete("/api/v1/publications/{identifier}")
    async def delete(identifier: str, request: Request):
        return success(
            request, await run_in_threadpool(core.delete_publication, identifier)
        )

    @app.get("/api/v1/publications/{identifier}/open")
    async def opening(identifier: str, request: Request):
        value = await run_in_threadpool(core.get_publication, identifier)
        return success(request, {"url": publication(config, value)["stable_url"]})

    @app.post("/api/v1/uploads")
    async def plan_upload(request: Request):
        return success(
            request,
            await run_in_threadpool(
                core.create_upload,
                await json_body(request),
                request.state.principal.subject,
            ),
            201,
        )

    @app.get("/api/v1/uploads/{identifier}")
    async def upload_status(identifier: str, request: Request):
        return success(
            request,
            await run_in_threadpool(
                core.upload_status, identifier, request.state.principal.subject
            ),
        )

    @app.put("/api/v1/uploads/{identifier}/content")
    async def receive_upload(identifier: str, request: Request):
        await run_in_threadpool(
            jobs.authorize_upload, identifier, request.state.principal
        )

        async def chunks():
            async for chunk in request.stream():
                for offset in range(0, len(chunk), 1024**2):
                    yield memoryview(chunk)[offset : offset + 1024**2]

        return success(
            request,
            await core.receive_upload(
                identifier, chunks(), request.state.principal.subject
            ),
        )

    @app.post("/api/v1/uploads/{identifier}/commit")
    async def commit(identifier: str, request: Request):
        await run_in_threadpool(
            jobs.authorize_upload, identifier, request.state.principal
        )
        return success(
            request,
            publication(
                config,
                await run_in_threadpool(
                    core.commit_upload,
                    identifier,
                    request.state.principal.subject,
                    authorize=lambda connection, upload_id: jobs.guard_commit(
                        connection, upload_id, request.state.principal
                    ),
                ),
            ),
        )

    @app.post("/api/v1/uploads/{identifier}/inspect")
    async def inspect_upload(identifier: str, request: Request):
        await run_in_threadpool(
            jobs.authorize_upload, identifier, request.state.principal
        )
        return success(
            request,
            await run_in_threadpool(
                core.inspect_upload, identifier, request.state.principal.subject
            ),
        )

    @app.delete("/api/v1/uploads/{identifier}")
    async def abort(identifier: str, request: Request):
        return success(
            request,
            await run_in_threadpool(
                core.abort_upload, identifier, request.state.principal.subject
            ),
        )

    from .content import install_content_routes

    install_content_routes(app, config, core, auth)
    from .compat import install_compat

    install_compat(app, config, core)
    if remote:
        endpoint = MCPControlRoute(remote, app)
        for path in ("/mcp", "/mcp/"):
            app.router.routes.append(
                Route(
                    path,
                    endpoint=endpoint,
                    methods=["GET", "HEAD", "POST", "DELETE", "OPTIONS"],
                )
            )
        oauth.install(app)
    web = Path(__file__).parent / "web"
    index_markup = (
        inline_marks((web / "index.html").read_text(encoding="utf-8"))
        .replace('<meta charset="utf-8">', '<meta charset="utf-8">' + metadata(config), 1)
        .replace("<!-- source-footer -->", source_footer(config.source_url, project_url=config.project_url), 1)
    )
    app.mount("/static", PackagedStaticFiles(directory=web), name="static")

    @app.get("/")
    async def index(request: Request):
        if request.state.revision_id:
            return await app.state.content(request, "")
        if request.state.legacy_host:
            raise PublishError("not_found", 404)
        return HTMLResponse(
            index_markup,
            headers={
                "Content-Security-Policy": f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self'; connect-src 'self'; frame-src 'self' {config.base_url.split('://')[0]}://*.{config.content_domain}:*; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            },
        )

    @app.get("/login")
    async def owner_login(request: Request):
        if request.state.revision_id:
            return await app.state.content(request, "login")
        if request.state.legacy_host:
            from .content import legacy_redirect

            return legacy_redirect(config, "login", request)
        return await index(request)

    async def owner_viewer(request, identifier, revision_id=None):
        if not request.state.principal or not request.state.principal.owner:
            raise PublishError("owner_required", 403)
        response = await index(request)
        attributes = f' data-publication-id="{html.escape(identifier, quote=True)}"'
        if revision_id:
            attributes += f' data-revision-id="{html.escape(revision_id, quote=True)}"'
        markup = inline_marks((web / "viewer.html").read_text(encoding="utf-8")).replace(
            '<body class="owner-viewer">', '<body class="owner-viewer"' + attributes + '>', 1
        )
        return HTMLResponse(markup, headers={
            "Content-Security-Policy": response.headers["Content-Security-Policy"],
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
        })

    app.state.owner_viewer = owner_viewer

    @app.get("/viewer/{identifier}")
    async def viewer(identifier: str, request: Request):
        if request.state.revision_id:
            return await app.state.content(request, "viewer/" + identifier)
        from .content import redirect

        revision = request.query_params.get("revision")
        if not request.state.principal or not request.state.principal.owner:
            destination = "/viewer/" + quote(identifier, safe="")
            if revision:
                destination += "?" + urlencode({"revision": revision})
            return redirect("/login?return_to=" + quote(destination, safe=""))
        value = await run_in_threadpool(core.get_publication, identifier)
        query = {"preview": "tools"}
        if revision:
            query["revision"] = revision
        return redirect(stable_url(config.base_url, value["link_name"]) + "?" + urlencode(query))

    @app.get("/upload/{identifier}")
    async def browser_upload_job(identifier: str, request: Request):
        if request.state.revision_id:
            return await app.state.content(request, "upload/" + identifier)
        if request.state.legacy_host:
            from .content import legacy_redirect

            return legacy_redirect(config, "upload/" + identifier, request)
        return await index(request)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST"])
    async def content_or_legacy(path: str, request: Request):
        if request.state.revision_id:
            return await app.state.content(request, path)
        from .content import legacy_redirect

        return legacy_redirect(config, path, request)

    return app
