"""Source and project links for server-owned guest surfaces."""

from collections.abc import Callable
from html import escape


def source_footer(
    url: str, *, translate: Callable[[str], str] = str, project_url: str = ""
) -> str:
    if project_url:
        label = "Made available via minder publish."
        return (
            '<footer class="source-footer">'
            f'<a class="project-attribution" href="{escape(project_url, quote=True)}" '
            f'referrerpolicy="no-referrer" data-i18n="{label}">{escape(translate(label))}</a>'
            '</footer>'
        )
    if not url:
        return ""
    mark = (
        '<svg viewBox="0 0 16 16" aria-hidden="true" fill="currentColor">'
        '<path d="M8 0a8 8 0 0 0-2.53 15.59c.4.07.55-.17.55-.38v-1.49'
        'c-2.23.49-2.7-.95-2.7-.95-.37-.93-.89-1.18-.89-1.18-.73-.5.05-.49.05-.49'
        '.8.06 1.22.82 1.22.82.72 1.22 1.87.87 2.32.66.07-.52.28-.87.51-1.07'
        '-1.78-.2-3.65-.89-3.65-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12'
        ' 0 0 .67-.22 2.2.82a7.68 7.68 0 0 1 4 0c1.53-1.04 2.2-.82 2.2-.82'
        '.44 1.1.16 1.92.08 2.12.51.56.82 1.28.82 2.15 0 3.07-1.87 3.75-3.66 3.95'
        '.29.25.55.73.55 1.48v2.22c0 .21.14.46.55.38A8 8 0 0 0 8 0Z"/></svg>'
    )
    label = "Code and installation instructions on GitHub"
    name = f'<span data-i18n="{label}">{escape(translate(label))}</span>'
    return (
        '<footer class="source-footer">'
        f'<a href="{escape(url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer" referrerpolicy="no-referrer">{mark}{name}</a>'
        '</footer>'
    )
