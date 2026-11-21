"""Generate the packaged Publish marks, touch icon and square preview."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "minder_publish" / "server" / "web"
SVG_NS = "{http://www.w3.org/2000/svg}"


def render() -> None:
    source = ElementTree.parse(ROOT / "docs" / "brand" / "open-corner.svg").getroot()
    path = source.find(f"{SVG_NS}path").get("d")
    circle = source.find(f"{SVG_NS}circle")
    dot = " ".join(f'{key}="{circle.get(key)}"' for key in ("cx", "cy", "r"))
    colors = dict(re.findall(r"--([\w-]+): (#[\da-fA-F]{6});", (WEB / "theme.css").read_text()))
    ink, dark_ink = colors["brand-mark"], colors["dark-brand-mark"]
    accent, dark_accent = colors["brand-dot"], colors["dark-brand-dot"]

    def svg(body: str) -> str:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">\n{body}\n</svg>\n'

    mark = svg(
        f'  <style>path{{fill:{ink}}}circle{{fill:{accent}}}'
        f'@media(prefers-color-scheme:dark){{path{{fill:{dark_ink}}}circle{{fill:{dark_accent}}}}}</style>\n'
        f'  <path fill="{ink}" d="{path}"/>\n'
        f'  <circle fill="{accent}" {dot}/>'
    )
    lockup = svg(
        f'  <style>@media(prefers-color-scheme:dark){{path{{fill:{dark_ink}}}}}</style>\n'
        f'  <path fill="{ink}" d="{path}"/>'
    )
    preview_svg = svg(
        f'  <path fill="{colors["surface2"]}" d="M0 0H32V32H0Z"/>\n'
        '  <g transform="translate(4.8 4.8) scale(.7)">\n'
        f'    <path fill="{ink}" d="{path}"/>\n'
        f'    <circle fill="{accent}" {dot}/>\n'
        '  </g>'
    )
    (WEB / "mark.svg").write_text(mark, encoding="utf-8")
    (WEB / "mark-lockup.svg").write_text(lockup, encoding="utf-8")
    for filename, size in (("link-preview.png", "1024"), ("apple-touch-icon.png", "180")):
        image = subprocess.run(
            ["rsvg-convert", "-w", size, "-h", size],
            input=preview_svg.encode(), capture_output=True, check=True,
        ).stdout
        (WEB / filename).write_bytes(image)


if __name__ == "__main__":
    render()
