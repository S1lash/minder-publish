"""Packaged theme and lockup for server-rendered system screens."""

from pathlib import Path
from xml.etree import ElementTree


_WEB = Path(__file__).parent / "web"
THEME_CSS = (_WEB / "theme.css").read_text(encoding="utf-8")
SYSTEM_CSS = (_WEB / "system.css").read_text(encoding="utf-8")
LANGUAGE_ICON = (_WEB / "language.svg").read_text(encoding="utf-8").strip()
THEME_ICON = (_WEB / "theme-icon.svg").read_text(encoding="utf-8").strip()
_SVG = "{http://www.w3.org/2000/svg}"
_MARK_ASSET = ElementTree.parse(_WEB / "mark.svg").getroot()
_PATH = _MARK_ASSET.find(f"{_SVG}path").attrib["d"]
_CIRCLE = _MARK_ASSET.find(f"{_SVG}circle").attrib
_DOT = " ".join(f'{key}="{_CIRCLE[key]}"' for key in ("cx", "cy", "r"))
_SYMBOL = '<svg viewBox="0 0 32 32" aria-hidden="true">'
_FRAME = f'<path fill="var(--brand-mark)" d="{_PATH}"/>'
_LOCKUP_SYMBOL = f"{_SYMBOL}{_FRAME}</svg>"
_STANDALONE_SYMBOL = f'{_SYMBOL}{_FRAME}<circle fill="var(--brand-dot)" {_DOT}/></svg>'

LOCKUP = (
    f'<div class="brand">{_LOCKUP_SYMBOL}'
    '<span class="wordmark">minder publish<span class="dot">.</span></span></div>'
)


def inline_marks(markup):
    return (
        markup.replace("<!-- brand-lockup -->", _LOCKUP_SYMBOL)
        .replace("<!-- brand-standalone -->", _STANDALONE_SYMBOL)
    )
