"""Archive boundary for the instance's anonymous CLI download."""

from __future__ import annotations

import configparser
import re
import stat
from email.parser import BytesParser
from zipfile import ZipFile

CLIENT_PREFIX = "minder_publish/client/"
CLIENT_ENTRY = "minder_publish.client.cli:main"
SHARED_CLIENT_MODULES = frozenset(
    {"minder_publish/__init__.py", "minder_publish/dependencies.py"}
)
METADATA_FILES = frozenset(
    {"METADATA", "WHEEL", "entry_points.txt", "RECORD", "licenses/LICENSE"}
)
REQUIRED_CLIENT_FILES = SHARED_CLIENT_MODULES | frozenset(
    {
        CLIENT_PREFIX + "__init__.py",
        CLIENT_PREFIX + "api.py",
        CLIENT_PREFIX + "cli.py",
        CLIENT_PREFIX + "config.py",
        CLIENT_PREFIX + "errors.py",
        CLIENT_PREFIX + "preflight.py",
        CLIENT_PREFIX + "setup.py",
        CLIENT_PREFIX + "skills.py",
    }
)
SKILL_FOLDER = "share/minder-publish/skills/minder-publish"
REQUIRED_SKILL_FILES = frozenset(
    {"SKILL.md", "references/cli.md", "references/installation.md", "references/mcp.md"}
)
MAX_WHEEL_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MODULE = re.compile(r"[a-z_][a-z0-9_]*\.py")
SKILL_REFERENCE = re.compile(r"references/[a-z][a-z0-9-]*\.md")


def is_client_module(name: str) -> bool:
    return name in SHARED_CLIENT_MODULES or (
        name.startswith(CLIENT_PREFIX)
        and MODULE.fullmatch(name.removeprefix(CLIENT_PREFIX)) is not None
    )


def validate_client_wheel(archive: ZipFile, version: str) -> None:
    stem = f"minder_publish-{version}"
    metadata = f"{stem}.dist-info/"
    skill = f"{stem}.data/data/{SKILL_FOLDER}/"
    required = (
        REQUIRED_CLIENT_FILES
        | {metadata + name for name in METADATA_FILES}
        | {skill + name for name in REQUIRED_SKILL_FILES}
    )
    entries = archive.infolist()
    if sum(entry.file_size for entry in entries) > MAX_WHEEL_BYTES:
        raise ValueError("expanded client wheel size")
    seen = set()
    for entry in entries:
        name = entry.filename
        if (
            entry.orig_filename != name
            or name in seen
            or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
            or not (
                name in required
                or name == metadata + "top_level.txt"
                or is_client_module(name)
                or (
                    name.startswith(skill)
                    and SKILL_REFERENCE.fullmatch(name.removeprefix(skill))
                )
            )
        ):
            raise ValueError("unexpected client wheel member")
        seen.add(name)
    if not required <= seen:
        raise ValueError("missing client wheel member")

    def read_metadata(name: str) -> bytes:
        entry = archive.getinfo(metadata + name)
        if entry.file_size > MAX_METADATA_BYTES:
            raise ValueError("client wheel metadata size")
        return archive.read(entry)

    info = BytesParser().parsebytes(read_metadata("METADATA"))
    if (
        info.get_all("Name") != ["minder-publish"]
        or info.get_all("Version") != [version]
        or info.get_all("Requires-Python") != [">=3.10"]
        or info.get_all("License-Expression") != ["MIT"]
        or info.get_all("License-File") != ["LICENSE"]
        or info.get_all("Provides-Extra")
    ):
        raise ValueError("client wheel metadata identity")
    points = configparser.ConfigParser(interpolation=None)
    points.read_string(read_metadata("entry_points.txt").decode("utf-8"))
    if (
        points.sections() != ["console_scripts"]
        or points.defaults()
        or dict(points["console_scripts"]) != {"minder-publish": CLIENT_ENTRY}
    ):
        raise ValueError("client wheel entry points")
    wheel = BytesParser().parsebytes(read_metadata("WHEEL"))
    if wheel.get_all("Root-Is-Purelib") != ["true"] or wheel.get_all("Tag") != [
        "py3-none-any"
    ]:
        raise ValueError("client wheel portability")
