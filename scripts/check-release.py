#!/usr/bin/env python3
"""Audit built public artifacts without extracting them or printing secret values.

This checks archive boundaries, resources, local documentation links and selected
credential/private-path signatures. It is not a complete Git-history secret scan,
a dependency vulnerability audit or proof that an installed application works.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import posixpath
import re
import runpy
import subprocess
import tarfile
import unicodedata
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 128 * 1024**2
MAX_MEMBER_BYTES = 16 * 1024**2
MAX_ENTRIES = 4096
PACKAGE = "minder_publish/"
PUBLIC_ROOT_FILES = frozenset(
    {
        "AGENTS.md",
        "CONTRIBUTING.md",
        "Dockerfile",
        "compose.yaml",
        ".dockerignore",
        ".gitattributes",
        ".gitignore",
        ".env.example",
        "requirements.lock",
        "requirements-test.lock",
        "MANIFEST.in",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "PKG-INFO",
        "setup.cfg",
        "scripts/install-skills.py",
        "scripts/smoke-container.py",
        "scripts/check-release.py",
        "scripts/normalize-sdist.py",
        "scripts/render-brand.py",
        "scripts/build-client-wheel.py",
        ".github/workflows/ci.yml",
    }
)
PUBLIC_PREFIXES = (
    "src/minder_publish/",
    "src/minder_publish.egg-info/",
    "skill/minder-publish/",
    "tests/",
    "deploy/publish/",
)
REQUIRED_RESOURCES = frozenset(
    {
        "minder_publish/__init__.py",
        "minder_publish/defaults.json",
        "minder_publish/client/cli.py",
        "minder_publish/client/api.py",
        "minder_publish/client/preflight.py",
        "minder_publish/client/config.py",
        "minder_publish/client/skills.py",
        "minder_publish/mcp/server.py",
        "minder_publish/mcp/internal_api.py",
        "minder_publish/mcp/host_files.py",
        "minder_publish/mcp/validation.py",
        "minder_publish/server/commands.py",
        "minder_publish/server/app.py",
        "minder_publish/server/auth.py",
        "minder_publish/server/core.py",
        "minder_publish/server/storage.py",
        "minder_publish/server/backup.py",
        "minder_publish/server/distribution.py",
        "minder_publish/server/client_wheel.py",
        "minder_publish/server/upload_jobs.py",
        "minder_publish/server/navigation.py",
        "minder_publish/server/publication_links.py",
        "minder_publish/server/oauth/__init__.py",
        "minder_publish/server/oauth/invalidation.py",
        "minder_publish/server/oauth/provider.py",
        "minder_publish/server/oauth/routes.py",
        "minder_publish/server/oauth/store.py",
        "minder_publish/server/migrations/core/001_up.sql",
        "minder_publish/server/migrations/core/001_down.sql",
        "minder_publish/server/migrations/core/002_up.sql",
        "minder_publish/server/migrations/core/002_down.sql",
        "minder_publish/server/migrations/core/003_up.sql",
        "minder_publish/server/migrations/core/004_up.sql",
        "minder_publish/server/migrations/core/004_down.sql",
        "minder_publish/server/migrations/core/005_up.sql",
        "minder_publish/server/migrations/core/005_down.sql",
        "minder_publish/server/migrations/core/006_up.sql",
        "minder_publish/server/migrations/core/006_down.sql",
        "minder_publish/server/migrations/auth/001_up.sql",
        "minder_publish/server/migrations/auth/001_down.sql",
        "minder_publish/server/migrations/auth/002_up.sql",
        "minder_publish/server/migrations/auth/002_down.sql",
        "minder_publish/server/migrations/oauth/001_up.sql",
        "minder_publish/server/migrations/oauth/001_down.sql",
        "minder_publish/server/web/index.html",
        "minder_publish/server/web/app.js",
        "minder_publish/server/web/i18n.js",
        "minder_publish/server/web/controls.js",
        "minder_publish/server/web/tooltips.js",
        "minder_publish/server/web/preferences.js",
        "minder_publish/server/web/ui.css",
        "minder_publish/server/web/guest.css",
        "minder_publish/server/web/connection.js",
        "minder_publish/server/web/connection.css",
        "minder_publish/server/web/materials.css",
        "minder_publish/server/web/reauth.js",
        "minder_publish/server/web/routes.js",
        "minder_publish/server/web/session.js",
        "minder_publish/server/web/messages.json",
        "minder_publish/server/web/mark.svg",
        "minder_publish/server/web/apple-touch-icon.png",
        "minder_publish/server/web/language.svg",
        "minder_publish/server/web/theme-icon.svg",
        "minder_publish/server/web/link-copy.js",
        "minder_publish/server/web/publication-link.js",
        "minder_publish/server/web/preview-policy.js",
        "minder_publish/server/web/fonts/commissioner.ttf",
        "minder_publish/server/web/fonts/commissioner-OFL.txt",
        "minder_publish/server/web/fonts/golostext.ttf",
        "minder_publish/server/web/fonts/golostext-OFL.txt",
    }
)
REQUIRED_SOURCE = frozenset(
    {
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "LICENSE",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "Dockerfile",
        "compose.yaml",
        ".dockerignore",
        ".gitattributes",
        ".env.example",
        "requirements.lock",
        "requirements-test.lock",
        ".github/workflows/ci.yml",
        "scripts/install-skills.py",
        "scripts/smoke-container.py",
        "scripts/check-release.py",
        "scripts/normalize-sdist.py",
        "scripts/render-brand.py",
        "scripts/build-client-wheel.py",
        "tests/fixtures/browser/index.html",
        "tests/fixtures/browser/worker.js",
        "tests/fixtures/browser/sw.js",
        "skill/minder-publish/SKILL.md",
        "skill/minder-publish/references/cli.md",
        "skill/minder-publish/references/installation.md",
        "skill/minder-publish/references/mcp.md",
        "docs/SELF_HOSTING.md",
        "docs/SELF_HOSTING.ru.md",
        "docs/SECURITY.md",
        "docs/ARCHITECTURE.md",
        "docs/implementation/API.md",
        "docs/LINK_PREVIEWS.md",
        "docs/REVISION_HISTORY.md",
        "docs/STATISTICS.md",
        "docs/brand/open-corner.svg",
    }
)
PUBLIC_DOC_FILES = frozenset(
    name for name in REQUIRED_SOURCE if name.startswith("docs/")
)
REQUIRED_SKILL_FILES = frozenset(
    {"SKILL.md", "references/cli.md", "references/installation.md", "references/mcp.md"}
)
SENSITIVE_NAMES = frozenset(
    {
        ".env",
        "server.secret",
        "auth.json",
        "keys.json",
        "credentials.json",
        "cookies.json",
        "id_rsa",
        "id_ed25519",
        ".DS_Store",
    }
)
SENSITIVE_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".token",
    ".log",
)
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".txt",
        ".json",
        ".sql",
        ".html",
        ".css",
        ".js",
        ".svg",
        ".toml",
        ".yml",
        ".yaml",
        ".template",
        ".cfg",
        ".lock",
        ".example",
        ".in",
    }
)
PRIVATE_PATH = re.compile(
    "/" + "Users" + r'/[^\s/"<>]+/|[A-Za-z]:\\' + "Users" + r'\\[^\s\\"<>]+\\'
)
CREDENTIAL = re.compile(
    r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{40,}|mp_[A-Za-z0-9_-]{32,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{24,})"
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{80,}"
)
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def safe_path(value: str) -> bool:
    return bool(
        value
        and not value.startswith("/")
        and "\\" not in value
        and "\x00" not in value
        and all(
            part not in ("", ".", "..") and ":" not in part for part in value.split("/")
        )
    )


def read_archive(path: Path) -> tuple[str, dict[str, bytes], list[dict]]:
    problems = []
    entries = {}
    expanded_bytes = 0
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        return "unknown", {}, [{"code": "archive_too_large", "path": path.name}]
    if path.name.endswith(".whl"):
        kind = "wheel"
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ENTRIES:
                return kind, {}, [{"code": "too_many_entries", "path": path.name}]
            for member in members:
                if member.is_dir():
                    continue
                mode = member.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    problems.append({"code": "archive_link", "path": member.filename})
                    continue
                if member.file_size > MAX_MEMBER_BYTES:
                    problems.append(
                        {"code": "member_too_large", "path": member.filename}
                    )
                    continue
                expanded_bytes += member.file_size
                if expanded_bytes > MAX_ARCHIVE_BYTES:
                    problems.append(
                        {"code": "expanded_archive_too_large", "path": path.name}
                    )
                    break
                if member.filename in entries:
                    problems.append(
                        {"code": "duplicate_entry", "path": member.filename}
                    )
                entries[member.filename] = archive.read(member)
    elif path.name.endswith(".tar.gz"):
        kind = "sdist"
        with path.open("rb") as compressed:
            header = compressed.read(10)
        if len(header) == 10 and header[3] & 0x1E:
            problems.append({"code": "archive_gzip_metadata", "path": path.name})
        with tarfile.open(path, "r:gz") as archive:
            for index, member in enumerate(archive):
                if index > MAX_ENTRIES:
                    problems.append({"code": "too_many_entries", "path": path.name})
                    break
                if not safe_path(member.name):
                    problems.append(
                        {"code": "unsafe_archive_path", "path": member.name}
                    )
                if (
                    member.uid != 0
                    or member.gid != 0
                    or member.uname not in ("", "root")
                    or member.gname not in ("", "root")
                ):
                    problems.append({"code": "archive_owner_metadata", "path": member.name})
                pax = member.pax_headers
                if (
                    pax.keys() - {"path", "mtime", "comment"}
                    or (
                        "comment" in pax
                        and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", pax["comment"])
                    )
                    or ("mtime" in pax and not re.fullmatch(r"-?\d+(?:\.\d+)?", pax["mtime"]))
                ):
                    problems.append({"code": "archive_pax_metadata", "path": member.name})
                if member.isdir():
                    continue
                if not member.isfile() or member.issparse():
                    problems.append(
                        {"code": "archive_link_or_special", "path": member.name}
                    )
                    continue
                if member.size > MAX_MEMBER_BYTES:
                    problems.append({"code": "member_too_large", "path": member.name})
                    continue
                expanded_bytes += member.size
                if expanded_bytes > MAX_ARCHIVE_BYTES:
                    problems.append(
                        {"code": "expanded_archive_too_large", "path": path.name}
                    )
                    break
                if member.name in entries:
                    problems.append({"code": "duplicate_entry", "path": member.name})
                handle = archive.extractfile(member)
                entries[member.name] = handle.read() if handle else b""
        roots = {name.split("/")[0] for name in entries}
        if len(roots) != 1 or any("/" not in name for name in entries):
            problems.append({"code": "invalid_source_root", "path": path.name})
        else:
            entries = {name.split("/", 1)[1]: value for name, value in entries.items()}
    else:
        return "unknown", {}, [{"code": "unsupported_artifact", "path": path.name}]
    return kind, entries, problems


def compare_git_tree(
    entries: dict[str, bytes], root: Path, tree: str
) -> tuple[str, list[dict]]:
    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout

    resolved_tree = (
        git("rev-parse", "--verify", "--end-of-options", tree + "^{tree}")
        .decode("ascii")
        .strip()
    )
    expected = {}
    findings = []
    for record in git("ls-tree", "-r", "-z", resolved_tree).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, _, object_id = metadata.split()
        name = raw_path.decode("utf-8")
        if mode not in (b"100644", b"100755") or not safe_path(name):
            findings.append({"code": "unsupported_git_tree_entry", "path": name})
            continue
        expected[name] = object_id

    for name in sorted(expected.keys() - entries.keys()):
        findings.append({"code": "missing_git_tree_file", "path": name})
    for name in sorted(entries.keys() - expected.keys()):
        findings.append({"code": "unexpected_git_tree_file", "path": name})
    for name in sorted(expected.keys() & entries.keys()):
        content = entries[name]
        algorithm = hashlib.sha1 if len(expected[name]) == 40 else hashlib.sha256
        header = f"blob {len(content)}\0".encode("ascii")
        digest = algorithm(header, usedforsecurity=False)
        digest.update(content)
        if digest.hexdigest().encode("ascii") != expected[name]:
            findings.append({"code": "changed_git_tree_file", "path": name})
    return resolved_tree, findings


def audit(
    path: Path,
    *,
    source_root: Path | None = None,
    git_tree: str | None = None,
    client_wheel: bool = False,
) -> dict:
    kind, entries, problems = read_archive(path)
    source_tree = None
    client_module = None
    if client_wheel:
        if kind != "wheel":
            problems.append({"code": "expected_client_wheel", "path": path.name})
        else:
            package = Path(__file__).resolve().parents[1] / "src/minder_publish"
            boundary = runpy.run_path(str(package / "server/client_wheel.py"))
            version = runpy.run_path(str(package / "__init__.py"))["__version__"]
            client_module = boundary["is_client_module"]
            try:
                with zipfile.ZipFile(path) as archive:
                    boundary["validate_client_wheel"](archive, version)
            except (ValueError, KeyError, configparser.Error):
                problems.append({"code": "invalid_client_wheel", "path": path.name})
    if git_tree is not None:
        if kind == "sdist":
            kind = "git-source"
            try:
                source_tree, tree_findings = compare_git_tree(
                    entries, source_root or Path.cwd(), git_tree
                )
            except (OSError, subprocess.CalledProcessError):
                problems.append({"code": "unreadable_git_tree"})
            else:
                problems.extend(tree_findings)
        else:
            problems.append({"code": "expected_git_source_tarball"})
        source_root = None
    folded = set()
    metadata_files = [name for name in entries if name.endswith(".dist-info/METADATA")]
    metadata_prefix = (
        metadata_files[0].rsplit("/", 1)[0] + "/" if len(metadata_files) == 1 else ""
    )
    skill_files = [
        name
        for name in entries
        if name.endswith(
            ".data/data/share/minder-publish/skills/minder-publish/SKILL.md"
        )
    ]
    skill = skill_files[0] if len(skill_files) == 1 else ""
    skill_root = skill.removesuffix("SKILL.md") if skill else ""
    skill_sources = (
        {
            skill_root + name: "skill/minder-publish/" + name
            for name in REQUIRED_SKILL_FILES
        }
        if skill_root
        else {}
    )
    if kind == "wheel":
        required = set(
            boundary["REQUIRED_CLIENT_FILES"] if client_module else REQUIRED_RESOURCES
        )
        if not metadata_prefix:
            problems.append({"code": "missing_metadata", "path": path.name})
        else:
            required.update(
                metadata_prefix + name
                for name in (
                    "METADATA",
                    "WHEEL",
                    "entry_points.txt",
                    "RECORD",
                    "licenses/LICENSE",
                )
            )
        if not skill:
            problems.append({"code": "missing_skill", "path": path.name})
        else:
            required.update(skill_sources)
    else:
        required = set(REQUIRED_SOURCE) | {"src/" + name for name in REQUIRED_RESOURCES}
    if source_root is not None:
        for source in (source_root / "skill/minder-publish").rglob("*"):
            if source.is_file() and source.suffix == ".md":
                relative = source.relative_to(
                    source_root / "skill/minder-publish"
                ).as_posix()
                if kind == "wheel" and skill_root:
                    skill_sources[skill_root + relative] = (
                        "skill/minder-publish/" + relative
                    )
                    required.add(skill_root + relative)
                elif kind == "sdist":
                    required.add("skill/minder-publish/" + relative)
        for source in (source_root / "src" / "minder_publish").rglob("*"):
            if (
                source.is_file()
                and "__pycache__" not in source.parts
                and source.suffix not in (".pyc", ".pyo")
                and source.name != ".DS_Store"
            ):
                package_path = source.relative_to(source_root / "src").as_posix()
                if client_module and not client_module(package_path):
                    continue
                required.add(package_path if kind == "wheel" else "src/" + package_path)
    for missing in sorted(required - entries.keys()):
        problems.append({"code": "missing_required_file", "path": missing})
    for name, content in entries.items():
        if not safe_path(name):
            problems.append({"code": "unsafe_archive_path", "path": name})
            continue
        normalized_name = unicodedata.normalize("NFC", name).casefold()
        if normalized_name in folded:
            problems.append({"code": "portable_path_collision", "path": name})
        folded.add(normalized_name)
        parts = PurePosixPath(name).parts
        if (
            any(
                part
                in (
                    ".git",
                    ".venv",
                    "__pycache__",
                    "node_modules",
                    "private",
                )
                for part in parts
            )
            or any(part in SENSITIVE_NAMES for part in parts)
            or name.endswith(SENSITIVE_SUFFIXES)
        ):
            problems.append({"code": "private_or_generated_file", "path": name})
        if kind == "wheel":
            permitted = (
                (client_module(name) if client_module else name.startswith(PACKAGE))
                or bool(metadata_prefix and name.startswith(metadata_prefix))
                or name in skill_sources
            )
        else:
            permitted = (
                name in PUBLIC_ROOT_FILES
                or name in PUBLIC_DOC_FILES
                or name.startswith(PUBLIC_PREFIXES)
            )
        if not permitted:
            problems.append({"code": "outside_public_boundary", "path": name})
        text = None
        if Path(name).suffix in TEXT_SUFFIXES or Path(name).name in (
            "Dockerfile",
            "PKG-INFO",
            "METADATA",
            "LICENSE",
            ".dockerignore",
            ".gitattributes",
            ".gitignore",
        ):
            try:
                text = content.decode("utf-8")
            except UnicodeError:
                problems.append({"code": "invalid_text_encoding", "path": name})
            if text is not None:
                for code, pattern in (
                    ("personal_path", PRIVATE_PATH),
                    ("credential_signature", CREDENTIAL),
                    ("private_key_material", PRIVATE_KEY),
                ):
                    match = pattern.search(text)
                    if match:
                        problems.append(
                            {
                                "code": code,
                                "path": name,
                                "line": text.count("\n", 0, match.start()) + 1,
                            }
                        )
        if source_root is not None:
            relative = (
                "LICENSE"
                if metadata_prefix and name == metadata_prefix + "licenses/LICENSE"
                else ("src/" + name)
                if kind == "wheel" and name.startswith(PACKAGE)
                else skill_sources[name]
                if name in skill_sources
                else name
            )
            if (
                (
                    kind == "wheel"
                    and not (
                        name.startswith(PACKAGE)
                        or name in skill_sources
                        or relative == "LICENSE"
                    )
                )
                or relative.startswith("src/minder_publish.egg-info/")
                or relative in ("PKG-INFO", "setup.cfg")
            ):
                continue
            canonical = source_root / relative
            if not canonical.is_file() or canonical.read_bytes() != content:
                problems.append(
                    {"code": "stale_or_unowned_artifact_file", "path": name}
                )
    if metadata_prefix:
        metadata = BytesParser().parsebytes(
            entries.get(metadata_prefix + "METADATA", b"")
        )
        if (
            metadata.get("Name") != "minder-publish"
            or metadata.get("Requires-Python") != ">=3.10"
        ):
            problems.append(
                {
                    "code": "invalid_distribution_identity",
                    "path": metadata_prefix + "METADATA",
                }
            )
        points = configparser.ConfigParser()
        points.read_string(
            entries.get(metadata_prefix + "entry_points.txt", b"").decode("utf-8")
        )
        expected = {
            "minder-publish": "minder_publish.client.cli:main",
            "minder-publish-server": "minder_publish.server.commands:main",
            "minder-publish-mcp": "minder_publish.mcp.server:main",
        }
        if client_module:
            expected = {"minder-publish": expected["minder-publish"]}
        if not points.has_section("console_scripts") or any(
            points.get("console_scripts", key, fallback="") != value
            for key, value in expected.items()
        ):
            problems.append(
                {
                    "code": "invalid_entry_points",
                    "path": metadata_prefix + "entry_points.txt",
                }
            )
        wheel = entries.get(metadata_prefix + "WHEEL", b"")
        if b"Tag: py3-none-any" not in wheel or b"Root-Is-Purelib: true" not in wheel:
            problems.append(
                {"code": "nonportable_wheel", "path": metadata_prefix + "WHEEL"}
            )
    resource_prefix = "" if kind == "wheel" else "src/"
    for resource in (
        "minder_publish/defaults.json",
        "minder_publish/server/web/messages.json",
    ):
        key = resource_prefix + resource
        if key in entries:
            try:
                value = json.loads(entries[key])
                if not isinstance(value, dict) or not value:
                    raise ValueError("empty resource")
            except (ValueError, UnicodeError):
                problems.append({"code": "invalid_json_resource", "path": key})
    for font in ("commissioner", "golostext"):
        license_key = (
            resource_prefix + f"minder_publish/server/web/fonts/{font}-OFL.txt"
        )
        font_key = resource_prefix + f"minder_publish/server/web/fonts/{font}.ttf"
        if (
            license_key in entries
            and b"SIL OPEN FONT LICENSE" not in entries[license_key]
        ):
            problems.append({"code": "missing_font_license_text", "path": license_key})
        if font_key in entries and entries[font_key][:4] not in (
            b"\x00\x01\x00\x00",
            b"OTTO",
            b"ttcf",
        ):
            problems.append({"code": "invalid_font_asset", "path": font_key})
    if kind in ("sdist", "git-source"):
        for name, content in entries.items():
            if not name.endswith(".md"):
                continue
            for link in LINK.findall(content.decode("utf-8")):
                target = link.split("#", 1)[0].strip("<>")
                if not target or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                    continue
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(name), target)
                )
                if resolved not in entries and not any(
                    item.startswith(resolved.rstrip("/") + "/") for item in entries
                ):
                    problems.append(
                        {
                            "code": "broken_local_document_link",
                            "path": name,
                            "target": resolved,
                        }
                    )
    for finding in problems:
        for field in ("path", "target"):
            if field in finding:
                finding[field] = CREDENTIAL.sub(
                    "[redacted]", PRIVATE_PATH.sub("[private-path]/", finding[field])
                )
    return {
        "artifact": CREDENTIAL.sub("[redacted]", path.name),
        "kind": kind,
        "files": len(entries),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        **({"git_tree": source_tree} if source_tree else {}),
        "passed": not problems,
        "findings": problems,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="*", type=Path)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="check package files against this checkout, or locate the Git repository",
    )
    parser.add_argument(
        "--git-tree",
        metavar="TREE",
        help="compare one Git source export against this exact tree, commit or tag",
    )
    parser.add_argument(
        "--client-wheel",
        action="store_true",
        help="enforce the anonymous CLI wheel boundary",
    )
    args = parser.parse_args(argv)
    if args.client_wheel and (args.git_tree or not args.artifacts):
        parser.error(
            "--client-wheel requires explicit wheel paths and cannot be used with --git-tree"
        )
    if args.git_tree is not None and len(args.artifacts) != 1:
        parser.error("--git-tree requires exactly one explicit source archive")
    artifacts = args.artifacts or sorted(
        [*Path("dist").glob("*.whl"), *Path("dist").glob("*.tar.gz")]
    )
    if not artifacts:
        print(json.dumps({"passed": False, "findings": [{"code": "no_artifacts"}]}))
        return 1
    results = []
    for artifact in artifacts:
        try:
            results.append(
                audit(
                    artifact,
                    source_root=args.source_root,
                    git_tree=args.git_tree,
                    client_wheel=args.client_wheel,
                )
            )
        except (
            OSError,
            ValueError,
            zipfile.BadZipFile,
            tarfile.TarError,
            RecursionError,
            configparser.Error,
        ) as error:
            results.append(
                {
                    "artifact": CREDENTIAL.sub("[redacted]", artifact.name),
                    "passed": False,
                    "findings": [
                        {
                            "code": "unreadable_artifact",
                            "error_type": type(error).__name__,
                        }
                    ],
                }
            )
    print(
        json.dumps(
            {"passed": all(item["passed"] for item in results), "artifacts": results},
            indent=2,
        )
    )
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
