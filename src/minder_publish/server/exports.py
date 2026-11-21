"""Bounded streaming ZIP exports with no temporary archive on disk."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import zipfile

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from minder_publish.errors import PublishError


async def site_export(core, config, publication):
    revision = await run_in_threadpool(
        core.get_revision, publication["current_revision"]
    )
    metadata = sum(
        len(info["path"].encode("utf-8")) * 2 + 256 for info in revision["files"]
    )
    if metadata > config.operations["export_metadata_bytes"]:
        raise PublishError(
            "export_metadata_limit",
            413,
            limit=config.operations["export_metadata_bytes"],
        )
    total = sum(info["size_bytes"] for info in revision["files"])
    if (
        total + config.operations["site_export_overhead_bytes"]
        > config.ceilings["site_export_bytes"]
    ):
        raise PublishError(
            "export_size_limit", 413, limit=config.ceilings["site_export_bytes"]
        )
    limits = (await run_in_threadpool(core.policy_settings))["limits"]

    class ExportResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            lease = await run_in_threadpool(
                core.acquire_lease, revision["id"], "export"
            )
            refreshed = time.monotonic()

            async def progress(message):
                nonlocal refreshed
                if time.monotonic() - refreshed > 20:
                    await run_in_threadpool(core.renew_lease, lease)
                    refreshed = time.monotonic()
                async with asyncio.timeout(limits["export_idle_seconds"]):
                    await send(message)

            try:
                async with asyncio.timeout(limits["export_total_seconds"]):
                    await super().__call__(scope, receive, progress)
            finally:
                try:
                    await asyncio.shield(iterator.aclose())
                finally:
                    await asyncio.shield(run_in_threadpool(core.release_lease, lease))

    async def archive():
        channel = queue.Queue(maxsize=1)
        stopped = threading.Event()

        class Writer:
            position = 0

            def write(self, data):
                for offset in range(
                    0, len(data), config.operations["export_buffer_bytes"]
                ):
                    item = bytes(
                        memoryview(data)[
                            offset : offset + config.operations["export_buffer_bytes"]
                        ]
                    )
                    while not stopped.is_set():
                        try:
                            channel.put(item, timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    else:
                        raise InterruptedError("Export disconnected")
                self.position += len(data)
                return len(data)

            def tell(self):
                return self.position

            def flush(self):
                pass

        def produce():
            try:
                with zipfile.ZipFile(
                    Writer(), "w", compression=zipfile.ZIP_STORED, allowZip64=True
                ) as output:
                    for info in revision["files"]:
                        if stopped.is_set():
                            return
                        source, validated = core.resolve_file(
                            revision["id"], info["path"]
                        )
                        entry = zipfile.ZipInfo(info["path"])
                        entry.compress_type = zipfile.ZIP_STORED
                        entry.external_attr = 0o100600 << 16
                        with (
                            source.open("rb") as stream,
                            output.open(entry, "w", force_zip64=True) as target,
                        ):
                            while data := stream.read(
                                config.operations["export_buffer_bytes"]
                            ):
                                target.write(data)
                final = None
            except BaseException as error:
                final = error
            while not stopped.is_set():
                try:
                    channel.put(final, timeout=0.1)
                    return
                except queue.Full:
                    continue

        worker = threading.Thread(target=produce, name="publish-export", daemon=True)
        worker.start()
        try:
            while True:
                deadline = time.monotonic() + limits["export_idle_seconds"]
                while True:
                    try:
                        item = channel.get_nowait()
                        break
                    except queue.Empty:
                        if time.monotonic() >= deadline:
                            raise PublishError("export_idle_timeout", 408)
                        await asyncio.sleep(0.01)
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stopped.set()
            await asyncio.to_thread(worker.join, 2)

    iterator = archive()
    return ExportResponse(
        iterator,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="publication-{publication["id"]}.zip"',
            "Content-Security-Policy": "default-src 'none'",
        },
    )
