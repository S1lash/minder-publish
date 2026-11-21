"""Bounded immutable local storage. Untrusted names never become unchecked paths."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import struct
import time
import unicodedata
import zipfile
from pathlib import Path

from minder_publish.errors import PublishError

CHUNK_BYTES = 1 << 20
_ZIP_END = struct.Struct("<4s4H2LH")
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP64_END = struct.Struct("<4sQ2H2L4Q")
_ZIP_DIRECTORY_HEADER_BYTES = 46
_ZIP_MAX_COMMENT_BYTES = 65535
_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
MAX_CUSTOM_KEY_CHARACTERS = 64
_CUSTOM_KEY = re.compile(rf"^[a-z0-9][a-z0-9-]{{1,{MAX_CUSTOM_KEY_CHARACTERS - 1}}}$")
_RESERVED_KEYS = frozenset(
    {
        "api",
        "admin",
        "health",
        "static",
        "assets",
        "unlock",
        "login",
        "oauth",
        "mcp",
        "metrics",
        "healthz",
        "downloads",
        "upload",
        "viewer",
        "p",
    }
)


def safe_id(value):
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise PublishError("invalid_id", 400)
    return value


def custom_key(value):
    """Validate newly chosen keys without restricting existing imported identifiers."""
    if not isinstance(value, str) or not _CUSTOM_KEY.fullmatch(value):
        raise PublishError("invalid_key", 400, field="key")
    if value in _RESERVED_KEYS:
        raise PublishError("reserved_key", 400, field="key")
    return value


def normalize_path(value: str, limits: dict, *, reserved=True) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PublishError("unsafe_path", 400, field="path")
    value = unicodedata.normalize("NFC", value)
    parts = value.split("/")
    if any(
        p in ("", ".", "..") or ":" in p or any(ord(c) < 32 for c in p) for p in parts
    ):
        raise PublishError("unsafe_path", 400, field="path")
    if reserved and parts[0].casefold() == "__mp":
        raise PublishError("reserved_path", 400, field="path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise PublishError("unsafe_path", 400, field="path") from exc
    if len(encoded) > limits["path_utf8_bytes"] or len(parts) > limits["path_depth"]:
        raise PublishError("path_limit", 413, field="path")
    return value


def content_type(path: str) -> str:
    extension = Path(path).suffix.lower()
    fixed = {
        ".html": "text/html",
        ".htm": "text/html",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".xml": "application/xml",
        ".md": "text/plain",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".zip": "application/zip",
        ".wasm": "application/wasm",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }
    return fixed.get(
        extension, mimetypes.guess_type(path)[0] or "application/octet-stream"
    )


def preflight_zip(source: Path, limits: dict, metadata_limit: int):
    """Bound directory bytes and records before ZipFile allocates either."""
    size = source.stat().st_size
    entry_limit = limits["site_files"] * 2
    tail_limit = _ZIP_END.size + _ZIP_MAX_COMMENT_BYTES
    with source.open("rb") as stream:
        stream.seek(max(0, size - tail_limit))
        tail = stream.read(tail_limit)
        last_record = len(tail) - _ZIP_END.size
        if (
            last_record >= 0
            and tail.startswith(b"PK\x05\x06", last_record)
            and tail[-2:] == b"\x00\x00"
        ):
            index = last_record
        else:
            index = tail.rfind(b"PK\x05\x06")
        if index < 0 or index > last_record:
            raise PublishError("invalid_archive", 400)
        (
            _,
            disk,
            directory_disk,
            disk_entries,
            entries,
            directory_size,
            directory_offset,
            comment,
        ) = _ZIP_END.unpack_from(tail, index)
        if (
            index + _ZIP_END.size + comment != len(tail)
            or disk
            or directory_disk
            or disk_entries != entries
        ):
            raise PublishError("unsupported_archive", 400)
        directory_end = size - len(tail) + index
        locator_position = directory_end - _ZIP64_LOCATOR.size
        locator = b""
        if locator_position >= 0:
            stream.seek(locator_position)
            locator = stream.read(_ZIP64_LOCATOR.size)
        # A ZIP64 locator takes precedence even with ordinary legacy field values.
        if locator.startswith(b"PK\x06\x07"):
            directory_end, entries, directory_size, directory_offset = _zip64_directory(
                stream, locator_position, locator
            )
        elif (
            entries == 65535
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
        ):
            raise PublishError("unsupported_archive", 400)
        if entries > entry_limit or directory_size > metadata_limit:
            raise PublishError("archive_metadata_limit", 413, limit=metadata_limit)
        start = directory_end - directory_size
        if start < directory_offset:
            raise PublishError("invalid_archive", 400)
        _count_zip_directory(
            stream, start, directory_end, entries, entry_limit, metadata_limit
        )


def _zip64_directory(stream, locator_position, locator):
    _, locator_disk, locator_offset, disks = _ZIP64_LOCATOR.unpack(locator)
    position = locator_position - _ZIP64_END.size
    if locator_disk or disks != 1 or locator_offset > position:
        raise PublishError("unsupported_archive", 400)
    stream.seek(position)
    (
        signature,
        record_size,
        _,
        _,
        disk,
        directory_disk,
        disk_entries,
        entries,
        directory_size,
        directory_offset,
    ) = _ZIP64_END.unpack(stream.read(_ZIP64_END.size))
    if (
        signature != b"PK\x06\x06"
        or record_size != _ZIP64_END.size - 12
        or disk
        or directory_disk
        or disk_entries != entries
        or directory_offset + directory_size != locator_offset
    ):
        raise PublishError("unsupported_archive", 400)
    if locator_offset != position:
        # A prefixed ZIP64 archive can use relative offsets. Reject ambiguous
        # candidates so the reader cannot select a different ZIP64 end record.
        stream.seek(locator_offset)
        if stream.read(4) == b"PK\x06\x06":
            raise PublishError("unsupported_archive", 400)
    return position, entries, directory_size, directory_offset


def _count_zip_directory(
    stream, position, end, declared_entries, entry_limit, metadata_limit
):
    entries = 0
    while position < end:
        if entries >= entry_limit:
            raise PublishError("archive_metadata_limit", 413, limit=metadata_limit)
        if end - position < _ZIP_DIRECTORY_HEADER_BYTES:
            raise PublishError("invalid_archive", 400)
        stream.seek(position)
        header = stream.read(_ZIP_DIRECTORY_HEADER_BYTES)
        if len(header) != _ZIP_DIRECTORY_HEADER_BYTES or not header.startswith(
            b"PK\x01\x02"
        ):
            raise PublishError("invalid_archive", 400)
        name_size, extra_size, comment_size = struct.unpack_from("<3H", header, 28)
        position += _ZIP_DIRECTORY_HEADER_BYTES + name_size + extra_size + comment_size
        entries += 1
    if position != end or entries != declared_entries:
        raise PublishError("invalid_archive", 400)


def iter_bytes(root: Path):
    """Observe each regular file once, retaining directory depth rather than a whole tree."""
    root = Path(root)

    def visit(directory):
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            return
        with entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    yield Path(entry.path).relative_to(root).as_posix(), info.st_size
                elif stat.S_ISDIR(info.st_mode):
                    yield from visit(entry.path)

    yield from visit(root)


def tree_bytes(root: Path) -> int:
    return sum(size for _, size in iter_bytes(root))


def fsync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root: Path):
    """Flush child directory entries before their parent without sorting a whole tree."""
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                fsync_tree(Path(entry.path))
    fsync_directory(root)


class Storage:
    def __init__(self, config):
        self.config = config
        self.revisions = Path(config.revisions_dir)
        self.staging = Path(config.staging_dir)

    def initialize(self):
        Path(self.config.data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        Path(self.config.data_dir).chmod(0o700)
        self.revisions.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.revisions.chmod(0o700)
        self.staging.chmod(0o700)
        if self.revisions.stat().st_dev != self.staging.stat().st_dev:
            raise PublishError(
                "storage_filesystem_mismatch",
                409,
                "Staging and revisions must share a filesystem.",
            )

    def stage(self, upload_id):
        return self.staging / safe_id(upload_id)

    def revision(self, revision_id):
        return self.revisions / safe_id(revision_id)

    def allocation(self, upload_id, revision_id):
        return tree_bytes(self.stage(upload_id)) + tree_bytes(
            self.revision(revision_id)
        )

    def remove_stage(self, upload_id):
        try:
            shutil.rmtree(self.stage(upload_id))
        except FileNotFoundError:
            pass

    def prepare(self, upload_id, plan, limits, deadline, heartbeat):
        stage = self.stage(upload_id)
        source = stage / "payload"
        content = stage / "prepared" / "content"
        content.mkdir(parents=True, exist_ok=False)
        manifest = []
        total = 0
        expanded_limit = plan.get("expanded_size_bytes", limits["site_expanded_bytes"])

        def copy_file(stream, name):
            nonlocal total
            name = normalize_path(name, limits)
            destination = content.joinpath(*name.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with destination.open("xb") as output:
                destination.chmod(0o600)
                while True:
                    if time.monotonic() > deadline:
                        raise PublishError("prepare_timeout", 408)
                    chunk = stream.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    maximum = (
                        expanded_limit
                        if plan["kind"] == "site"
                        else limits["file_bytes"]
                    )
                    if total > maximum:
                        raise PublishError(
                            "expanded_size_limit"
                            if plan["kind"] == "site"
                            else "file_size_limit",
                            413,
                            actual=total,
                            limit=maximum,
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    heartbeat()
                output.flush()
                os.fsync(output.fileno())
            manifest.append(
                {
                    "path": name,
                    "size_bytes": size,
                    "content_type": content_type(name),
                    "sha256": digest.hexdigest(),
                }
            )

        if plan["kind"] == "file":
            with source.open("rb") as stream:
                copy_file(stream, plan["filename"])
        else:
            preflight_zip(
                source, limits, self.config.operations["export_metadata_bytes"]
            )
            try:
                with zipfile.ZipFile(source) as archive:
                    entries = archive.infolist()
                    if len(entries) > limits["site_files"] * 2:
                        raise PublishError("file_count_limit", 413)
                    names = set()
                    files = []
                    for info in entries:
                        if "\x00" in info.orig_filename:
                            raise PublishError("unsafe_path", 400, field="path")
                        raw = info.filename[:-1] if info.is_dir() else info.filename
                        name = normalize_path(raw, limits)
                        folded = name.casefold()
                        if folded in names:
                            raise PublishError("duplicate_path", 400, field="path")
                        names.add(folded)
                        mode = info.external_attr >> 16
                        kind = stat.S_IFMT(mode)
                        if (
                            kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                            or (kind == stat.S_IFDIR and not info.is_dir())
                            or info.flag_bits & 1
                        ):
                            raise PublishError(
                                "unsupported_archive_entry", 400, field="path"
                            )
                        if info.is_dir():
                            continue
                        files.append((info, name))
                        if len(files) > limits["site_files"]:
                            raise PublishError(
                                "file_count_limit", 413, limit=limits["site_files"]
                            )
                    # Reject file/directory collisions before writing anything.
                    file_names = {name.casefold() for _, name in files}
                    for name in file_names:
                        if any(
                            "/".join(name.split("/")[:n]) in file_names
                            for n in range(1, len(name.split("/")))
                        ):
                            raise PublishError("duplicate_path", 400)
                    for info, name in files:
                        with archive.open(info) as stream:
                            copy_file(stream, name)
            except (
                zipfile.BadZipFile,
                RuntimeError,
                NotImplementedError,
                EOFError,
            ) as exc:
                raise PublishError(
                    "invalid_archive",
                    400,
                    "The site archive is corrupt or unsupported.",
                ) from exc
            entry = normalize_path(plan["entry_path"], limits)
            if entry not in {item["path"] for item in manifest}:
                raise PublishError("entry_missing", 400, field="entry_path")
        manifest_path = stage / "prepared" / "manifest.json"
        with manifest_path.open("xb") as output:
            manifest_path.chmod(0o600)
            metadata_bytes = 0
            encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
            for piece in encoder.iterencode(manifest):
                encoded = piece.encode("utf-8")
                metadata_bytes += len(encoded)
                if metadata_bytes > self.config.operations["export_metadata_bytes"]:
                    raise PublishError("manifest_metadata_limit", 413)
                output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        fsync_tree(content)
        fsync_directory(stage / "prepared")
        return manifest, total

    def promote(self, upload_id, revision_id):
        target = self.revision(revision_id)
        if target.exists():
            raise PublishError("revision_exists", 409)
        os.rename(self.stage(upload_id) / "prepared", target)
        fsync_directory(self.revisions)
        return target
