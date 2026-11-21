"""Named instances and private device-token files, without owner-specific defaults."""

from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import sysconfig
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import ClientError
from .preflight import open_regular

LOCK_TIMEOUT_SECONDS = 2.0
LOCK_RETRY_SECONDS = 0.025
MAX_SECRET_FILE_BYTES = 8193

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


@dataclass(frozen=True)
class Connection:
    name: str
    base_url: str
    token: str


def executable_path() -> Path:
    """Locate this installation without assuming its scripts directory is on PATH."""
    return Path(sysconfig.get_path("scripts")) / (
        "minder-publish.exe" if os.name == "nt" else "minder-publish"
    )


def config_directory() -> Path:
    if os.environ.get("MINDER_PUBLISH_CONFIG_DIR"):
        return Path(os.environ["MINDER_PUBLISH_CONFIG_DIR"]).expanduser()
    if os.name == "nt":
        return (
            Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
            / "minder-publish"
        )
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "minder-publish"
    )


def validate_base_url(value: str, *, allow_http: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ClientError(
            "invalid_base_url", "Enter a valid instance URL.", field="url"
        ) from error
    local = parsed.hostname == "localhost" or bool(
        parsed.hostname and parsed.hostname.endswith(".localhost")
    )
    try:
        local = local or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        pass
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and not (local or allow_http))
    ):
        raise ClientError(
            "invalid_base_url",
            "Use an HTTPS instance URL without credentials, query or fragment; local HTTP is allowed.",
            field="url",
        )
    host = parsed.hostname
    authority = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), "", ""))


def read_config() -> dict:
    directory = config_directory()
    for name in ("config.json", "config.toml"):
        path = directory / name
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8")
            data = (
                json.loads(content)
                if path.suffix == ".json"
                else tomllib.loads(content)
            )
            if not isinstance(data, dict) or not isinstance(
                data.get("instances", {}), dict
            ):
                raise ValueError("invalid schema")
            if data.get("version", 1) != 1:
                raise ClientError(
                    "unsupported_config_version",
                    "The client configuration version is not supported.",
                )
            return data
        except (OSError, ValueError) as error:
            raise ClientError(
                "invalid_config",
                "Client configuration is unreadable or invalid.",
                field="config",
            ) from error
    return {"version": 1, "default_instance": None, "instances": {}}


def _safe_name(name: str) -> str:
    if (
        not name
        or len(name) > 64
        or any(
            not (character.isascii() and (character.isalnum() or character in "-_"))
            for character in name
        )
    ):
        raise ClientError(
            "invalid_instance_name",
            "Instance names use 1–64 ASCII letters, digits, hyphens or underscores.",
        )
    return name


def _write_private(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ClientError("unsafe_config", "Refusing a symlink configuration target.")
    descriptor, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _check_lock_node(info, *, directory: bool):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if not expected(info.st_mode) or reparse:
        raise ClientError("unsafe_config", "The configuration lock contains a symlink or an unexpected file type.")
    if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ClientError("unsafe_config", "Configuration locks must be private and owned by the current user.")
    if not directory and info.st_nlink != 1:
        raise ClientError("unsafe_config", "Refusing a hard-linked configuration lock.")


def _configuration_lock_directory() -> Path:
    if os.name == "nt":
        # Match credential storage: Windows relies on the user's profile ACLs.
        # Do not use a machine-wide TEMP directory or follow profile reparse points.
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))).expanduser()
        for parent in (base, *base.parents):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ClientError("unsafe_config", "The user-profile lock path contains a reparse point or non-directory.")
        paths = (base / "minder-publish", base / "minder-publish" / "config-locks")
    else:
        # A fixed OS-temp namespace keeps processes with different TMPDIR values
        # on the same lock. Never remove lock files: unlinking can split waiters.
        paths = (Path("/tmp").resolve() / f"minder-publish-locks-{os.geteuid()}",)
    for path in paths:
        path.mkdir(mode=0o700, exist_ok=True)
        _check_lock_node(path.lstat(), directory=True)
    return paths[-1]


def _wait_for_configuration_lock(acquire):
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            acquire()
            return
        except OSError as error:
            windows_error = getattr(error, "winerror", None)
            contention = (
                windows_error == 33
                if os.name == "nt" and windows_error is not None
                else error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
            )
            if not contention:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError(
                    "configuration_busy",
                    "Timed out waiting for another client to finish using configuration.",
                    limit=LOCK_TIMEOUT_SECONDS,
                    action="Retry the command after the other configuration operation finishes.",
                ) from error
            time.sleep(min(LOCK_RETRY_SECONDS, remaining))


@contextmanager
def configuration_lock():
    """Snapshot profile URL/token together, without writing into the profile mount."""
    directory = _configuration_lock_directory()
    identity = os.path.normcase(str(config_directory().resolve()))
    key = hashlib.sha256(os.fsencode(identity)).hexdigest()
    path = directory / (key + ".lock")
    try:
        _check_lock_node(path.lstat(), directory=False)
    except FileNotFoundError:
        pass
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        opened = os.fstat(descriptor)
        _check_lock_node(opened, directory=False)
        current = path.lstat()
        _check_lock_node(current, directory=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ClientError("unsafe_config", "The configuration lock changed while opening it.")
        if os.name == "nt":
            import msvcrt

            if opened.st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            acquire = partial(msvcrt.locking, descriptor, msvcrt.LK_NBLCK, 1)
            release = partial(msvcrt.locking, descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            acquire = partial(fcntl.flock, descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = partial(fcntl.flock, descriptor, fcntl.LOCK_UN)
        _wait_for_configuration_lock(acquire)
        locked = True
        yield
    finally:
        try:
            if locked:
                release()
        finally:
            os.close(descriptor)


def save_instance(
    name: str, base_url: str, token: str, *, make_default: bool = True,
    require_new: bool = False,
) -> dict:
    with configuration_lock():
        return _save_instance(name, base_url, token, make_default=make_default, require_new=require_new)


def _save_instance(name, base_url, token, *, make_default, require_new):
    name = _safe_name(name)
    base_url = validate_base_url(base_url)
    token = token.strip()
    if (
        not token
        or len(token) > 8192
        or any(character.isspace() for character in token)
    ):
        raise ClientError(
            "invalid_token",
            "Provide a nonempty device token without whitespace.",
            field="token",
        )
    directory = config_directory()
    data = read_config()
    instances = data.setdefault("instances", {})
    previous = instances.get(name)
    if require_new and name in instances:
        raise ClientError("instance_conflict", "This instance is already configured. Run setup again to verify it; credentials were not changed.")
    token_file = f"tokens/{name}-{secrets.token_hex(16)}.token"
    token_path = directory / token_file
    instances[name] = {"base_url": base_url, "token_file": token_file}
    data["version"] = 1
    if make_default or not data.get("default_instance"):
        data["default_instance"] = name
    profile_data = json.dumps(data, indent=2) + "\n"
    _write_private(token_path, token + "\n")
    try:
        _write_private(directory / "config.json", profile_data)
    except (OSError, ClientError):
        with suppress(OSError):
            token_path.unlink(missing_ok=True)
        raise

    # A profile can point to an operator-managed file or share a credential.
    old_token = previous.get("token_file") if isinstance(previous, dict) else None
    if isinstance(old_token, str) and re.fullmatch(
        rf"tokens/{re.escape(name)}-[0-9a-f]{{32}}\.token", old_token
    ):
        with suppress(OSError, RuntimeError, ValueError):
            old_path = directory / old_token
            referenced_paths = {
                _token_path(directory, profile["token_file"]).resolve()
                for profile in instances.values()
                if isinstance(profile, dict) and isinstance(profile.get("token_file"), str)
            }
            if old_path.resolve() not in referenced_paths:
                old_path.unlink(missing_ok=True)
    return {
        "instance": name,
        "base_url": base_url,
        "token_stored": True,
        "credential_protection": "owner-only file permissions"
        if os.name != "nt"
        else "private user profile; verify Windows ACLs",
    }


def read_private_secret_file(path: Path, *, field: str = "token") -> str:
    description = "Device-token" if field == "token" else "Publication-password"
    try:
        with open_regular(path) as source:
            info = os.fstat(source.fileno())
            if os.name != "nt" and info.st_mode & 0o077:
                raise ClientError(
                    f"unsafe_{field}_file",
                    f"{description} file permissions must be owner-only.",
                    action="Set the file permissions to 0600.",
                )
            raw = source.read(MAX_SECRET_FILE_BYTES + 1)
            if len(raw) > MAX_SECRET_FILE_BYTES:
                raise ClientError(f"invalid_{field}", f"{description} file is too large.")
            value = raw.decode("utf-8")
    except ClientError as error:
        if error.code not in ("unsafe_source", "source_changed"):
            raise
        raise ClientError(
            f"unsafe_{field}_file",
            f"{description} must be stored in a stable regular private file.",
        ) from error
    except UnicodeDecodeError as error:
        raise ClientError(
            f"invalid_{field}", f"{description} file must contain UTF-8 text."
        ) from error
    except OSError as error:
        raise ClientError(
            f"{field}_unavailable", f"{description} file is unavailable."
        ) from error
    # A text file's final line terminator is not part of the secret; spaces are.
    return value.replace("\r\n", "\n").replace("\r", "\n").removesuffix("\n")


def _read_token(path: Path) -> str:
    value = read_private_secret_file(path).strip()
    if not value or any(character.isspace() for character in value):
        raise ClientError("invalid_token", "Device-token file must contain one token.")
    return value


def _token_path(directory: Path, token_file: str) -> Path:
    return directory / Path(token_file).expanduser()


def connection(name: str | None = None, *, use_environment: bool = True) -> Connection:
    environment = dict(os.environ) if use_environment else {}
    selected = name or environment.get("MINDER_PUBLISH_INSTANCE")
    base_url = environment.get("MINDER_PUBLISH_URL")
    token = environment.get("MINDER_PUBLISH_TOKEN")
    token_file = environment.get("MINDER_PUBLISH_TOKEN_FILE")
    if base_url or token or token_file:
        if not base_url or not (token or token_file):
            raise ClientError(
                "incomplete_environment_connection",
                "Set MINDER_PUBLISH_URL together with MINDER_PUBLISH_TOKEN or MINDER_PUBLISH_TOKEN_FILE.",
                action="Provide both environment settings or unset them to use a saved instance.",
            )
        return _connection(selected or "environment", base_url, token, token_file)
    with configuration_lock():
        data = read_config()
        selected = selected or data.get("default_instance")
        profile = data.get("instances", {}).get(selected, {})
        if not isinstance(profile, dict):
            raise ClientError("invalid_config", "The selected instance configuration is invalid.")
        return _connection(selected, profile.get("base_url"), None, profile.get("token_file"))


def _connection(name, base_url, token, token_file):
    if not base_url:
        raise ClientError(
            "not_configured",
            "Configure an instance with minder-publish configure or MINDER_PUBLISH_URL and MINDER_PUBLISH_TOKEN.",
        )
    base_url = validate_base_url(base_url)
    if not token and token_file:
        token = _read_token(_token_path(config_directory(), token_file))
    if not token:
        raise ClientError(
            "not_authenticated",
            "Provide a device token through stdin setup, MINDER_PUBLISH_TOKEN or a private token file.",
        )
    if len(token) > 8192 or any(character.isspace() for character in token):
        raise ClientError(
            "invalid_token", "The device token is invalid.", field="token"
        )
    return Connection(name, base_url, token)


def redacted_config() -> dict:
    data = read_config()
    return {
        "default_instance": data.get("default_instance"),
        "instances": {
            name: {
                "base_url": item.get("base_url"),
                "credential_configured": bool(item.get("token_file")),
            }
            for name, item in data.get("instances", {}).items()
        },
    }
