"""Explicit file/site preparation with bounded storage and portable manifests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import tempfile
import time
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from .errors import ClientError

CHUNK_BYTES = 65536
SENSITIVE_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".ssh",
        ".aws",
        ".gnupg",
        ".env",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "tokens.json",
        "keystore.json",
    }
)
SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"})


@dataclass(frozen=True)
class PreparedFile:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class PreparedContent:
    path: Path
    kind: str
    filename: str
    size_bytes: int
    expanded_size_bytes: int
    files: list[str]
    excluded: list[str]
    manifest: tuple[PreparedFile, ...]
    entry_path: str | None = None

    def manifest_report(self, *, allow_sensitive: bool) -> tuple[dict, str]:
        # Content approval excludes transport ZIP metadata and publication policy.
        manifest = {
            "version": 1,
            "kind": self.kind,
            "filename": self.filename,
            "entry_path": self.entry_path,
            "files": [
                {"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256}
                for item in self.manifest
            ],
            "excluded": sorted(unicodedata.normalize("NFC", path) for path in self.excluded),
            "sensitive_override": allow_sensitive,
        }
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return manifest, hashlib.sha256(encoded).hexdigest()

    def dependency_report(self) -> dict:
        from minder_publish.dependencies import scan_dependencies

        if self.kind == "file":
            manifest = [{"path": self.files[0], "size_bytes": self.size_bytes}]
            return scan_dependencies(
                manifest, lambda path: open_regular(self.path), entry_path=self.files[0]
            )
        with zipfile.ZipFile(self.path) as archive:
            by_path = {
                PurePosixPath(
                    unicodedata.normalize("NFC", item.filename.rstrip("/"))
                ).as_posix(): item
                for item in archive.infolist()
                if not item.is_dir()
            }
            manifest = [
                {"path": path, "size_bytes": by_path[path].file_size}
                for path in self.files
            ]
            return scan_dependencies(
                manifest,
                lambda path: archive.open(by_path[path]),
                entry_path=self.entry_path,
            )

    def chunks(self) -> Iterator[bytes]:
        # Every prepared path is a private completed snapshot, never an author source.
        with open_regular(self.path) as handle:
            total = 0
            while chunk := handle.read(CHUNK_BYTES):
                total += len(chunk)
                if total > self.size_bytes:
                    raise ClientError(
                        "source_changed",
                        "The prepared source grew. Run preflight again.",
                    )
                yield chunk
            if total != self.size_bytes:
                raise ClientError(
                    "source_changed",
                    "The prepared source changed. Run preflight again.",
                )


def sensitive(path: PurePosixPath) -> bool:
    return any(
        part.casefold() in SENSITIVE_NAMES
        or part.casefold().startswith(".env.")
        or Path(part).suffix.casefold() in SENSITIVE_SUFFIXES
        for part in path.parts
    )


def portable_path(value: str, limits: dict, *, field: str = "path") -> str:
    normalized = unicodedata.normalize("NFC", value)
    parts = PurePosixPath(normalized).parts
    invalid = (
        not normalized
        or "\\" in normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or any(
            part in ("", ".", "..") or ":" in part or part.endswith((" ", "."))
            for part in parts
        )
        or any(any(ord(character) < 32 for character in part) for part in parts)
        or len(parts) > limits["path_depth"]
        or len(normalized.encode("utf-8")) > limits["path_utf8_bytes"]
    )
    if invalid:
        raise ClientError(
            "invalid_path",
            f"Invalid portable {field}: {value!r}.",
            field=field,
            action="Choose a relative, normalized path within the selected site.",
        )
    windows_reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if any(part.split(".")[0].upper() in windows_reserved for part in parts):
        raise ClientError(
            "invalid_path", f"Reserved portable {field}: {value!r}.", field=field
        )
    if parts and parts[0] == "__mp":
        raise ClientError(
            "reserved_path", "The /__mp/ namespace is reserved.", field=field
        )
    return PurePosixPath(*parts).as_posix()


def _link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


@contextmanager
def open_regular(path: Path):
    before = path.lstat()
    if _link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ClientError(
            "unsafe_source", "Refusing a symlink or non-regular source file."
        )
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(descriptor, "rb") as handle:
        after = os.fstat(handle.fileno())
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise ClientError("source_changed", "The source changed during preflight.")
        yield handle


def _bounded(value: int, maximum: int, field: str):
    if value > maximum:
        raise ClientError(
            "limit_exceeded",
            f"{field} exceeds the server limit.",
            field=field,
            actual=value,
            limit=maximum,
            action="Reduce the selected files or ask the instance owner to change its limits.",
        )


@contextmanager
def _snapshot(path: Path, expected: os.stat_result, maximum: int, deadline: float, *, digest=None):
    """Freeze bounded bytes before reviewing them; source timestamps are not a content proof."""
    with tempfile.TemporaryDirectory(prefix="minder-publish-") as temporary:
        snapshot = Path(temporary) / "payload"
        total = 0
        with open_regular(path) as source, snapshot.open("xb") as destination:
            initial = os.fstat(source.fileno())
            if (initial.st_dev, initial.st_ino, initial.st_size) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_size,
            ):
                raise ClientError(
                    "source_changed", "The source changed before snapshot preparation."
                )
            while chunk := source.read(CHUNK_BYTES):
                total += len(chunk)
                _bounded(total, min(maximum, expected.st_size), "source_size")
                if time.monotonic() > deadline:
                    raise ClientError(
                        "preflight_timeout",
                        "Snapshot preparation exceeded its deadline.",
                    )
                destination.write(chunk)
                if digest is not None:
                    digest.update(chunk)
        if total != expected.st_size:
            raise ClientError(
                "source_changed", "The source changed during snapshot preparation."
            )
        if time.monotonic() > deadline:
            raise ClientError(
                "preflight_timeout", "Snapshot preparation exceeded its deadline."
            )
        # Nothing reopens this file for writing. Review, scan and upload share its bytes.
        yield snapshot


class _BoundedArchive:
    def __init__(self, file, maximum: int):
        self.file, self.maximum = file, maximum

    def write(self, data):
        _bounded(self.file.tell() + len(data), self.maximum, "upload_payload_bytes")
        return self.file.write(data)

    def __getattr__(self, name):
        return getattr(self.file, name)


def _archive_manifest(
    path: Path, limits: dict, entry: str, allow_sensitive: bool, deadline: float
):
    # Bound central-directory allocation before ZipFile creates Python objects.
    with open_regular(path) as source:
        source.seek(max(0, path.stat().st_size - 65557))
        tail = source.read(65557)
        position = tail.rfind(b"PK\x05\x06")
        if position < 0 or len(tail) - position < 22:
            raise ClientError(
                "invalid_archive",
                "Site mode requires a valid ZIP archive or static directory.",
            )
        _, disk, directory_disk, count_disk, count, directory_size, _, comment_size = (
            struct.unpack_from("<4s4H2LH", tail, position)
        )
        if (
            disk
            or directory_disk
            or count_disk != count
            or position + 22 + comment_size != len(tail)
        ):
            raise ClientError(
                "invalid_archive",
                "Multi-volume or ambiguous ZIP archives are not supported.",
            )
        if count == 65535 or directory_size == 0xFFFFFFFF:
            locator_position = position - 20
            if (
                locator_position < 0
                or tail[locator_position : locator_position + 4] != b"PK\x06\x07"
            ):
                raise ClientError("invalid_archive", "Invalid ZIP64 directory locator.")
            _, zip64_disk, offset, total_disks = struct.unpack_from(
                "<4sLQL", tail, locator_position
            )
            if zip64_disk or total_disks != 1:
                raise ClientError(
                    "invalid_archive", "Multi-volume ZIP64 is not supported."
                )
            source.seek(offset)
            record = source.read(56)
            if len(record) != 56 or record[:4] != b"PK\x06\x06":
                raise ClientError("invalid_archive", "Invalid ZIP64 directory record.")
            count = struct.unpack_from("<Q", record, 32)[0]
            directory_size = struct.unpack_from("<Q", record, 40)[0]
        _bounded(count, limits["site_files"] * 2 + 32, "archive_entries")
        _bounded(
            directory_size,
            limits["site_files"] * (limits["path_utf8_bytes"] + 1024) + 65536,
            "archive_metadata_bytes",
        )
    files, names, expanded = [], set(), 0
    manifest = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if time.monotonic() > deadline:
                    raise ClientError(
                        "preflight_timeout", "Site preparation exceeded its deadline."
                    )
                original_name = info.orig_filename
                normalized = portable_path(original_name.rstrip("/"), limits)
                if normalized.casefold() in names:
                    raise ClientError(
                        "path_collision",
                        "The ZIP has duplicate or colliding portable paths.",
                    )
                names.add(normalized.casefold())
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (
                    stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                ):
                    raise ClientError(
                        "unsafe_source",
                        "The ZIP contains a symlink or non-regular entry.",
                    )
                if info.flag_bits & 1:
                    raise ClientError(
                        "encrypted_archive",
                        "Encrypted site archives are not supported.",
                    )
                if sensitive(PurePosixPath(normalized)) and not allow_sensitive:
                    raise ClientError(
                        "sensitive_source",
                        "The site ZIP contains excluded names. Repackage after reviewing them, or explicitly override exclusions.",
                    )
                if info.is_dir():
                    continue
                files.append(normalized)
                _bounded(len(files), limits["site_files"], "site_files")
                _bounded(info.file_size, limits["file_bytes"], "file_bytes")
                expanded += info.file_size
                _bounded(expanded, limits["site_expanded_bytes"], "site_expanded_bytes")
                received = 0
                digest = hashlib.sha256()
                with archive.open(info) as content:
                    while chunk := content.read(CHUNK_BYTES):
                        received += len(chunk)
                        digest.update(chunk)
                        _bounded(
                            received,
                            min(info.file_size, limits["file_bytes"]),
                            "file_bytes",
                        )
                        if time.monotonic() > deadline:
                            raise ClientError(
                                "preflight_timeout",
                                "Site preparation exceeded its deadline.",
                            )
                if received != info.file_size:
                    raise ClientError(
                        "invalid_archive", "The ZIP has inconsistent entry sizes."
                    )
                manifest.append(PreparedFile(normalized, received, digest.hexdigest()))
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as error:
        raise ClientError(
            "invalid_archive", "The ZIP is corrupt or uses an unsupported format."
        ) from error
    if entry not in files:
        raise ClientError(
            "entry_not_found",
            "The site entry must be an included file.",
            field="entry_path",
        )
    return sorted(files), expanded, tuple(sorted(manifest, key=lambda item: item.path))


@contextmanager
def prepare(
    path: str | Path,
    *,
    kind: str,
    limits: dict,
    entry_path: str = "index.html",
    allow_sensitive: bool = False,
) -> Iterator[PreparedContent]:
    deadline = time.monotonic() + limits.get("prepare_seconds", 300)
    target = Path(path).expanduser()
    if kind not in ("file", "site"):
        raise ClientError(
            "kind_required", "Explicitly choose file or site mode.", field="kind"
        )
    try:
        info = target.lstat()
    except OSError as error:
        raise ClientError(
            "source_unavailable", "The selected source is unavailable.", field="path"
        ) from error
    if _link_or_reparse(info):
        raise ClientError("unsafe_source", "Refusing a symlink source.", field="path")
    if kind == "file":
        if not stat.S_ISREG(info.st_mode):
            raise ClientError(
                "site_mode_required",
                "A directory requires explicit site mode.",
                field="kind",
            )
        _bounded(
            info.st_size,
            min(limits["file_bytes"], limits["upload_payload_bytes"]),
            "file_bytes",
        )
        if sensitive(PurePosixPath(target.name)) and not allow_sensitive:
            raise ClientError(
                "sensitive_source",
                "This filename is excluded by default. Review it before an explicit override.",
                field="path",
            )
        filename = portable_path(target.name, limits, field="filename")
        digest = hashlib.sha256()
        with _snapshot(
            target,
            info,
            min(limits["file_bytes"], limits["upload_payload_bytes"]),
            deadline,
            digest=digest,
        ) as snapshot:
            yield PreparedContent(
                snapshot,
                "file",
                filename,
                info.st_size,
                info.st_size,
                [filename],
                [],
                (PreparedFile(filename, info.st_size, digest.hexdigest()),),
            )
        return
    entry = portable_path(entry_path, limits, field="entry_path")
    if stat.S_ISREG(info.st_mode):
        _bounded(info.st_size, limits["upload_payload_bytes"], "upload_payload_bytes")
        with _snapshot(
            target, info, limits["upload_payload_bytes"], deadline
        ) as snapshot:
            files, expanded, manifest = _archive_manifest(
                snapshot, limits, entry, allow_sensitive, deadline
            )
            yield PreparedContent(
                snapshot,
                "site",
                portable_path(target.name, limits, field="filename"),
                info.st_size,
                expanded,
                files,
                [],
                manifest,
                entry,
            )
        return
    if not stat.S_ISDIR(info.st_mode):
        raise ClientError(
            "site_directory_required",
            "Site mode requires a static directory or ZIP.",
            field="path",
        )
    included: list[tuple[str, Path, os.stat_result]] = []
    excluded: list[str] = []
    names: set[str] = set()
    expanded = 0
    for root, directories, filenames in os.walk(target, followlinks=False):
        if time.monotonic() > deadline:
            raise ClientError(
                "preflight_timeout", "Site preparation exceeded its deadline."
            )
        root_path = Path(root)
        for name in sorted(directories + filenames):
            candidate = root_path / name
            relative = candidate.relative_to(target).as_posix()
            is_directory = name in directories
            if sensitive(PurePosixPath(relative)) and not allow_sensitive:
                excluded.append(relative + ("/" if is_directory else ""))
                if is_directory:
                    directories.remove(name)
                continue
            candidate_info = candidate.lstat()
            if _link_or_reparse(candidate_info):
                raise ClientError(
                    "unsafe_source",
                    f"Refusing symlink/reparse entry: {relative}.",
                    field="path",
                )
            normalized = portable_path(relative, limits)
            collision = normalized.casefold()
            if collision in names:
                raise ClientError(
                    "path_collision",
                    f"Portable path collision: {relative}.",
                    field="path",
                )
            names.add(collision)
            if is_directory:
                continue
            if not stat.S_ISREG(candidate_info.st_mode):
                raise ClientError(
                    "unsafe_source",
                    f"Non-regular site entry: {relative}.",
                    field="path",
                )
            _bounded(candidate_info.st_size, limits["file_bytes"], "file_bytes")
            expanded += candidate_info.st_size
            _bounded(expanded, limits["site_expanded_bytes"], "site_expanded_bytes")
            included.append((normalized, candidate, candidate_info))
            _bounded(len(included), limits["site_files"], "site_files")
    included.sort(key=lambda row: row[0])
    files = [row[0] for row in included]
    if entry not in files:
        raise ClientError(
            "entry_not_found",
            "The site entry must be an included file.",
            field="entry_path",
        )
    with tempfile.TemporaryDirectory(prefix="minder-publish-") as temporary:
        archive_path = Path(temporary) / "site.zip"
        manifest = []
        with archive_path.open("w+b") as output:
            with zipfile.ZipFile(
                _BoundedArchive(output, limits["upload_payload_bytes"]),
                "w",
                zipfile.ZIP_DEFLATED,
            ) as archive:
                for relative, source, original in included:
                    count = 0
                    digest = hashlib.sha256()
                    with (
                        open_regular(source) as input_file,
                        archive.open(relative, "w", force_zip64=True) as destination,
                    ):
                        current = os.fstat(input_file.fileno())
                        if (
                            current.st_dev,
                            current.st_ino,
                            current.st_size,
                            current.st_mtime_ns,
                        ) != (
                            original.st_dev,
                            original.st_ino,
                            original.st_size,
                            original.st_mtime_ns,
                        ):
                            raise ClientError(
                                "source_changed",
                                "A site source changed after its manifest was checked.",
                            )
                        while chunk := input_file.read(CHUNK_BYTES):
                            count += len(chunk)
                            _bounded(count, original.st_size, "source_size")
                            if time.monotonic() > deadline:
                                raise ClientError(
                                    "preflight_timeout",
                                    "Site preparation exceeded its deadline.",
                                )
                            destination.write(chunk)
                            digest.update(chunk)
                    if count != original.st_size:
                        raise ClientError(
                            "source_changed",
                            "A site source changed during archive creation.",
                        )
                    manifest.append(PreparedFile(relative, count, digest.hexdigest()))
        yield PreparedContent(
            archive_path,
            "site",
            portable_path(target.name + ".zip", limits, field="filename"),
            archive_path.stat().st_size,
            expanded,
            files,
            sorted(excluded),
            tuple(manifest),
            entry,
        )
