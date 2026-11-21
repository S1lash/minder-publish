"""Packaged control-origin presentation for OAuth consent and continuation."""

from __future__ import annotations

from ..brand import LOCKUP


def preferences(t, language, theme):
    """Change only existing allowlisted browser preferences, outside the auth form."""

    def control(name, title, selected, choices):
        options = "".join(
            f'<button type="button" data-oauth-preference="{name}" value="{value}"'
            + (' aria-current="true"' if value == selected else '')
            + f'>{label}</button>'
            for value, label in choices
        )
        return (
            '<details class="oauth-preference">'
            f'<summary>{title}</summary><div class="oauth-preference-menu">'
            f'{options}</div></details>'
        )

    return (
        '<div class="oauth-preferences" hidden>'
        + control("mp_lang", t("Language"), language, (
            ("en", "English"), ("ru", "Русский"), ("es", "Español"), ("fr", "Français"),
        ))
        + control("mp_theme", t("Theme"), theme, (
            ("system", t("System")), ("light", t("Light")), ("dark", t("Dark")),
        ))
        + '</div>'
    )


def page(title, body, *, language, theme, controls="", refresh="", continuation=False):
    """Receive escaped presentation fragments; keep code and fonts on this origin."""
    scripts = (
        '<script src="/static/preferences.js"></script>'
        '<script type="module" src="/static/oauth.js"></script>'
        if controls else ""
    )
    return (
        f'<!doctype html><html lang="{language}" data-theme="{theme}"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f'<title>{title} · minder publish.</title>'
        '<link rel="icon" href="/static/mark.svg" type="image/svg+xml">'
        + scripts
        + '<link rel="stylesheet" href="/static/theme.css">'
        '<link rel="stylesheet" href="/static/ui.css">'
        '<link rel="stylesheet" href="/static/oauth.css">'
        + refresh
        + '</head><body class="oauth-page'
        + (' oauth-continuation' if continuation else '')
        + '"><div class="oauth-shell"><header class="oauth-header">'
        + LOCKUP
        + controls
        + f'</header><main class="oauth-card">{body}</main></div></body></html>'
    )
