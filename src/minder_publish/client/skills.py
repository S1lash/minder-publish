"""Install a managed skill with immutable references and an atomic entry update."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sysconfig
import tempfile
from pathlib import Path

from .config import _safe_name, executable_path, validate_base_url
from .errors import ClientError
from .preflight import open_regular, portable_path

MARKER = "<!-- minder-publish-managed-skill -->"
RUNTIMES = {"codex": ".agents", "claude": ".claude"}
INSTALLATION_REFERENCE = "references/installation.md"
MAX_FILES = 64
MAX_ENTRIES = 256
MAX_FILE_BYTES = 256 * 1024
MAX_SKILL_BYTES = 1024 * 1024
CACHE = ".minder-publish"
GENERATION_MANIFEST = ".minder-publish-files.json"
OWNER = b'{"format":"minder-publish-managed-skill","version":1}\n'
LINK = re.compile(r"(!?\[[^\]]*\]\()([^\s)]+)([^)]*\))")


def canonical_source() -> Path:
    packaged = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "minder-publish"
        / "skills"
        / "minder-publish"
        / "SKILL.md"
    )
    checkout = (
        Path(__file__).resolve().parents[3] / "skill" / "minder-publish" / "SKILL.md"
    )
    for candidate in (packaged, checkout):
        if candidate.is_file():
            return candidate
    raise ClientError(
        "skill_source_unavailable",
        "Install a complete Minder Publish wheel or use the source release installer.",
    )


def _safe_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or getattr(
        info, "st_file_attributes", 0
    ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ClientError(
            "unsafe_skill_target", "Refusing a symlink or non-directory skill path."
        )


def _read_file(path: Path) -> bytes:
    with open_regular(path) as handle:
        content = handle.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ClientError("skill_size_limit", "A skill file exceeds 256 KiB.")
    return content


def _read_tree(root: Path, *, internal: bool = False) -> dict[str, bytes]:
    _safe_directory(root)
    if not root.is_dir():
        raise ClientError("skill_source_unavailable", "The skill directory is absent.")
    files: dict[str, bytes] = {}
    names = set()
    entries = total = 0
    for parent, directories, filenames in os.walk(root, followlinks=False):
        entries += len(directories) + len(filenames)
        if entries > MAX_ENTRIES:
            raise ClientError("skill_size_limit", "The skill has too many entries.")
        for directory in directories:
            _safe_directory(Path(parent) / directory)
        for filename in sorted(filenames):
            path = Path(parent) / filename
            relative = portable_path(
                path.relative_to(root).as_posix(),
                {"path_utf8_bytes": 1024, "path_depth": 16},
                field="skill path",
            )
            if relative == GENERATION_MANIFEST and internal:
                pass
            elif any(part.startswith(".") for part in Path(relative).parts):
                raise ClientError(
                    "unsafe_skill_source", "Hidden skill files are not distributable."
                )
            if relative.casefold() in names:
                raise ClientError(
                    "unsafe_skill_source",
                    "Skill paths collide on a portable filesystem.",
                )
            names.add(relative.casefold())
            content = _read_file(path)
            total += len(content)
            if len(files) >= MAX_FILES + int(internal) or total > MAX_SKILL_BYTES + (
                MAX_FILE_BYTES if internal else 0
            ):
                raise ClientError(
                    "skill_size_limit", "The skill exceeds its file or byte budget."
                )
            files[relative] = content
    return dict(sorted(files.items()))


def read_skill_files(source: Path | None = None) -> dict[str, bytes]:
    """Read one bounded, portable, symlink-free canonical skill tree."""
    source = Path(source) if source is not None else canonical_source()
    if source.name == "SKILL.md":
        if source.is_symlink():
            raise ClientError("unsafe_skill_source", "Refusing a symlink skill entry.")
        source = source.parent
    files = _read_tree(source)
    try:
        content = files["SKILL.md"].decode("utf-8")
    except (KeyError, UnicodeDecodeError) as error:
        raise ClientError(
            "unmanaged_skill_source", "A UTF-8 SKILL.md entry is required."
        ) from error
    if MARKER not in content:
        raise ClientError(
            "unmanaged_skill_source",
            "The source is not the canonical managed Minder Publish skill.",
        )
    return files


def _version(files: dict[str, bytes]) -> tuple[str, bytes]:
    hashes = {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }
    manifest = (
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    return hashlib.sha256(manifest).hexdigest(), manifest


def runtime_skill_files(files: dict[str, bytes], runtime: str) -> dict[str, bytes]:
    """Render the Claude namespace without changing portable canonical naming."""
    if runtime not in RUNTIMES:
        raise ClientError("unknown_runtime", "Choose codex or claude.")
    result = dict(files)
    if runtime == "claude":
        entry = result["SKILL.md"].decode("utf-8")
        result["SKILL.md"] = re.sub(
            r"(?m)^name: minder-publish(\r?\n)",
            r"name: minder:publish\1",
            entry,
            count=1,
        ).encode("utf-8")
    return result


def _installed_skill_files(
    files: dict[str, bytes], runtime: str, instance_name: str | None, instance_url: str | None
) -> dict[str, bytes]:
    result = runtime_skill_files(files, runtime)
    if INSTALLATION_REFERENCE in result:
        executable = str(executable_path())
        command = [executable]
        if instance_name is not None:
            command.extend(["--instance", instance_name])
            selection = (
                f"Preferred instance for this skill: {instance_name}.\n"
                f"Expected server URL: {instance_url}.\n\n"
                "Always include the explicit --instance argument shown below; the CLI's global default may be another server. "
                "Before publishing, run doctor and verify its base_url equals this expected URL. "
                "If it differs, stop and resolve the configuration or environment override before sending files. "
                "The most recent setup selects this skill's preferred instance without changing the CLI's global default. "
                "Use another instance only when the user selects it, then verify that server with doctor first.\n\n"
            )
        else:
            selection = (
                "No instance is bound to this skill installation. Before publishing, explicitly choose a configured "
                "instance and run doctor with --instance NAME to confirm the intended server. "
                "Do not silently rely on a global default for an unspecified destination.\n\n"
            )
        if os.name == "nt":
            shell = "powershell"
            shell_command = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in command)
        else:
            shell = "sh"
            shell_command = shlex.join(command)
        result[INSTALLATION_REFERENCE] = (
            "# Local CLI installation\n\n"
            "Generated installation metadata by Minder Publish. This reference replaces "
            "the portable installation reference for this machine only; it contains no credentials.\n\n"
            "Use this installed executable even when the agent's PATH does not contain it. "
            "Replace `minder-publish` in the CLI examples with the executable and instance arguments below, "
            "keeping the remaining operation arguments.\n\n"
            + selection
            + f"```{shell}\n{shell_command} doctor --brief --json\n```\n\n"
            "If this path is unavailable in the execution environment, use the instance's "
            "Connection instructions to install there. Do not search private profiles or "
            "request a device token in chat.\n"
        ).encode("utf-8")
    return result


def _legacy_entry(home: Path, runtime: str) -> tuple[Path | None, bytes]:
    if runtime != "codex":
        return None, b""
    target = home / ".codex" / "skills" / "minder-publish" / "SKILL.md"
    previous = _check_target(home, target)
    if target.exists() and MARKER.encode() not in previous:
        raise ClientError(
            "unmanaged_skill_target",
            "The legacy Codex skill is unmanaged. Review and move it explicitly before installing.",
        )
    return (target, previous) if previous else (None, b"")


def _entry(files: dict[str, bytes], version: str) -> bytes:
    def rewrite(match):
        value = match[2]
        if value.startswith(("https:", "http:", "mailto:", "#")):
            return match[0]
        path, separator, fragment = value.partition("#")
        normalized = portable_path(path, {"path_utf8_bytes": 1024, "path_depth": 16})
        if normalized not in files:
            raise ClientError(
                "missing_skill_reference",
                "The skill links to a missing local resource.",
            )
        return f"{match[1]}{CACHE}/versions/{version}/{normalized}{separator}{fragment}{match[3]}"

    return LINK.sub(rewrite, files["SKILL.md"].decode("utf-8")).encode("utf-8")


def _check_target(home: Path, target: Path) -> bytes:
    current = home
    _safe_directory(current)
    for part in target.parent.relative_to(home).parts:
        current = current / part
        _safe_directory(current)
    if target.is_symlink():
        raise ClientError("unsafe_skill_target", "Refusing a symlink skill entry.")
    previous = _read_file(target) if target.exists() else b""
    if target.exists() and MARKER.encode() not in previous:
        raise ClientError(
            "unmanaged_skill_target",
            "The destination contains an unmanaged skill. Review and move it explicitly before installing.",
        )
    cache = target.parent / CACHE
    _safe_directory(cache)
    if cache.exists():
        owner = cache / "owner.json"
        if not owner.is_file() or owner.is_symlink() or _read_file(owner) != OWNER:
            raise ClientError(
                "unmanaged_skill_target",
                "The reserved skill cache is not installer-managed.",
            )
        _safe_directory(cache / "versions")
    return previous


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(
        prefix=".minder-publish-entry-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _verify_generation(generation: Path, expected: dict[str, bytes]) -> None:
    try:
        matches = _read_tree(generation, internal=True) == dict(
            sorted(expected.items())
        )
    except (ClientError, OSError):
        matches = False
    if not matches:
        raise ClientError(
            "modified_skill_generation",
            "An immutable skill generation was modified; review it before reinstalling.",
        )


def _ensure_generation(
    target: Path, version: str, files: dict[str, bytes], manifest: bytes
) -> None:
    cache = target.parent / CACHE
    cache.mkdir(exist_ok=True)
    owner = cache / "owner.json"
    if not owner.exists():
        _atomic_write(owner, OWNER)
    versions = cache / "versions"
    versions.mkdir(exist_ok=True)
    generation = versions / version
    expected = {**files, GENERATION_MANIFEST: manifest}
    if generation.exists() or generation.is_symlink():
        _verify_generation(generation, expected)
        return
    staging = Path(tempfile.mkdtemp(prefix=".minder-publish-version-", dir=versions))
    try:
        for name, content in expected.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        try:
            os.replace(staging, generation)
        except OSError:
            if not generation.exists():
                raise
            _verify_generation(generation, expected)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def claude_directory(override: Path | str | None = None) -> Path | None:
    value = override if override is not None else os.getenv("CLAUDE_CONFIG_DIR")
    if not value:
        return None
    directory = Path(os.path.abspath(Path(value).expanduser()))
    if directory.is_relative_to(Path.home().absolute()):
        return directory
    # Pin the parent of an explicit profile outside the home boundary. The
    # profile directory and the managed skill tree still get symlink checks.
    try:
        return directory.parent.resolve() / directory.name
    except (OSError, RuntimeError) as error:
        raise ClientError("unsafe_skill_target", "Cannot resolve the Claude configuration directory.") from error


def install(
    source: Path, *, runtimes: list[str], home: Path | None = None, apply: bool = False,
    instance_name: str | None = None, instance_url: str | None = None,
    claude_config_dir: Path | None = None,
) -> list[dict]:
    if (instance_name is None) != (instance_url is None):
        raise ClientError("invalid_skill_instance", "The skill instance requires both its name and server URL.")
    if instance_name is not None:
        instance_name = _safe_name(instance_name)
        instance_url = validate_base_url(instance_url)
    files = read_skill_files(source)
    claude_config_dir = (
        claude_directory(claude_config_dir) if home is None and "claude" in runtimes else None
    )
    home = Path(home or Path.home()).expanduser().absolute()
    plans = []
    snapshots = {}
    for runtime in dict.fromkeys(runtimes):
        if runtime not in RUNTIMES:
            raise ClientError("unknown_runtime", "Choose codex or claude.")
        rendered = _installed_skill_files(files, runtime, instance_name, instance_url)
        version, manifest = _version(rendered)
        content = _entry(rendered, version)
        runtime_directory = home / RUNTIMES[runtime]
        target_root = home
        if runtime == "claude" and claude_config_dir is not None:
            runtime_directory = claude_config_dir
            if not runtime_directory.is_relative_to(home):
                target_root = runtime_directory
        target = runtime_directory / "skills" / "minder-publish" / "SKILL.md"
        previous = _check_target(target_root, target)
        legacy, legacy_previous = _legacy_entry(home, runtime)
        snapshots[runtime] = (target_root, previous, target.exists(), legacy, legacy_previous)
        generation = target.parent / CACHE / "versions" / version
        if generation.exists() or generation.is_symlink():
            _verify_generation(generation, {**rendered, GENERATION_MANIFEST: manifest})
        diff = "".join(
            difflib.unified_diff(
                previous.decode("utf-8").splitlines(keepends=True),
                content.decode("utf-8").splitlines(keepends=True),
                fromfile=str(target),
                tofile=str(target),
            )
        )
        plans.append(
            {
                "runtime": runtime,
                "instance": instance_name,
                "base_url": instance_url,
                "path": str(target),
                "changed": previous != content or legacy is not None,
                "diff": diff,
                "applied": bool(apply and (previous != content or legacy is not None)),
                "migrates_from": str(legacy) if legacy else None,
                "generation": version,
                "files": list(files),
                "retains_previous_generations": True,
            }
        )
    if apply:
        for plan in plans:
            rendered = _installed_skill_files(files, plan["runtime"], instance_name, instance_url)
            version, manifest = _version(rendered)
            content = _entry(rendered, version)
            target = Path(plan["path"])
            target_root, previous, existed, legacy, legacy_previous = snapshots[plan["runtime"]]
            if (
                _check_target(target_root, target) != previous
                or _legacy_entry(home, plan["runtime"]) != (legacy, legacy_previous)
            ):
                raise ClientError(
                    "skill_target_changed",
                    "The skill changed after planning. Review and retry.",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            _ensure_generation(target, version, rendered, manifest)
            if previous != content:
                _atomic_write(target, content)
            if legacy is not None:
                try:
                    if _check_target(home, legacy) != legacy_previous:
                        raise ClientError(
                            "skill_target_changed",
                            "The legacy skill changed during installation.",
                        )
                    legacy.unlink()
                except (ClientError, OSError):
                    if previous != content:
                        if existed:
                            _atomic_write(target, previous)
                        else:
                            target.unlink()
                    raise
    return plans
