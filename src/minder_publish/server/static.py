"""Revalidated, precompressed packaged UI assets, separate from author content."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles


def accepts_gzip(value: str) -> bool:
    qualities = {}
    for item in value.lower().split(","):
        encoding, *parameters = item.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            name, separator, raw = parameter.strip().partition("=")
            if name == "q" and separator:
                try:
                    quality = float(raw)
                except ValueError:
                    quality = 0.0
        qualities[encoding] = quality if 0 <= quality <= 1 else 0.0
    return qualities.get("gzip", qualities.get("*", 0)) > 0


def etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest() + '"'


class PackagedStaticFiles(StaticFiles):
    """Assets belong to an immutable application release; changes require restart."""

    def __init__(self, directory):
        super().__init__(directory=directory)
        self.assets = {}
        for path in Path(directory).rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            body = path.read_bytes()
            compressed = gzip.compress(body, mtime=0) if len(body) >= 1024 else b""
            if len(compressed) >= len(body):
                compressed = b""
            self.assets[str(path.resolve())] = (
                etag(body), compressed, etag(compressed) if compressed else None
            )

    def file_response(self, full_path, stat_result, scope, status_code=200):
        identity_tag, compressed, compressed_tag = self.assets[str(Path(full_path).resolve())]
        request = Headers(scope=scope)
        use_gzip = bool(compressed) and "range" not in request and accepts_gzip(
            request.get("accept-encoding", "")
        )
        selected_tag = compressed_tag if use_gzip else identity_tag
        headers = {
            "Cache-Control": "private, no-cache",
            "ETag": selected_tag,
            "Vary": "Accept-Encoding",
        }
        # Only the content digest validates a release, never its copied mtime.
        candidates = request.get("if-none-match", "")
        if candidates.strip() == "*" or selected_tag in (
            candidate.strip().removeprefix("W/") for candidate in candidates.split(",")
        ):
            return Response(status_code=304, headers=headers)
        response = FileResponse(
            full_path, stat_result=stat_result, status_code=status_code, headers=headers
        )
        if not use_gzip:
            return response
        headers.update({
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed)),
            "Content-Type": response.headers["content-type"],
        })
        return Response(
            compressed if scope["method"] != "HEAD" else b"",
            status_code=status_code,
            headers=headers,
        )
