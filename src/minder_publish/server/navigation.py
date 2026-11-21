"""One-use browser-bound POST/Redirect/GET transitions across isolated origins."""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from minder_publish.errors import PublishError

from .http import problem

PATH = "/__mp/continue"
PUBLICATION_NAVIGATION_SCRIPT = """'use strict';
const link = document.getElementById('mp-continue');
const destination = new URL(link.href);
destination.hash = location.hash;
link.href = destination.href;
location.replace(destination.href);
"""
PUBLICATION_SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(PUBLICATION_NAVIGATION_SCRIPT.encode()).digest()
).decode()
ENVELOPE_FIELDS = frozenset(
    {"version", "purpose", "audience", "kind", "realm", "subject_kind", "subject_id"}
)
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


def callback_uri(base, **params):
    """Preserve the literal registered callback, including its original query."""
    separator = "&" if "?" in base else "?"
    if base.endswith("?"):
        separator = ""
    return (
        base
        + separator
        + urlencode({key: value for key, value in params.items() if value is not None})
    )


class Navigation:
    def __init__(self, config, auth):
        self.config, self.auth = config, auth
        self.validators = {}
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"minder-publish:browser-navigation:v1\x00"
            + config.control_origin.encode(),
        ).derive(config.secret.encode())
        self.cipher = AESGCM(key)
        self.messages = json.loads(
            (Path(__file__).parent / "web/messages.json").read_text(encoding="utf-8")
        )

    def register(self, kind, validator):
        if kind in self.validators:
            raise RuntimeError("Navigation purpose is already registered")
        self.validators[kind] = validator

    @staticmethod
    def associated_data(payload):
        return json.dumps(
            {key: payload[key] for key in ENVELOPE_FIELDS},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()

    def seal(self, payload):
        envelope = {key: payload[key] for key in ENVELOPE_FIELDS}
        plaintext = json.dumps(
            {key: payload[key] for key in ("target", "details")}, separators=(",", ":")
        ).encode()
        if len(plaintext) > 32740:
            raise PublishError("invalid_navigation", 403)
        nonce = secrets.token_bytes(12)
        encrypted = self.cipher.encrypt(
            nonce, plaintext, self.associated_data(envelope)
        )
        return {
            **envelope,
            "sealed": base64.urlsafe_b64encode(nonce + encrypted).decode(),
        }

    def open(self, payload):
        if (
            set(payload) != ENVELOPE_FIELDS | {"sealed"}
            or type(payload.get("version")) is not int
            or payload["version"] != 1
            or not isinstance(payload.get("sealed"), str)
            or not 40 <= len(payload["sealed"]) <= 43692
            or payload.get("audience") != self.config.control_origin
            or any(
                not isinstance(payload.get(key), str) or len(payload[key]) > 512
                for key in ENVELOPE_FIELDS - {"version"}
            )
        ):
            return None
        try:
            encrypted = base64.b64decode(
                payload["sealed"], altchars=b"-_", validate=True
            )
            if not 28 <= len(encrypted) <= 32768:
                return None
            plaintext = self.cipher.decrypt(
                encrypted[:12], encrypted[12:], self.associated_data(payload)
            )
            decoded = json.loads(plaintext)
            if not isinstance(decoded, dict) or set(decoded) != {"target", "details"}:
                return None
            return {**payload, **decoded}
        except (InvalidTag, ValueError, TypeError, KeyError, binascii.Error):
            return None

    def valid_plaintext(self, payload, connection):
        target = payload.get("target")
        if not isinstance(target, str) or len(target.encode("utf-8")) > 16384:
            return False
        try:
            parsed = urlsplit(target)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or any(ord(character) < 33 or character == "\\" for character in target)
                or payload.get("audience") != self.config.control_origin
            ):
                return False
            validator = self.validators.get(payload.get("kind"))
            return bool(validator and validator(payload, connection))
        except (KeyError, TypeError, ValueError, PublishError):
            return False

    def valid(self, payload, connection):
        opened = self.open(payload)
        return (
            opened
            if opened is not None and self.valid_plaintext(opened, connection)
            else None
        )

    def redirect(self, target, *, kind, realm, subject_id, details):
        if realm not in ("owner", "normal", "probe"):
            raise PublishError("invalid_navigation", 403)
        payload = {
            "version": 1,
            "purpose": "navigation",
            "audience": self.config.control_origin,
            "kind": kind,
            "realm": realm,
            "subject_kind": "owner" if realm == "owner" else "guest",
            "subject_id": subject_id,
            "target": target,
            "details": details,
        }
        with self.auth.connection() as connection:
            if not self.valid_plaintext(payload, connection):
                raise PublishError("invalid_navigation", 403)
        ticket = self.auth.intent(self.seal(payload))
        return RedirectResponse(
            self.config.base_url + PATH + "?" + urlencode({"ticket": ticket}),
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def continue_navigation(self, request):
        try:
            if (
                request.headers.get("host", "").lower()
                != urlsplit(self.config.base_url).netloc
            ):
                raise PublishError("not_found", 404)
            if request.method != "GET":
                return Response(status_code=405, headers={**HEADERS, "Allow": "GET"})
            if (
                list(request.query_params) != ["ticket"]
                or len(request.query_params.multi_items()) != 1
            ):
                raise PublishError("invalid_navigation", 403)
            cookies = {
                "owner": request.cookies.get(self.config.owner_cookie),
                "normal": request.cookies.get(self.config.viewer_cookie),
                "probe": request.cookies.get(self.config.probe_viewer_cookie),
            }
            payload = await run_in_threadpool(
                self.auth.consume_navigation,
                request.query_params["ticket"],
                cookies,
                self.valid,
            )
            language = request.cookies.get("mp_lang", "en")
            language = language if language in ("en", "ru", "es", "fr") else "en"
            theme = request.cookies.get("mp_theme", "system")
            theme = theme if theme in ("system", "light", "dark") else "system"
            label = html.escape(self.messages["Continue"][language])
            target = html.escape(payload["target"], quote=True)
            if payload["kind"] == "publication":
                from .previews import opening_page

                return opening_page(
                    self.config, None, payload["target"], language=language, theme=theme,
                    loading_label=self.messages["Loading…"][language],
                    open_label=self.messages["Open"][language],
                )
            headers = dict(HEADERS)
            headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
            refresh = f'<meta http-equiv="refresh" content="0;url={target}">'
            from .oauth.pages import page as oauth_page

            title = html.escape(self.messages["Returning to the application…"][language])
            explanation = html.escape(self.messages["If you are not redirected, continue below."][language])
            body = (
                f'<h1 role="status">{title}</h1><p>{explanation}</p>'
                f'<a id="mp-continue" class="primary oauth-continue" href="{target}" rel="noreferrer">{label}</a>'
            )
            return HTMLResponse(
                oauth_page(title, body, language=language, theme=theme, refresh=refresh, continuation=True),
                headers=headers,
            )
        except PublishError as error:
            response = problem(error)
            response.headers.update(HEADERS)
            return response


def install_navigation(app, config, auth):
    existing = getattr(app.state, "navigation", None)
    if existing is not None:
        if existing.config is not config or existing.auth is not auth:
            raise RuntimeError("Navigation instance configuration differs")
        return existing
    navigation = Navigation(config, auth)
    app.state.navigation = navigation
    app.router.routes.insert(
        0, Route(PATH, navigation.continue_navigation, methods=["GET"])
    )
    return navigation
