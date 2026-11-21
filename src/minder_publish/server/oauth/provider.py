"""MCP SDK OAuth provider with durable one-use grants and hashed credentials."""

from __future__ import annotations

import base64
import hmac
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from mcp.server.auth.middleware.client_auth import AuthenticationError
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.concurrency import run_in_threadpool

from .store import SCOPES, OAuthFailure, digest


def valid_redirect(value):
    parsed = urlsplit(value)
    return bool(
        len(value) <= 2048
        and parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and not any(ord(c) < 33 for c in value)
        and not (
            {key for key, _ in parse_qsl(parsed.query)}
            & {"code", "state", "error", "error_description", "error_uri", "iss"}
        )
    )


class OAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self, store):
        self.store = store

    async def get_client(self, client_id):
        row = await run_in_threadpool(self.store.client, client_id)
        return (
            OAuthClientInformationFull.model_validate(row["metadata"]) if row else None
        )

    async def register_client(self, client_info):
        redirects = client_info.redirect_uris or []
        if not 1 <= len(redirects) <= 8 or any(
            not valid_redirect(str(uri)) for uri in redirects
        ):
            raise RegistrationError(
                "invalid_redirect_uri",
                "Register exact HTTPS callback URLs without fragments.",
            )
        if client_info.client_name and len(client_info.client_name) > 120:
            raise RegistrationError(
                "invalid_client_metadata", "Client name exceeds the limit."
            )
        if client_info.token_endpoint_auth_method not in (
            "none",
            "client_secret_post",
            "client_secret_basic",
        ):
            raise RegistrationError(
                "invalid_client_metadata", "Unsupported client authentication method."
            )
        if set(client_info.grant_types) - {
            "authorization_code",
            "refresh_token",
        } or client_info.response_types != ["code"]:
            raise RegistrationError(
                "invalid_client_metadata",
                "Only the authorization code and refresh grants are supported.",
            )
        if not client_info.scope or not set(client_info.scope.split()) <= SCOPES:
            raise RegistrationError(
                "invalid_client_metadata", "Unsupported publication scope."
            )
        metadata = client_info.model_dump(mode="json", exclude_none=True)
        metadata.pop("client_secret", None)
        await run_in_threadpool(
            self.store.register,
            client_info.client_id,
            metadata,
            client_info.client_secret,
        )

    async def authorize(self, client, params):
        if params.resource != self.store.resource:
            raise AuthorizeError(
                "invalid_target", "Select this instance's MCP resource."
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError(
                "invalid_request", "A valid S256 challenge is required."
            )
        scopes = params.scopes or ["materials:read"]
        if not set(scopes) <= SCOPES:
            raise AuthorizeError("invalid_scope", "Unsupported publication scope.")
        if "materials:manage" in scopes:
            scopes = [*scopes, "materials:read"]
        payload = params.model_dump(mode="json")
        payload["scopes"] = sorted(set(scopes))
        try:
            pending = await run_in_threadpool(
                self.store.pending, client.client_id, payload
            )
        except OAuthFailure:
            raise AuthorizeError(
                "temporarily_unavailable", "Try again later."
            ) from None
        return self.store.config.base_url + "/oauth/consent?request=" + pending

    async def load_authorization_code(self, client, authorization_code):
        if not 32 <= len(authorization_code) <= 512:
            return None
        row = await run_in_threadpool(
            self.store.code, authorization_code, client.client_id
        )
        if not row:
            return None
        payload = row["payload"]
        return AuthorizationCode(
            code=authorization_code,
            client_id=client.client_id,
            scopes=payload["scopes"],
            expires_at=row["expires_at"],
            code_challenge=payload["code_challenge"],
            redirect_uri=payload["redirect_uri"],
            redirect_uri_provided_explicitly=payload[
                "redirect_uri_provided_explicitly"
            ],
            resource=payload["resource"],
            subject="owner",
        )

    async def exchange_authorization_code(self, client, authorization_code):
        try:
            return OAuthToken(
                **await run_in_threadpool(
                    self.store.exchange_code, authorization_code.code, client.client_id
                )
            )
        except OAuthFailure as error:
            if error.status == 429:
                raise
            raise TokenError(
                error.code, "The authorization grant is unavailable."
            ) from None

    async def load_refresh_token(self, client, refresh_token):
        row = await run_in_threadpool(
            self.store.token,
            refresh_token,
            kind="refresh",
            client_id=client.client_id,
            include_used=True,
        )
        return (
            RefreshToken(
                token=refresh_token,
                client_id=client.client_id,
                scopes=row["scopes"],
                expires_at=row["expires_at"],
                resource=row["resource"],
                subject=row["subject"],
            )
            if row
            else None
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        try:
            return OAuthToken(
                **await run_in_threadpool(
                    self.store.refresh, refresh_token.token, client.client_id, scopes
                )
            )
        except OAuthFailure as error:
            if error.status == 429:
                raise
            raise TokenError(error.code, "The refresh grant is unavailable.") from None

    async def load_access_token(self, token):
        row = await run_in_threadpool(self.store.token, token, kind="access")
        return (
            AccessToken(
                token=token,
                client_id=row["client_id"],
                scopes=row["scopes"],
                expires_at=row["expires_at"],
                resource=row["resource"],
                subject=row["subject"],
                claims={"iss": self.store.config.base_url},
            )
            if row
            else None
        )

    async def verify_token(self, token):
        return await self.load_access_token(token)

    async def revoke_token(self, token):
        await run_in_threadpool(self.store.revoke_token, token.token, token.client_id)


class HashedClientAuthenticator:
    """The SDK authenticator requires plaintext secrets; random secrets stay hashed here."""

    def __init__(self, provider):
        self.provider = provider

    async def authenticate_request(self, request):
        form = await request.form()
        basic_id = secret = None
        header = request.headers.get("authorization", "")
        if header:
            try:
                if not header.startswith("Basic "):
                    raise ValueError()
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
                basic_id, secret = (unquote(part) for part in decoded.split(":", 1))
            except (ValueError, UnicodeError):
                raise AuthenticationError("Invalid client credentials") from None
        client_id = form.get("client_id") or basic_id
        row = await run_in_threadpool(self.provider.store.client, client_id)
        if not row or (basic_id and client_id != basic_id):
            raise AuthenticationError("Invalid client credentials")
        metadata = row["metadata"]
        method = metadata["token_endpoint_auth_method"]
        if method == "none":
            if header or form.get("client_secret"):
                raise AuthenticationError("Unexpected client credentials")
        else:
            if method == "client_secret_post":
                if header:
                    raise AuthenticationError(
                        "Use the registered authentication method"
                    )
                secret = form.get("client_secret")
            elif not basic_id or form.get("client_secret"):
                raise AuthenticationError("Use the registered authentication method")
            if (
                not isinstance(secret, str)
                or len(secret) > 512
                or not row["secret_hash"]
                or not hmac.compare_digest(digest(secret), row["secret_hash"])
            ):
                raise AuthenticationError("Invalid client credentials")
        return OAuthClientInformationFull.model_validate(metadata)
