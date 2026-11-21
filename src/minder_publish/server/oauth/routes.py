"""Control-origin OAuth routes using the maintained MCP SDK protocol handlers.

The provider protocol keeps storage separate from wire parsing. Local wrappers
enforce resource binding (not checked by SDK TokenHandler), bounded requests,
literal redirect matching, owner consent, and hashed confidential credentials.
"""

from __future__ import annotations

import base64
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl, AnyUrl
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response

from minder_publish.errors import PublishError

from ..navigation import callback_uri, install_navigation
from .pages import page as oauth_page, preferences
from .provider import HashedClientAuthenticator, OAuthProvider, valid_redirect
from .store import SCOPES, OAuthFailure, OAuthStore, digest


def problem(code, status=400):
    return JSONResponse(
        {"error": code},
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


class ControlOriginRoute:
    def __init__(self, app, config, content_application=None, methods=()):
        self.app, self.authority = app, urlsplit(config.base_url).netloc
        self.content_application = content_application
        self.methods = set(methods) | ({"HEAD"} if "GET" in methods else set())

    async def __call__(self, scope, receive, send):
        hosts = [
            value.decode("latin1").lower()
            for key, value in scope["headers"]
            if key.lower() == b"host"
        ]
        if hosts != [self.authority]:
            content = getattr(
                getattr(self.content_application, "state", None), "content", None
            )
            # Admission supplies this state only after validating the revision host.
            # Author GET/HEAD paths remain files; they never enter OAuth/CORS handlers.
            if (
                scope.get("state", {}).get("revision_id")
                and scope["method"] in ("GET", "HEAD")
                and content is not None
            ):
                request = Request(scope, receive)
                response = await content(request, scope["path"].lstrip("/"))
                return await response(scope, receive, send)
            return await problem("not_found", 404)(scope, receive, send)
        if scope["method"] not in self.methods:
            return await Response(
                status_code=405, headers={"Allow": ", ".join(sorted(self.methods))}
            )(scope, receive, send)
        return await self.app(scope, receive, send)


class OAuthService:
    def __init__(self, config, auth, store=None):
        if not config.testing and urlsplit(config.base_url).scheme != "https":
            raise PublishError("oauth_https_required", 503)
        self.config, self.auth = config, auth
        self.store = store or OAuthStore(config)
        self.provider = OAuthProvider(self.store)
        authenticator = HashedClientAuthenticator(self.provider)
        self.authorize_handler = AuthorizationHandler(self.provider)
        self.token_handler = TokenHandler(self.provider, authenticator)
        self.revoke_handler = RevocationHandler(self.provider, authenticator)
        self.registration_options = ClientRegistrationOptions(
            enabled=True, valid_scopes=sorted(SCOPES), default_scopes=sorted(SCOPES)
        )
        self.registration_handler = RegistrationHandler(
            self.provider, self.registration_options
        )
        self.messages = json.loads(
            (Path(__file__).parents[1] / "web" / "messages.json").read_text()
        )

    def initialize(self):
        self.store.initialize()
        return self

    async def verify_token(self, token):
        return await self.provider.verify_token(token)

    def _text(self, request, key):
        language = request.cookies.get("mp_lang", "en")
        return html.escape(self.messages.get(key, {}).get(language, key))

    def _owner(self, request):
        raw = request.cookies.get(self.config.owner_cookie)
        if not raw:
            return None, None
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            principal = self.auth.session(raw)
        if not principal or not principal.owner:
            return None, raw
        return principal, raw

    async def _prepared_request(self, request):
        if request.method not in ("POST", "PUT"):
            if len(request.url.query.encode()) > self.store.options.maximum_body_bytes:
                raise OAuthFailure("invalid_request", 413)
            return request
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > self.store.options.maximum_body_bytes:
                raise OAuthFailure("invalid_request", 413)
            body.extend(chunk)

        async def receive():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        return Request(request.scope, receive)

    async def _form(self, request):
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/x-www-form-urlencoded"
        ):
            raise OAuthFailure("invalid_request", 415)
        form = await request.form(
            max_files=0,
            max_fields=20,
            max_part_size=self.store.options.maximum_body_bytes,
        )
        if len(form.multi_items()) != len(form):
            raise OAuthFailure("invalid_request")
        return form

    async def _endpoint(self, request, handler):
        if (
            request.headers.get("host", "").lower()
            != urlsplit(self.config.base_url).netloc
        ):
            return problem("not_found", 404)
        try:
            request = await self._prepared_request(request)
            response = await handler(request)
        except OAuthFailure as error:
            response = problem(error.code, error.status)
        except PublishError as error:
            response = problem(
                "temporarily_unavailable" if error.status >= 429 else "access_denied",
                error.status,
            )
        for key, value in {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            # Native consent forms need their real Origin; callbacks still get no referrer.
            "Referrer-Policy": "same-origin"
            if isinstance(response, HTMLResponse)
            else "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; font-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        }.items():
            response.headers[key] = value
        return response

    async def _throttle(self, request, name, count=120, seconds=60):
        peer = request.client.host if request.client else "unknown"
        await run_in_threadpool(
            self.auth.throttle, f"oauth:{name}:{peer}", count, seconds
        )

    async def metadata(self, request):
        metadata = build_metadata(
            AnyHttpUrl(self.config.base_url),
            None,
            self.registration_options,
            RevocationOptions(enabled=True),
        ).model_dump(mode="json", exclude_none=True)
        metadata.update(
            issuer=self.config.base_url,
            authorization_endpoint=self.config.base_url + "/oauth/authorize",
            token_endpoint=self.config.base_url + "/oauth/token",
            registration_endpoint=self.config.base_url + "/oauth/register",
            revocation_endpoint=self.config.base_url + "/oauth/revoke",
            token_endpoint_auth_methods_supported=[
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            revocation_endpoint_auth_methods_supported=[
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            authorization_response_iss_parameter_supported=True,
        )
        return JSONResponse(metadata)

    async def resource_metadata(self, request):
        return JSONResponse(
            {
                "resource": self.store.resource,
                "authorization_servers": [self.config.base_url],
                "scopes_supported": sorted(SCOPES),
                "bearer_methods_supported": ["header"],
                "resource_name": "Minder Publish",
            }
        )

    async def register(self, request):
        await self._throttle(request, "register", 5, 3600)
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/json"
        ):
            raise OAuthFailure("invalid_client_metadata", 415)
        try:

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result

            metadata = json.loads(await request.body(), object_pairs_hook=unique)
            if not isinstance(metadata, dict) or not isinstance(
                metadata.get("redirect_uris"), list
            ):
                raise ValueError()
            for uri in metadata["redirect_uris"]:
                if (
                    not isinstance(uri, str)
                    or not valid_redirect(uri)
                    or str(AnyUrl(uri)) != uri
                ):
                    return problem("invalid_redirect_uri")
            if len(metadata["redirect_uris"]) != len(set(metadata["redirect_uris"])):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            return problem("invalid_client_metadata")
        return await self.registration_handler.handle(request)

    async def authorize(self, request):
        await self._throttle(request, "authorize", 30)
        params = (
            request.query_params
            if request.method == "GET"
            else await self._form(request)
        )
        if len(params.multi_items()) != len(params):
            raise OAuthFailure("invalid_request")
        if params.get("resource") != self.store.resource:
            raise OAuthFailure("invalid_target")
        if (
            params.get("code_challenge_method") != "S256"
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", ""))
            or len(params.get("state", "")) > 512
        ):
            raise OAuthFailure("invalid_request")
        client = await run_in_threadpool(self.store.client, params.get("client_id"))
        # Match literal registered strings before SDK URL parsing can normalize them.
        if not client or params.get("redirect_uri") not in client["metadata"].get(
            "redirect_uris", []
        ):
            raise OAuthFailure("invalid_request")
        response = await self.authorize_handler.handle(request)
        if response.headers.get("location"):
            location = response.headers["location"]
            if "error" in dict(parse_qsl(urlsplit(location).query)):
                returned = {
                    key: value
                    for key, value in parse_qsl(urlsplit(location).query)
                    if key in ("error", "error_description", "error_uri", "state")
                }
                response.headers["location"] = callback_uri(
                    params["redirect_uri"], **returned, iss=self.config.base_url
                )
        return response

    async def token(self, request):
        await self._throttle(request, "token")
        form = await self._form(request)
        request, form = await self._sdk_form(request, form)
        if form.get("resource") != self.store.resource:
            raise OAuthFailure("invalid_target")
        if form.get("grant_type") == "authorization_code":
            if not re.fullmatch(
                r"[A-Za-z0-9._~-]{43,128}", form.get("code_verifier", "")
            ):
                raise OAuthFailure("invalid_grant")
            client = await run_in_threadpool(self.store.client, form.get("client_id"))
            if client and form.get("redirect_uri") not in client["metadata"].get(
                "redirect_uris", []
            ):
                raise OAuthFailure("invalid_grant")
        return await self.token_handler.handle(request)

    async def _sdk_form(self, request, form, revocation=False):
        pairs = list(form.multi_items())
        if "client_id" not in form and request.headers.get(
            "authorization", ""
        ).startswith("Basic "):
            try:
                pairs.append(
                    ("client_id", unquote_basic_id(request.headers["authorization"]))
                )
            except (ValueError, UnicodeError):
                raise OAuthFailure("invalid_client", 401) from None
        # SDK RevocationRequest requires the nullable field even for public clients.
        if revocation and "client_secret" not in form:
            pairs.append(("client_secret", ""))
        if pairs != list(form.multi_items()):
            body = urlencode(pairs).encode()
            if len(body) > self.store.options.maximum_body_bytes:
                raise OAuthFailure("invalid_request", 413)

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request = Request(request.scope, receive)
            form = await self._form(request)
        return request, form

    async def revoke(self, request):
        await self._throttle(request, "revoke")
        request, _ = await self._sdk_form(
            request, await self._form(request), revocation=True
        )
        return await self.revoke_handler.handle(request)

    def _consent_page(self, request, pending, payload, raw, *, invalid_password=False):
        client = self.store.client(payload["client_id"])
        if not client:
            raise OAuthFailure()

        def t(key):
            return self._text(request, key)

        language = request.cookies.get("mp_lang", "en")
        language = language if language in ("en", "ru", "es", "fr") else "en"
        theme = request.cookies.get("mp_theme", "system")
        theme = theme if theme in ("system", "light", "dark") else "system"
        session = self.auth.session_info(raw)
        password = ""
        if not session["recent_auth"] or invalid_password:
            # The singleton owner belongs to the verified session, never client metadata.
            username = html.escape(self.auth.username(), quote=True)
            password = (
                '<div class="oauth-password">'
                '<div class="form-field"><label for="oauth-owner-username">'
                + t("Username")
                + f'</label><input id="oauth-owner-username" name="username" type="text" autocomplete="username" value="{username}" readonly></div>'
                '<div class="form-field"><label for="oauth-owner-password">'
                + t("Confirm owner password")
                + '</label><input id="oauth-owner-password" name="password" type="password" autocomplete="current-password" required></div></div>'
            )
        scopes = "".join(
            f"<li>{t('The application can create, change and delete your publications.' if scope == 'materials:manage' else 'The application can read your publications.')}</li>"
            for scope in payload["scopes"]
        )
        error = (
            '<p class="error" role="alert">' + t("Invalid username or password.") + '</p>'
            if invalid_password else ""
        )
        body = (
            f'<h1>{t("Authorize application")}</h1>'
            f'<h2 class="oauth-client">{html.escape(client["metadata"].get("client_name") or "OAuth application")}</h2>'
            f'<dl class="oauth-callback"><dt>{t("Redirect URI")}</dt><dd>{html.escape(payload["redirect_uri"])}</dd></dl>'
            f'<h3>{t("Requested permissions")}</h3><ul class="oauth-scopes">{scopes}</ul>'
            f'<p class="oauth-scope-note">{t("You are granting access to this instance only.")}</p>'
            f'{error}<form method="post" action="/oauth/consent" id="oauth-consent-form">'
            f'<input type="hidden" name="request" value="{html.escape(pending, quote=True)}">'
            f'<input type="hidden" name="csrf" value="{html.escape(self.auth.csrf(raw), quote=True)}">{password}'
            f'<div class="oauth-actions"><button class="primary" type="submit" name="decision" value="approve">{t("Approve")}</button>'
            f'<button type="submit" name="decision" value="deny" formnovalidate>{t("Deny")}</button></div></form>'
        )
        return HTMLResponse(
            oauth_page(
                t("Authorize application"), body, language=language, theme=theme,
                controls=preferences(t, language, theme),
            ),
            status_code=401 if invalid_password else 200,
        )

    async def consent(self, request):
        principal, raw = self._owner(request)
        if request.method == "GET":
            if len(request.query_params.multi_items()) != len(request.query_params):
                raise OAuthFailure("invalid_request")
            pending = request.query_params.get("request", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", pending):
                raise OAuthFailure("invalid_request")
            if not principal:
                target = "/oauth/consent?" + urlencode({"request": pending})
                return RedirectResponse(
                    self.config.base_url + "/login?" + urlencode({"return_to": target}),
                    status_code=303,
                )
            payload = await run_in_threadpool(
                self.store.pending_details, pending, digest(raw)
            )
            return await run_in_threadpool(
                self._consent_page, request, pending, payload, raw
            )
        if not principal:
            raise OAuthFailure("access_denied", 401)
        form = await self._form(request)
        if request.headers.get(
            "origin"
        ) != self.config.control_origin or not self.auth.validate_csrf(
            raw, form.get("csrf")
        ):
            raise OAuthFailure("access_denied", 403)
        if form.get("decision") not in ("approve", "deny"):
            raise OAuthFailure("invalid_request")
        if form["decision"] == "approve" and form.get("password"):
            try:
                await run_in_threadpool(
                    self.auth.reauthenticate,
                    raw,
                    form["password"],
                    request.client.host if request.client else "unknown",
                )
            except PublishError as error:
                if error.code != "invalid_credentials":
                    raise
                pending = form.get("request", "")
                payload = await run_in_threadpool(
                    self.store.pending_details, pending, digest(raw)
                )
                return await run_in_threadpool(
                    self._consent_page, request, pending, payload, raw,
                    invalid_password=True,
                )

        def authorize_owner(connection):
            try:
                self.auth.require_recent_auth(raw, connection=connection)
            except PublishError as error:
                # Denial reduces authority; an active session still must be verified.
                if (
                    form["decision"] != "deny"
                    or error.code != "reauthentication_required"
                ):
                    raise

        code, payload = await run_in_threadpool(
            self.store.consent,
            form.get("request", ""),
            digest(raw),
            form["decision"] == "approve",
            authorize_owner,
        )
        params = {"state": payload.get("state"), "iss": self.config.base_url}
        params.update({"code": code} if code else {"error": "access_denied"})
        return await run_in_threadpool(
            self.navigation.redirect,
            callback_uri(payload["redirect_uri"], **params),
            kind="oauth",
            realm="owner",
            subject_id=digest(raw),
            details={
                "client_id": payload["client_id"],
                "redirect_uri": payload["redirect_uri"],
                "params": params,
            },
        )

    def navigation_available(self, payload, connection):
        details = payload["details"]
        client = connection.execute(
            "SELECT metadata,expires_at FROM oauth_client WHERE id=?",
            (details["client_id"],),
        ).fetchone()
        if (
            not client
            or payload["realm"] != "owner"
            or payload["subject_kind"] != "owner"
        ):
            return False
        if (
            client["expires_at"] <= self.store.clock()
            and not connection.execute(
                "SELECT 1 FROM oauth_family WHERE client_id=? AND revoked_at IS NULL AND expires_at>?",
                (details["client_id"], self.store.clock()),
            ).fetchone()
        ):
            return False
        registered = json.loads(client["metadata"])
        uri, params = details["redirect_uri"], details["params"]
        if (
            uri not in registered.get("redirect_uris", [])
            or not valid_redirect(uri)
            or not isinstance(params, dict)
            or params.get("iss") != self.config.base_url
            or set(params) not in ({"state", "iss", "code"}, {"state", "iss", "error"})
        ):
            return False
        if "code" in params:
            code = connection.execute(
                "SELECT payload,expires_at,used_at FROM oauth_code WHERE id=? AND client_id=?",
                (digest(params["code"]), details["client_id"]),
            ).fetchone()
            if (
                not code
                or code["used_at"] is not None
                or code["expires_at"] <= self.store.clock()
            ):
                return False
            granted = json.loads(code["payload"])
            if (
                granted["redirect_uri"] != uri
                or granted.get("state") != params["state"]
            ):
                return False
        elif params["error"] != "access_denied":
            return False
        return payload["target"] == callback_uri(uri, **params)

    def install(self, app):
        self.navigation = install_navigation(app, self.config, self.auth)
        self.navigation.register("oauth", self.navigation_available)
        routes = [
            (
                "/.well-known/oauth-authorization-server",
                self.metadata,
                ["GET", "OPTIONS"],
                True,
            ),
            (
                "/.well-known/oauth-protected-resource/mcp",
                self.resource_metadata,
                ["GET", "OPTIONS"],
                True,
            ),
            (
                "/.well-known/oauth-protected-resource",
                self.resource_metadata,
                ["GET", "OPTIONS"],
                True,
            ),
            ("/oauth/register", self.register, ["POST", "OPTIONS"], True),
            ("/oauth/authorize", self.authorize, ["GET", "POST"], False),
            ("/oauth/token", self.token, ["POST", "OPTIONS"], True),
            ("/oauth/revoke", self.revoke, ["POST", "OPTIONS"], True),
            ("/oauth/consent", self.consent, ["GET", "POST"], False),
        ]
        for path, handler, methods, cors in reversed(routes):

            async def endpoint(request, selected=handler):
                return await self._endpoint(request, selected)

            selected_endpoint = request_response(endpoint)
            if cors:
                selected_endpoint = CORSMiddleware(
                    selected_endpoint,
                    allow_origins=["*"],
                    allow_methods=methods,
                    allow_headers=[
                        "Authorization",
                        "Content-Type",
                        "MCP-Protocol-Version",
                    ],
                )
            app.router.routes.insert(
                0,
                Route(
                    path,
                    endpoint=ControlOriginRoute(
                        selected_endpoint,
                        self.config,
                        app,
                        methods,
                    ),
                    methods=["GET", "HEAD", "POST", "OPTIONS"],
                ),
            )
        return self


def unquote_basic_id(header):
    from urllib.parse import unquote

    decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    return unquote(decoded.split(":", 1)[0])
