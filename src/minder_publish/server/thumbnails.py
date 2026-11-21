"""Raster-only decoding in a bounded disposable subprocess; no page execution."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path


SITE_COVER_PATHS = ("publish-cover.png", "publish-cover.jpg", "publish-cover.webp")
RASTER_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp")


def thumbnail_source(kind: str, manifest: list[dict]) -> str | None:
    if kind == "file" and len(manifest) == 1:
        entry = manifest[0]
        return entry["path"] if entry["content_type"] in RASTER_CONTENT_TYPES else None
    if kind == "site":
        paths = {entry["path"] for entry in manifest}
        return next((path for path in SITE_COVER_PATHS if path in paths), None)
    return None


def generate_thumbnail(source: Path, target: Path, limits: dict) -> bool:
    if source.stat().st_size > limits["source_bytes"]:
        return False
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "minder_publish.server.thumbnails",
                "--worker",
                str(source),
                str(target),
                json.dumps(limits),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=limits["decoder_seconds"] + 1,
            check=False,
        )
        return result.returncode == 0 and target.is_file()
    finally:
        Path(str(target) + ".tmp").unlink(missing_ok=True)


def _worker(source, target, limits):
    # Server deployment is Linux. Refuse decoding if enforceable resource limits are absent.
    import resource

    resource.setrlimit(
        resource.RLIMIT_AS,
        (limits["decoder_memory_bytes"], limits["decoder_memory_bytes"]),
    )
    cpu = max(1, math.ceil(limits["decoder_seconds"]))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (limits["output_bytes"], limits["output_bytes"])
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    import socket

    def denied(*args, **kwargs):
        raise OSError("Network APIs are disabled during thumbnail decoding")

    socket.socket = denied
    socket.create_connection = denied
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = limits["source_pixels"]
    formats = [name.upper() for name in limits["source_formats"]]
    with Image.open(source, formats=formats) as original:
        if getattr(original, "n_frames", 1) != 1:
            return 1
        width, height = original.size
        if (
            width * height > limits["source_pixels"]
            or max(width, height) > limits["source_dimension"]
        ):
            return 1
        image = ImageOps.exif_transpose(original)
        image.thumbnail(
            (limits["output_width"], limits["output_height"]), Image.Resampling.LANCZOS
        )
        if image.mode != "RGB":
            image = image.convert("RGB")
        temporary = Path(str(target) + ".tmp")
        temporary.touch(mode=0o600, exist_ok=False)
        try:
            for quality in (80, 60, 40, 20):
                image.save(temporary, "JPEG", quality=quality, optimize=False, exif=b"")
                if 4 <= temporary.stat().st_size <= limits["output_bytes"]:
                    # A native encoder can return after an RLIMIT_FSIZE-shortened write.
                    # Size alone is not proof of a complete image: require EOI and a full decode.
                    with temporary.open("rb") as saved:
                        if saved.read(2) != b"\xff\xd8":
                            continue
                        saved.seek(-2, os.SEEK_END)
                        if saved.read(2) != b"\xff\xd9":
                            continue
                    try:
                        with Image.open(temporary) as verified:
                            verified.load()
                            if (
                                verified.format != "JPEG"
                                or verified.mode != "RGB"
                                or verified.size != image.size
                            ):
                                continue
                    except (OSError, ValueError):
                        continue
                    with temporary.open("rb") as saved:
                        os.fsync(saved.fileno())
                    os.replace(temporary, target)
                    return 0
            return 1
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    try:
        result = _worker(Path(sys.argv[2]), Path(sys.argv[3]), json.loads(sys.argv[4]))
    except Exception:
        result = 1
    raise SystemExit(result)
