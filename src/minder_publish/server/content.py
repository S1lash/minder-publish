"""Revision origins, browser-bound grants and authorized byte serving."""

from __future__ import annotations

import asyncio
import html
import json
import re
import secrets
import time
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from fastapi import Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from minder_publish.errors import PublishError

from .auth import digest
from .brand import LANGUAGE_ICON, LOCKUP, SYSTEM_CSS, THEME_CSS, THEME_ICON
from .core import guest_available
from .history import pinned_revision, revision_grant_available, version_sequence, version_url
from .navigation import install_navigation
from .previews import cover_url, is_preview_request, metadata, opening_page, preview_page, public_cover
from .publication_links import stable_url
from .source_footer import source_footer
from .storage import normalize_path
from .statistics import Statistics


class LeasedFile(FileResponse):
    def __init__(self, core, revision_id, path, **kwargs):
        self.core, self.revision_id = core, revision_id
        super().__init__(path, **kwargs)

    async def __call__(self, scope, receive, send):
        lease = await run_in_threadpool(self.core.acquire_lease, self.revision_id)
        refreshed = time.monotonic()
        try:
            limits = (await run_in_threadpool(self.core.policy_settings))["limits"]

            async def progress(message):
                nonlocal refreshed
                if time.monotonic() - refreshed > 20:
                    await run_in_threadpool(self.core.renew_lease, lease)
                    refreshed = time.monotonic()
                async with asyncio.timeout(limits["export_idle_seconds"]):
                    await send(message)

            async with asyncio.timeout(limits["export_total_seconds"]):
                await super().__call__(scope, receive, progress)
        finally:
            await asyncio.shield(run_in_threadpool(self.core.release_lease, lease))


def redirect(url):
    return RedirectResponse(
        url,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def legacy_redirect(config, path, request):
    parts = path.split("/", 1)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", parts[0]):
        raise PublishError("not_found", 404)
    url = f"{config.base_url}/p/{quote(parts[0], safe='')}"
    if len(parts) > 1:
        url += "/" + quote(parts[1], safe="/")
    return redirect(
        url + stable_query(request.url.query, request.query_params.get("guest") == "1")
    )


def stable_query(query, guest=False):
    pairs = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key != "guest"
    ]
    if guest:
        pairs.append(("guest", "1"))
    encoded = urlencode(pairs)
    return "?" + encoded if encoded else ""


def document_navigation(request):
    """Select a control-gate redirect, never authority to serve author bytes."""
    if request.method not in ("GET", "HEAD") or any(
        header in request.headers
        for header in (
            "range", "if-range", "if-match", "if-none-match",
            "if-modified-since", "if-unmodified-since",
        )
    ):
        return False
    mode = request.headers.get("sec-fetch-mode", "").lower()
    destination = request.headers.get("sec-fetch-dest", "").lower()
    if mode or destination:
        return mode in ("", "navigate") and destination in (
            "", "document", "iframe", "frame"
        )
    if is_preview_request(request):
        return True
    # Older browsers and HTML clients may omit Fetch Metadata. A fetch that
    # identifies its non-navigation mode must never use this fallback.
    for accepted in request.headers.get("accept", "").split(","):
        media, *parameters = accepted.lower().strip().split(";")
        if media.strip() not in ("text/html", "application/xhtml+xml"):
            continue
        quality = "1"
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if name == "q" and separator:
                quality = value
        try:
            if 0 < float(quality) <= 1:
                return True
        except ValueError:
            continue
    return False


def opening_document(request, publication, path, *, verified_form=False):
    """Select document representation without requiring modern browser headers."""
    if request.method != "GET" and not (verified_form and request.method == "POST"):
        return False
    if any(name in request.headers for name in (
        "range", "if-range", "if-match", "if-none-match",
        "if-modified-since", "if-unmodified-since",
    )):
        return False
    if request.headers.get("sec-fetch-dest", "").lower() not in ("", "document", "iframe", "frame"):
        return False
    if request.headers.get("sec-fetch-mode", "").lower() not in ("", "navigate"):
        return False
    if publication["kind"] == "site" and path and path != publication["entry_path"]:
        return path.endswith("/") or PurePosixPath(path).suffix.lower() in ("", ".html", ".htm")
    return True


def countable_opening(request, publication, path, *, guest):
    return (
        not guest and opening_document(request, publication, path, verified_form=True)
        and not any("prefetch" in request.headers.get(name, "").lower() for name in ("purpose", "sec-purpose"))
    )


def install_content_routes(app, config, core, auth):
    statistics = Statistics(core)
    navigation = install_navigation(app, config, auth)
    web = Path(__file__).parent / "web"
    messages = json.loads((web / "messages.json").read_text(encoding="utf-8"))

    def preferences(request):
        language = request.cookies.get("mp_lang", "en")
        theme = request.cookies.get("mp_theme", "system")
        return (
            language if language in ("en", "ru", "es", "fr") else "en",
            theme if theme in ("system", "light", "dark") else "system",
        )

    def text(request, key):
        language, _ = preferences(request)
        return html.escape(messages.get(key, {}).get(language, key))

    def page(request, key, status=200, body="", *, publication=None, canonical=None, footer="", binary_theme=False):
        language, theme = preferences(request)

        def translated(key):
            return text(request, key)

        def icon(paths):
            return (
                '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
                f'aria-hidden="true">{paths}</svg>'
            )

        icons = {
            "language": LANGUAGE_ICON,
            "theme": THEME_ICON,
            "system": icon('<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>'),
            "light": icon('<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>'),
            "dark": icon('<path d="M20.5 14A9 9 0 0 1 10 3.5 9 9 0 1 0 20.5 14Z"/>'),
        }
        return_to = html.escape(
            request.url.path
            + ("?guest=1" if request.query_params.get("guest") == "1" else ""),
            quote=True,
        )

        def preference_control(name, label, selected, options, other_name, other_value):
            buttons = "".join(
                f'<button type="submit" name="{name}" value="{value}"'
                + (' aria-current="true"' if selected == value else "")
                + f'>{option_icon}<span>{option_label}</span>'
                + ('<span class="chosen" aria-hidden="true">✓</span>' if selected == value else "")
                + '</button>'
                for value, option_label, option_icon in options
            )
            summary_icon = icons["language"] if name == "language" else icons["theme"]
            return (
                '<details class="preference" name="guest-preferences">'
                f'<summary aria-label="{label}">{summary_icon}'
                f'<span class="preference-tooltip" aria-hidden="true">{label}</span></summary>'
                f'<div class="preference-panel"><p>{label}</p>'
                '<form method="post" action="/p/_preferences">'
                f'<input type="hidden" name="return_to" value="{return_to}">'
                f'<input type="hidden" name="{other_name}" value="{other_value}">'
                f'{buttons}</form></div></details>'
            )

        controls = preference_control(
            "language", translated("Language"), language,
            [(code, label, "") for code, label in (
                ("en", "English"), ("ru", "Русский"), ("es", "Español"), ("fr", "Français")
            )],
            "theme", theme,
        )
        if binary_theme:
            theme_buttons = "".join(
                f'<button class="gate-theme-{value}" type="submit" name="theme" value="{value}" '
                f'aria-label="{translated("Theme")}: {translated("Change theme")} — {translated(label)}">'
                f'{icons["theme"]}</button>'
                for value, label in (("light", "Light"), ("dark", "Dark"))
            )
            controls += (
                '<form class="gate-theme-toggle" method="post" action="/p/_preferences">'
                f'<input type="hidden" name="return_to" value="{return_to}">'
                f'<input type="hidden" name="language" value="{language}">'
                f'{theme_buttons}</form>'
            )
        else:
            controls += preference_control(
                "theme", translated("Theme"), theme,
                [(value, translated(label), icons[value]) for value, label in (
                    ("system", "System"), ("light", "Light"), ("dark", "Dark")
                )],
                "language", language,
            )
        enhance = not request.state.revision_id and not request.state.legacy_host
        enhancement = (
            '<script type="module" src="/static/gate-preferences.js"></script>'
            if enhance
            else ""
        )
        script_policy = "script-src 'self'; " if enhance else ""
        return HTMLResponse(
            f"""<!doctype html>
<html lang="{language}" data-theme="{theme}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{metadata(config, publication, gate=True, canonical=canonical)}
<meta name="color-scheme" content="{theme if theme != "system" else "light dark"}">
<title>{translated(key)} · minder publish.</title>
<style>
{THEME_CSS}{SYSTEM_CSS}
body{{padding-block:76px 28px}}
.password-hint{{color:var(--muted);overflow-wrap:anywhere}}
.gate-form{{display:grid;gap:18px}}
.gate-field{{display:grid;gap:8px}}
.gate-form label{{font-size:13px;color:var(--muted)}}
.gate-form input:not([type=hidden]){{width:100%;min-width:0;min-height:44px;padding:10px 12px;border:1px solid var(--field-line);border-radius:var(--radius-control);background:var(--surface);color:var(--ink);font:inherit}}
.gate-form input[aria-invalid="true"]{{border-color:var(--danger)}}
.gate-error{{margin:0 0 4px;padding:8px 12px;border-radius:var(--radius-control);background:var(--dangerBg);color:var(--danger);font-size:14px;overflow-wrap:anywhere}}
.has-attribution{{grid-template-rows:1fr auto;row-gap:16px;padding-block-end:max(8px,env(safe-area-inset-bottom))}}
.source-footer{{text-align:center}}.source-footer a{{display:inline-flex;justify-content:center;align-items:center;gap:7px;min-height:44px;color:var(--muted);font-size:13px;text-decoration:none}}.source-footer .project-attribution{{text-decoration:underline;text-underline-offset:3px}}.source-footer a:hover{{color:var(--ink);text-decoration:underline}}.source-footer svg{{width:15px;height:15px;flex:none}}
.preferences{{position:fixed;top:max(16px,env(safe-area-inset-top));right:max(16px,env(safe-area-inset-right));display:flex;gap:4px;z-index:1}}
.gate-theme-toggle button{{display:none;width:44px;height:44px;padding:0;border:0;border-radius:var(--radius-control);background:transparent;color:var(--muted);cursor:pointer;place-items:center}}
.gate-theme-toggle button:hover{{background:var(--surface2);color:var(--ink)}}.gate-theme-toggle svg{{width:21px;height:21px}}
:root[data-theme="light"] .gate-theme-dark,:root[data-theme="dark"] .gate-theme-light{{display:grid}}
@media(prefers-color-scheme:light){{:root[data-theme="system"] .gate-theme-dark{{display:grid}}}}
@media(prefers-color-scheme:dark){{:root[data-theme="system"] .gate-theme-light{{display:grid}}}}
.preference{{position:relative}}.preference summary{{width:44px;height:44px;display:grid;place-items:center;list-style:none;border-radius:var(--radius-control);cursor:pointer;color:var(--muted)}}.preference summary::-webkit-details-marker{{display:none}}.preference summary:hover,.preference[open] summary{{background:var(--surface2);color:var(--ink)}}.preference svg{{width:21px;height:21px;flex:none}}
.preference-tooltip{{position:absolute;right:0;top:50px;white-space:nowrap;font-size:12px;padding:5px 9px;border-radius:var(--radius-control);background:var(--ink);color:var(--canvas);pointer-events:none;opacity:0}}
.preference:not([open]) summary:hover .preference-tooltip,.preference:not([open]) summary:focus-visible .preference-tooltip{{opacity:1}}
.preference-panel{{position:absolute;right:0;top:52px;width:190px;max-width:calc(100vw - 32px);padding:8px;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius-panel);box-shadow:var(--floating-shadow)}}.preference-panel p{{margin:4px 10px 8px;font-size:12px;font-weight:600;color:var(--muted)}}
.preference-panel button{{display:flex;align-items:center;gap:10px;width:100%;min-height:44px;padding:10px;border:0;border-radius:var(--radius-control);background:transparent;color:var(--ink);font:14px/1.4 system-ui;text-align:left;cursor:pointer}}.preference-panel button:hover,.preference-panel button[aria-current=true]{{background:var(--surface2)}}.preference-panel svg{{width:18px;height:18px}}.chosen{{margin-left:auto;color:var(--accent);font-weight:700}}
@media(max-width:360px){{.preferences{{right:12px;top:12px}}}}
</style>{enhancement}</head><body{' class="has-attribution"' if footer else ''}>
<div class="preferences">{controls}</div>
<main class="system-card">{LOCKUP}<h1>{translated(key)}</h1>{body}</main>
{footer}
</body></html>""",
            status_code=status,
            headers={
                # no-referrer serializes native form POST Origin as null in browsers.
                # Keep strict Origin checks while withholding cross-origin referrers.
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": f"default-src 'none'; {script_policy}img-src {config.base_url}; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'self' {config.base_url}",
            },
        )

    def navigation_available(payload, connection):
        details = payload["details"]
        row = connection.execute(
            "SELECT payload FROM auth_intents WHERE token_hash=? AND expires_at>? AND consumed=0",
            (digest(details["intent"]), time.time()),
        ).fetchone()
        if not row:
            return False
        opening = json.loads(row["payload"])
        guest = opening.get("view_mode") == "guest"
        expected_realm = (
            "owner"
            if opening.get("subject_kind") == "owner"
            else "probe"
            if guest
            else "normal"
        )
        if (
            opening.get("purpose", "handoff") != "handoff"
            or opening["subject_id"] != payload["subject_id"]
            or opening["subject_kind"] != payload["subject_kind"]
            or payload["realm"] != expected_realm
            or (guest and opening["subject_kind"] == "owner")
        ):
            return False
        pub = core._publication(connection, opening["publication_id"])
        revision = connection.execute(
            "SELECT * FROM revision WHERE id=?",
            (opening["revision_id"],),
        ).fetchone()
        selected_revision = None if revision is None else dict(
            json.loads(revision["data"]), state=revision["state"],
            recovery_until=revision["recovery_until"], drain_until=revision["drain_until"],
        )
        return bool(
            selected_revision
            and revision_grant_available(pub, selected_revision, opening, new=True)
            and pub["access_version"] == opening["access_version"]
            and pub["content_state"] == "present"
            and not pub.get("deletion_state")
            and (opening["subject_kind"] == "owner" or guest_available(pub))
            and auth._subject_active(
                connection, opening["subject_kind"], opening["subject_id"], pub
            )
            and payload["target"]
            == config.content_origin(opening["revision_id"], guest=guest)
            + "/__mp/start?"
            + urlencode({"intent": details["intent"]})
        )

    navigation.register("publication", navigation_available)

    def check_payload(payload, new=False, mode=None, connection=None):
        if connection is None:
            with core.repository.connect() as connection:
                return check_payload(payload, new=new, mode=mode, connection=connection)
        try:
            selected = payload.get("view_mode", "normal")
            if (
                selected not in ("normal", "guest")
                or (mode is not None and mode != selected)
                or (selected == "guest" and payload["subject_kind"] == "owner")
            ):
                return False
            pub = core._publication(connection, payload["publication_id"])
            revision = core._revision(connection, payload["revision_id"], manifest=False)
            if (
                pub["access_version"] != payload["access_version"]
                or pub["content_state"] != "present"
                or pub.get("deletion_state")
            ):
                return False
            if not revision_grant_available(pub, revision, payload, new=new):
                return False
            if payload["subject_kind"] != "owner" and not guest_available(pub):
                return False
            return auth._subject_active(
                connection, payload["subject_kind"], payload["subject_id"], pub
            )
        except (PublishError, KeyError):
            return False

    async def admit_open(request, kind="open"):
        await run_in_threadpool(
            auth.throttle,
            f"{kind}:{request.client.host if request.client else 'unknown'}",
            policy=core.opening_budget,
            namespace="publication",
        )

    async def stable(request, identifier, path="", history_revision=None, pinned_sequence=None):
        await admit_open(request)
        if path:
            path = path.rstrip("/") + (
                "/" if path.endswith("/") and path.rstrip("/") else ""
            )
            if path:
                normalize_path(path.rstrip("/"), config.limits)
        selected_revision = None
        canonical = None
        try:
            if pinned_sequence is None and history_revision is None:
                pub = await run_in_threadpool(core.get_linked_publication, identifier)
                canonical = stable_url(config.base_url, identifier)
            else:
                pub = await run_in_threadpool(core.get_publication, identifier)
            identifier = pub["id"]
            if pinned_sequence is not None:
                selected_revision = await run_in_threadpool(pinned_revision, core, identifier, pinned_sequence)
                canonical = version_url(config, identifier, pinned_sequence)
        except PublishError as error:
            if pinned_sequence is not None and error.status in (404, 410):
                return page(request, "This publication is unavailable.", error.status)
            if error.status == 404 and history_revision is None and document_navigation(request):
                return page(request, "This publication is unavailable.", 404)
            raise
        if is_preview_request(request):
            if selected_revision is not None and not selected_revision.get("shared", False):
                return page(request, "This publication is unavailable.", 404)
            cover = cover_url(config, core, pub, selected_revision, pinned_sequence)
            return preview_page(config, pub, canonical=canonical, cover=cover)
        if pub["content_state"] != "present" or pub.get("deletion_state"):
            return page(request, "Files deleted", 410)
        guest = request.query_params.get("guest") == "1"
        principal = request.state.principal
        owner = bool(principal and principal.owner and not guest)
        if selected_revision is not None and not owner and not selected_revision.get("shared", False):
            return page(request, "This publication is unavailable.", 404)
        if (
            owner and history_revision is None and not path
            and set(request.query_params) <= {"preview", "revision"}
            and request.query_params.get("preview") != "1"
            and (request.cookies.get("mp_owner_view") != "clean"
                 or request.query_params.get("preview") == "tools")
            and request.method == "GET" and document_navigation(request)
            and request.headers.get("sec-fetch-dest", "document") == "document"
            and not any("prefetch" in request.headers.get(name, "").lower() for name in ("purpose", "sec-purpose"))
        ):
            return await app.state.owner_viewer(
                request, identifier, revision_id=selected_revision["id"] if selected_revision is not None else None
            )
        revision_id = pub["current_revision"]
        owner_history = history_revision is not None or (owner and selected_revision is not None)
        if history_revision is not None:
            if not owner:
                raise PublishError("owner_required", 403)
            selected_revision = await run_in_threadpool(core.history_revision, identifier, history_revision)
            revision_id = history_revision
        elif selected_revision is not None:
            revision_id = selected_revision["id"]
        allow_owner = owner and (owner_history or (
            pub["access"] != "password"
            or (await run_in_threadpool(core.policy_settings))["defaults"]["owner_auto_open"]
        ))
        if allow_owner:
            kind, subject = "owner", principal.id
        else:
            if not guest_available(pub):
                return page(request, "This publication is unavailable.", 410)
            if pub["access"] == "private":
                return_to = urlsplit(canonical or stable_url(config.base_url, pub["link_name"])).path
                login_url = config.base_url + "/login?" + urlencode({"return_to": return_to})
                return page(
                    request,
                    "Owner sign-in",
                    200 if request.method in ("GET", "HEAD") else 403,
                    f"""<a href="{html.escape(login_url, quote=True)}">{text(request, "Sign in")}</a>""",
                    publication=pub,
                    canonical=canonical,
                )
            if pub["access"] == "link":
                kind, subject = "anonymous", ""
            else:
                viewer_cookie = (
                    config.probe_viewer_cookie if guest else config.viewer_cookie
                )
                raw, viewer = await run_in_threadpool(
                    auth.viewer,
                    None
                    if guest and request.method in ("GET", "HEAD")
                    else request.cookies.get(viewer_cookie),
                )
                if not await run_in_threadpool(auth.unlocked, viewer, pub):
                    if request.method == "POST":
                        if request.headers.get("origin") != config.control_origin:
                            raise PublishError("csrf_rejected", 403)
                        form = await request.form(
                            max_files=0, max_fields=3, max_part_size=4096
                        )
                        try:
                            if not auth.validate_csrf(raw, str(form.get("csrf", ""))):
                                raise PublishError("csrf_rejected", 403)
                            await run_in_threadpool(
                                auth.throttle,
                                f"password:{identifier}:{request.client.host if request.client else 'unknown'}",
                                policy=core.publication_password_budget,
                                namespace="publication",
                            )
                            password = form.get("password", "")
                            if not isinstance(
                                password, str
                            ) or not await run_in_threadpool(
                                auth.verify_password, pub["password_hash"], password
                            ):
                                return password_page(
                                    request,
                                    raw,
                                    wrong_password=True,
                                    hint=pub.get("password_hint"),
                                    publication=pub,
                                    canonical=canonical,
                                )
                            await run_in_threadpool(auth.unlock, viewer, pub)
                        finally:
                            await form.close()
                    else:
                        return password_page(
                            request,
                            raw,
                            hint=pub.get("password_hint"),
                            publication=pub,
                            canonical=canonical,
                        )
                kind, subject = "guest", viewer
        language, theme = preferences(request)
        payload = {
            "publication_id": pub["id"],
            "revision_id": revision_id,
            "owner_history": owner_history,
            "shared_revision": pinned_sequence is not None and not owner_history,
            "revision_share_version": selected_revision.get("share_version", 0) if selected_revision is not None else None,
            "count_guest_opening": not owner and countable_opening(
                request, selected_revision if selected_revision is not None else pub, path,
                guest=guest
            ),
            "statistics_owner_id": principal.id if owner else None,
            "count_owner_opening": owner and countable_opening(
                request, selected_revision if selected_revision is not None else pub, path, guest=guest
            ),
            "access_version": pub["access_version"],
            "subject_kind": kind,
            "subject_id": subject,
            "path": path,
            "language": language,
            "theme": theme,
            "opening_method": request.method,
            "opening_prefetch": any("prefetch" in request.headers.get(name, "").lower() for name in ("purpose", "sec-purpose")),
            "opening_accept": request.headers.get("accept", ""),
            "view_mode": "guest" if guest else "normal",
            "query": urlencode(
                [
                    (key, value)
                    for key, value in parse_qsl(
                        request.url.query, keep_blank_values=True
                    )
                    if key not in ("preview", "guest")
                ]
            ),
        }
        intent = await run_in_threadpool(auth.intent, payload)
        target = (
            config.content_origin(revision_id, guest=guest)
            + "/__mp/start?"
            + urlencode({"intent": intent})
        )
        if request.method == "POST" and kind in ("guest", "owner"):
            return await run_in_threadpool(
                navigation.redirect,
                target,
                kind="publication",
                realm="owner" if kind == "owner" else "probe" if guest else "normal",
                subject_id=subject,
                details={"intent": intent},
            )
        if not owner and opening_document(request, selected_revision if selected_revision is not None else pub, path):
            cover = cover_url(config, core, pub, selected_revision, pinned_sequence)
            return opening_page(
                config, pub, target, language=language, theme=theme,
                canonical=canonical, cover=cover,
                loading_label=messages.get("Loading…", {}).get(language, "Loading…"),
                open_label=messages.get("Open", {}).get(language, "Open"),
            )
        return redirect(target)

    @app.get("/api/v1/publications/{identifier}/revisions/{revision_id}/open")
    async def open_history(identifier: str, revision_id: str, request: Request):
        if not request.state.principal or not request.state.principal.owner:
            raise PublishError("owner_required", 403)
        return await stable(request, identifier, history_revision=revision_id)

    def password_page(request, raw, *, wrong_password=False, hint=None, publication=None, canonical=None):
        csrf = auth.csrf(raw)
        language, _ = preferences(request)
        clue = f'<p id="password-hint" class="password-hint">{html.escape(hint)}</p>' if hint else ""
        error = (
            f'<p id="password-error" class="gate-error" role="alert">{text(request, "Incorrect material password. Try again.")}</p>'
            if wrong_password else ""
        )
        descriptions = " ".join(identifier for identifier, content in (
            ("password-error", error), ("password-hint", clue)
        ) if content)
        described_by = f' aria-describedby="{descriptions}"' if descriptions else ""
        invalid = ' aria-invalid="true" autofocus' if wrong_password else ""
        response = page(
            request,
            "Password-protected",
            401 if wrong_password else 200,
            f"""{clue}<form class="gate-form" method="post"><input type="hidden" name="csrf" value="{csrf}">{error}<div class="gate-field"><label for="material-password">{text(request, "Password")}</label><input id="material-password" name="password" type="password" autocomplete="current-password" maxlength="1024"{described_by}{invalid} required></div><button class="system-action">{text(request, "Open")}</button></form>""",
            publication=publication,
            canonical=canonical,
            binary_theme=not request.state.revision_id and not request.state.legacy_host,
            footer=source_footer(
                config.source_url,
                translate=lambda label: messages[label][language],
                project_url=config.project_url or (config.base_url + "/" if not config.source_url else ""),
            ),
        )
        response.set_cookie(
            config.probe_viewer_cookie
            if request.query_params.get("guest") == "1"
            else config.viewer_cookie,
            raw,
            max_age=config.operations["viewer_grant_hours"] * 3600,
            httponly=True,
            secure=config.cookie_secure,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/p/_preferences")
    async def save_preferences(request: Request):
        if request.state.revision_id:
            raise PublishError("not_found", 404)
        if request.headers.get("origin") != config.control_origin:
            raise PublishError("csrf_rejected", 403)
        form = await request.form(max_files=0, max_fields=4, max_part_size=1024)
        try:
            target, theme, language = (
                str(form.get("return_to", "")),
                str(form.get("theme", "")),
                str(form.get("language", "en")),
            )
            parsed_target = urlsplit(target)
            if (
                parsed_target.scheme
                or parsed_target.netloc
                or parsed_target.fragment
                or parse_qsl(parsed_target.query, keep_blank_values=True)
                not in ([], [("guest", "1")])
                or not re.fullmatch(
                    r"/p/[A-Za-z0-9_-]+(?:~[1-9][0-9]{0,18})?(?:/[^?#\\]*)?", parsed_target.path
                )
                or theme not in ("system", "light", "dark")
                or language not in ("en", "ru", "es", "fr")
            ):
                raise PublishError("invalid_preference", 400)
            response = redirect(config.base_url + target)
            response.set_cookie(
                "mp_lang",
                language,
                max_age=31536000,
                secure=config.cookie_secure,
                samesite="lax",
                path="/",
            )
            response.set_cookie(
                "mp_theme",
                theme,
                max_age=31536000,
                secure=config.cookie_secure,
                samesite="lax",
                path="/",
            )
            return response
        finally:
            await form.close()

    @app.api_route("/p/{identifier}", methods=["GET", "HEAD", "POST"])
    @app.api_route("/p/{identifier}/{path:path}", methods=["GET", "HEAD", "POST"])
    async def stable_route(identifier: str, request: Request, path: str = ""):
        if request.state.revision_id:
            return await serve(request, request.url.path.lstrip("/"))
        if "~" in identifier:
            identifier, _, sequence = identifier.rpartition("~")
            if not re.fullmatch(r"[1-9][0-9]{0,18}", sequence) or int(sequence) > 9223372036854775807:
                return page(request, "This publication is unavailable.", 404)
            return await stable(request, identifier, path, pinned_sequence=int(sequence))
        return await stable(request, identifier, path)

    @app.api_route("/__mp/preview/{identifier}/{revision_id}.jpg", methods=["GET", "HEAD"])
    async def preview_image(
        identifier: str,
        revision_id: str,
        request: Request,
        sequence: int | None = Query(default=None, ge=1, le=9223372036854775807),
    ):
        if request.state.revision_id or request.state.legacy_host:
            raise PublishError("not_found", 404)
        await admit_open(request, "preview-image")
        image = await run_in_threadpool(public_cover, core, identifier, revision_id, sequence)
        return Response(
            image if request.method == "GET" else b"", media_type="image/jpeg",
            headers={
                "Content-Length": str(len(image)),
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    @app.api_route("/__mp/authorize", methods=["GET", "HEAD"])
    async def authorize(request: Request, intent: str = "", nonce: str = ""):
        if request.state.revision_id or request.state.legacy_host:
            raise PublishError("not_found", 404)

        def validator(payload):
            if not check_payload(payload, new=True):
                return False
            if payload["subject_kind"] == "owner":
                principal = request.state.principal
                return bool(
                    principal
                    and principal.owner
                    and principal.id == payload["subject_id"]
                )
            if payload["subject_kind"] == "guest":
                return (
                    auth.existing_viewer(
                        request.cookies.get(
                            config.probe_viewer_cookie
                            if payload.get("view_mode") == "guest"
                            else config.viewer_cookie
                        )
                    )
                    == payload["subject_id"]
                )
            return True

        code, payload = await run_in_threadpool(
            auth.authorize_intent, intent, nonce, validator
        )
        return redirect(
            config.content_origin(
                payload["revision_id"], guest=payload.get("view_mode") == "guest"
            )
            + "/__mp/accept?"
            + urlencode({"code": code})
        )

    async def control_gate(request, revision_id, path, guest):
        if not document_navigation(request):
            return None
        try:
            revision = await run_in_threadpool(core.get_revision, revision_id)
            pub = await run_in_threadpool(
                core.get_publication, revision["publication_id"]
            )
        except PublishError as error:
            if error.status == 404:
                return None
            raise
        if path:
            normalize_path(path.rstrip("/"), config.limits)
        try:
            sequence = await run_in_threadpool(version_sequence, core, pub["id"], revision_id)
        except PublishError as error:
            if error.status == 404:
                return page(request, "This publication is unavailable.", 404)
            raise
        target = version_url(config, pub["id"], sequence)
        response = redirect(
            target
            + ("/" + quote(path, safe="/") if path else "")
            + stable_query(request.url.query, guest)
        )
        response.headers["Vary"] = "Accept, Sec-Fetch-Mode, Sec-Fetch-Dest, User-Agent"
        return response

    def file_in_manifest(connection, revision_id, path):
        return connection.execute(
            "SELECT 1 FROM revision_file WHERE revision_id=? AND path=?",
            (revision_id, path),
        ).fetchone() is not None

    def directory_index(connection, revision_id, path):
        return not file_in_manifest(connection, revision_id, path) and file_in_manifest(
            connection, revision_id, path + "/index.html"
        )

    def revision_route(revision_id, path):
        with core.repository.connect() as connection:
            revision = core._revision(connection, revision_id, manifest=False)
            needs_slash = (
                revision["kind"] == "site"
                and bool(path)
                and not path.endswith("/")
                and directory_index(connection, revision_id, path)
            )
            return revision, needs_slash

    def record_opening(request, connection, value):
        if not (value.get("count_guest_opening") or value.get("count_owner_opening")) or request.method == "HEAD" or any(
            "prefetch" in request.headers.get(name, "").lower() for name in ("purpose", "sec-purpose")
        ):
            return
        if value.get("count_owner_opening") and not auth._subject_active(
            connection, "owner", value.get("statistics_owner_id"),
            core._publication(connection, value["publication_id"]),
        ):
            return
        revision = core._revision(connection, value["revision_id"], manifest=False)
        requested = value.get("path") or revision["entry_path"]

        def exists(path):
            return file_in_manifest(connection, revision["id"], path)

        if revision["kind"] == "site":
            if requested.endswith("/"):
                requested += "index.html"
            elif directory_index(connection, revision["id"], requested):
                requested += "/index.html"
        if not exists(requested):
            publication = core._publication(connection, value["publication_id"])
            if not (
                revision["kind"] == "site" and publication["site_routing"] == "spa"
                and not PurePosixPath(requested).suffix and "text/html" in value.get("opening_accept", "")
                and exists(revision["entry_path"])
            ):
                return
        statistics.record(connection, value)

    def reader_response(token, payload, *, clear_bridge):
        clean = payload.get("path", "")
        if clean:
            normalize_path(clean.rstrip("/"), config.limits)
        response = redirect(
            config.content_origin(payload["revision_id"], guest=payload.get("view_mode") == "guest")
            + "/" + quote(clean, safe="/")
            + ("?" + payload["query"] if payload.get("query") else "")
        )
        response.set_cookie(
            config.reader_cookie, token,
            max_age=config.operations["viewer_grant_hours"] * 3600,
            secure=config.cookie_secure, httponly=True, samesite="lax", path="/",
        )
        if clear_bridge:
            response.delete_cookie(
                config.bridge_cookie, path="/", secure=config.cookie_secure,
                httponly=True, samesite="lax",
            )
        for name, key in (("mp_lang", "language"), ("mp_theme", "theme")):
            response.set_cookie(
                name, payload[key], max_age=31536000,
                secure=config.cookie_secure, samesite="lax", path="/",
            )
        return response

    async def serve(request, path):
        revision_id = request.state.revision_id
        if not revision_id:
            raise PublishError("not_found", 404)
        mode = request.state.content_mode
        guest = mode == "guest"
        if path == "__mp/start":
            raw = request.query_params.get("intent", "")
            reader = request.cookies.get(config.reader_cookie)
            if request.method == "GET" and not guest and reader and not any(
                "prefetch" in request.headers.get(name, "").lower() for name in ("purpose", "sec-purpose")
            ):
                resumed = await run_in_threadpool(
                    auth.resume_reader, raw, reader, revision_id,
                    lambda value, connection: check_payload(value, new=True, mode=mode, connection=connection),
                    lambda connection, value: record_opening(request, connection, value),
                )
                if resumed is not None:
                    return reader_response(reader, resumed, clear_bridge=False)
            payload = await run_in_threadpool(auth.peek_intent, raw)
            if (
                payload["revision_id"] != revision_id
                or payload.get("view_mode", "normal") != mode
                or (guest and payload["subject_kind"] == "owner")
            ):
                raise PublishError("handoff_denied", 403)
            nonce = secrets.token_urlsafe(32)
            response = redirect(
                config.base_url
                + "/__mp/authorize?"
                + urlencode({"intent": raw, "nonce": digest(nonce)})
            )
            response.set_cookie(
                config.bridge_cookie,
                nonce,
                max_age=config.operations["handoff_seconds"],
                secure=config.cookie_secure,
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response
        if path == "__mp/accept":
            token, payload = await run_in_threadpool(
                auth.consume_handoff,
                request.query_params.get("code", ""),
                request.cookies.get(config.bridge_cookie, ""),
                revision_id,
                lambda value: check_payload(value, new=True, mode=mode),
                lambda connection, value: record_opening(request, connection, value),
            )
            return reader_response(token, payload, clear_bridge=True)
        if path.startswith("__mp/"):
            raise PublishError("not_found", 404)
        if request.method not in ("GET", "HEAD"):
            raise PublishError("method_not_allowed", 405)
        payload = await run_in_threadpool(
            auth.reader, request.cookies.get(config.reader_cookie)
        )
        if (
            not payload
            or payload["revision_id"] != revision_id
            or payload.get("view_mode", "normal") != mode
        ):
            response = await control_gate(request, revision_id, path, guest)
            if response is not None:
                return response
            raise PublishError("content_access_required", 403)
        if not await run_in_threadpool(check_payload, payload, False, mode):
            response = await control_gate(request, revision_id, path, guest)
            if response is not None:
                return response
            raise PublishError("content_access_required", 403)
        pub = await run_in_threadpool(core.get_publication, payload["publication_id"])
        revision, needs_slash = await run_in_threadpool(revision_route, revision_id, path)
        requested = path or revision["entry_path"]
        if revision["kind"] == "site":
            suffix = "?" + request.url.query if request.url.query else ""
            if not path and revision["entry_path"] != "index.html":
                return redirect(
                    config.content_origin(revision_id, guest=guest)
                    + "/"
                    + quote(revision["entry_path"], safe="/")
                    + suffix
                )
            if requested.endswith("/"):
                requested += "index.html"
            elif needs_slash:
                return redirect(
                    config.content_origin(revision_id, guest=guest)
                    + "/"
                    + quote(requested, safe="/")
                    + "/"
                    + suffix
                )
        try:
            target, info = await run_in_threadpool(
                core.resolve_file, revision_id, requested
            )
        except PublishError as error:
            if (
                error.status != 404
                or revision["kind"] != "site"
                or pub["site_routing"] != "spa"
                or "text/html" not in request.headers.get("accept", "")
                or PurePosixPath(path).suffix
            ):
                raise
            target, info = await run_in_threadpool(
                core.resolve_file, revision_id, revision["entry_path"]
            )
        media = info.get("content_type", "application/octet-stream")
        inline = media.startswith(
            ("text/", "image/", "audio/", "video/", "font/")
        ) or media in (
            "application/pdf",
            "application/json",
            "application/wasm",
            "application/xml",
        )
        if revision["kind"] == "file" and pub["preview"] == "download":
            inline = False
        network = (
            "https: data: blob:"
            if pub["external_resources"] == "allow"
            else "data: blob:"
        )
        headers = {
            "Content-Security-Policy": f"default-src 'self' {network}; script-src 'self' 'unsafe-inline' 'unsafe-eval' {network}; style-src 'self' 'unsafe-inline' {network}; worker-src 'self' blob:; form-action 'self' {'https:' if pub['external_resources'] == 'allow' else ''}; object-src 'none'; base-uri 'self'; frame-ancestors 'self' {config.base_url}",
            "X-Robots-Tag": "index, follow"
            if pub["indexing"]
            else "noindex, nofollow, noarchive",
            "Vary": "Service-Worker, Sec-Fetch-Dest",
        }
        return LeasedFile(
            core,
            revision_id,
            target,
            media_type=media,
            headers=headers,
            filename=target.name,
            content_disposition_type="inline" if inline else "attachment",
        )

    app.state.content = serve

    @app.get("/api/v1/publications/{identifier}/thumbnail")
    async def thumbnail(identifier: str, request: Request):
        pub = await run_in_threadpool(core.get_publication, identifier)
        if pub["content_state"] != "present" or pub.get("deletion_state"):
            raise PublishError("content_unavailable", 410)
        revision = await run_in_threadpool(core.get_revision, pub["current_revision"])
        target = revision["path"] / "thumbnail.jpg"
        if not target.is_file() or target.is_symlink():
            raise PublishError("thumbnail_not_found", 404)
        return LeasedFile(
            core,
            revision["id"],
            target,
            media_type="image/jpeg",
            headers={"Content-Security-Policy": "default-src 'none'"},
        )

    @app.get("/api/v1/publications/{identifier}/download")
    async def download(identifier: str, request: Request):
        pub = await run_in_threadpool(core.get_publication, identifier)
        if pub["content_state"] != "present" or pub.get("deletion_state"):
            raise PublishError("content_unavailable", 410)
        if pub["kind"] == "file":
            target, info = await run_in_threadpool(
                core.resolve_file, pub["current_revision"], pub["entry_path"]
            )
            return LeasedFile(
                core,
                pub["current_revision"],
                target,
                media_type="application/octet-stream",
                filename=pub["filename"],
                content_disposition_type="attachment",
            )
        from .exports import site_export

        return await site_export(core, config, pub)

    @app.get("/api/v1/publications/{identifier}/revisions/{revision_id}/download")
    async def history_download(identifier: str, revision_id: str, request: Request):
        if not request.state.principal or not request.state.principal.owner:
            raise PublishError("owner_required", 403)
        await run_in_threadpool(core.history_revision, identifier, revision_id)
        revision = await run_in_threadpool(core.get_revision, revision_id)
        if revision["kind"] == "file":
            target, _ = await run_in_threadpool(core.resolve_file, revision_id, revision["entry_path"])
            return LeasedFile(core, revision_id, target, media_type="application/octet-stream", filename=revision.get("filename") or PurePosixPath(revision["entry_path"]).name, content_disposition_type="attachment")
        from .exports import site_export

        pub = await run_in_threadpool(core.get_publication, identifier)
        return await site_export(core, config, {**pub, "current_revision": revision_id})
