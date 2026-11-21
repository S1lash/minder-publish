"""Optional, operator-allowlisted host file inputs; no ambient URL fetching.

DNS validation and the pinned TLS connection are one operation. Source bytes go
straight into an authenticated, reserved upload, never into an unaccounted spool.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from minder_publish.client.api import (
    LINK_NAMES_UPLOAD_FIELD,
    identifier,
    link_name_upload_fields,
)
from minder_publish.client.errors import ClientError
from minder_publish.client.preflight import CHUNK_BYTES, portable_path, sensitive

_DNS_SLOTS = threading.BoundedSemaphore(4)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_CONTROL_FIELDS = frozenset(
    {
        "kind",
        "filename",
        "size_bytes",
        "expanded_size_bytes",
        "publication_id",
        "expected_revision",
        "entry_path",
        LINK_NAMES_UPLOAD_FIELD,
    }
)


def _failure(code, detail, **kwargs):
    return ClientError(code, detail, **kwargs)


def _dns_name(value):
    if not isinstance(value, str) or value != value.strip() or value.endswith("."):
        raise ValueError("Use an exact DNS hostname without a URL, port or wildcard.")
    try:
        name = value.encode("idna").decode("ascii").lower()
        ipaddress.ip_address(name)
    except UnicodeError:
        raise ValueError("Use a valid DNS hostname.") from None
    except ValueError:
        pass
    else:
        raise ValueError("IP address allowlists are not supported.")
    if (
        len(name) > 253
        or "." not in name
        or any(not _DNS_LABEL.fullmatch(label) for label in name.split("."))
    ):
        raise ValueError("Use an exact DNS hostname without a URL, port or wildcard.")
    return name


@dataclass(frozen=True)
class _Target:
    host: str
    path: str
    deadline_seconds: float = 120


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout, *, deadline_seconds=120):
        super().__init__(
            host, port=443, timeout=timeout, context=ssl.create_default_context()
        )
        self.address = address
        self.expired = threading.Event()
        self.timer = threading.Timer(deadline_seconds, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self):
        self.expired.set()
        current = self.sock
        if current is not None:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            current.close()

    def close(self):
        self.timer.cancel()
        super().close()

    def connect(self):
        # No proxy lookup and no second DNS resolution. Certificate verification
        # and TLS SNI still use the allowlisted hostname, never the pinned IP.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = raw
            if self.expired.is_set():
                raise TimeoutError()
            self.sock = self._context.wrap_socket(
                raw, server_hostname=self.host, do_handshake_on_connect=False
            )
            self.sock.do_handshake()
            if self.expired.is_set():
                raise TimeoutError()
        except BaseException:
            self.close()
            raw.close()
            raise


class _Response:
    def __init__(self, connection, response):
        self.connection, self.response = connection, response
        self.status = response.status

    def getheaders(self):
        return self.response.getheaders()

    def set_timeout(self, timeout):
        self.connection.sock.settimeout(timeout)

    def read(self, count):
        return self.response.read(count)

    def close(self):
        self.response.close()
        self.connection.close()


def _open(target, address, timeout):
    connection = _PinnedHTTPSConnection(
        target.host, address, timeout, deadline_seconds=target.deadline_seconds
    )
    try:
        connection.request(
            "GET",
            target.path,
            headers={"Accept-Encoding": "identity", "Connection": "close"},
        )
        # Keep socket ownership until streaming finishes, including for HTTP/1.0
        # and Connection: close responses. This permits per-read deadline updates.
        response = http.client.HTTPResponse(connection.sock, method="GET")
        response.begin()
        return _Response(connection, response)
    except BaseException:
        connection.close()
        raise


class HostFiles:
    def __init__(
        self,
        allowed_hosts,
        *,
        resolver=None,
        opener=None,
        clock=time.monotonic,
        read_timeout=10,
        total_seconds=120,
    ):
        if not isinstance(allowed_hosts, tuple):
            raise ValueError(
                "Provide a tuple of exact operator-approved DNS hostnames."
            )
        self.allowed_hosts = frozenset(_dns_name(host) for host in allowed_hosts)
        if not 0 < read_timeout <= 60 or not 0 < total_seconds <= 3600:
            raise ValueError("Use positive, bounded host file deadlines.")
        self.resolver, self.opener = resolver or socket.getaddrinfo, opener or _open
        self.clock, self.read_timeout, self.total_seconds = (
            clock,
            read_timeout,
            total_seconds,
        )

    def _remaining(self, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise _failure(
                "host_file_timeout", "The host file transfer exceeded its deadline."
            )
        return remaining

    def _target(self, file):
        if not self.allowed_hosts:
            raise _failure(
                "host_files_disabled", "This instance has no approved host file source."
            )
        if not isinstance(file, dict) or set(file) - {
            "download_url",
            "file_id",
            "file_name",
            "mime_type",
        }:
            raise _failure(
                "invalid_host_file",
                "Provide a host file object with its file identifier and download URL.",
            )
        if (
            not isinstance(file.get("file_id"), str)
            or not 1 <= len(file["file_id"]) <= 1024
            or any(ord(c) < 32 for c in file["file_id"])
        ):
            raise _failure(
                "invalid_host_file", "The host file identifier is missing or invalid."
            )
        value = file.get("download_url")
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 8192
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
        ):
            raise _failure(
                "invalid_host_file_url", "Use a bounded, encoded HTTPS host file URL."
            )
        try:
            parsed = urlsplit(value)
            host = _dns_name(parsed.hostname or "")
            valid = (
                parsed.scheme == "https"
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
            )
        except ValueError:
            valid, host = False, ""
        if not valid or host not in self.allowed_hosts or "@" in parsed.netloc:
            raise _failure(
                "invalid_host_file_url",
                "The file URL must use an approved HTTPS source on port 443, without credentials or a fragment.",
            )
        for key in ("file_name", "mime_type"):
            if key in file and (
                not isinstance(file[key], str)
                or not 1 <= len(file[key]) <= 1024
                or any(ord(c) < 32 for c in file[key])
            ):
                raise _failure(
                    "invalid_host_file", "The host file name or media type is invalid."
                )
        return _Target(
            host, (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        )

    def _address(self, target, deadline):
        # libc DNS has no portable timeout. A bounded number of daemon workers
        # prevents stalled resolution from growing unbounded threads or queues.
        if not _DNS_SLOTS.acquire(blocking=False):
            raise _failure(
                "host_file_source_busy",
                "Host file resolution is busy. Try again later.",
            )
        ready, result = threading.Event(), []

        def resolve():
            try:
                result.append(
                    self.resolver(
                        target.host,
                        443,
                        type=socket.SOCK_STREAM,
                        proto=socket.IPPROTO_TCP,
                    )
                )
            except Exception:
                result.append(None)
            finally:
                _DNS_SLOTS.release()
                ready.set()

        threading.Thread(
            target=resolve, daemon=True, name="publish-host-file-dns"
        ).start()
        if not ready.wait(min(self.read_timeout, self._remaining(deadline))):
            raise _failure(
                "host_file_timeout",
                "The host file source did not resolve within its deadline.",
            )
        self._remaining(deadline)
        answers = result[0]
        if not isinstance(answers, (list, tuple)) or not answers or len(answers) > 64:
            raise _failure(
                "unsafe_host_file_address",
                "The host file source has no acceptable public address.",
            )
        addresses = []
        for answer in answers:
            try:
                family, kind, protocol, _, endpoint = answer
                address = ipaddress.ip_address(endpoint[0])
                valid = (
                    family in (socket.AF_INET, socket.AF_INET6)
                    and address.version == (4 if family == socket.AF_INET else 6)
                    and kind == socket.SOCK_STREAM
                    and protocol in (0, socket.IPPROTO_TCP)
                    and endpoint[1] == 443
                    and address.is_global
                    and not address.is_multicast
                    and not address.is_reserved
                    and not address.is_unspecified
                    and not getattr(address, "ipv4_mapped", None)
                    and not getattr(address, "sixtofour", None)
                    and not getattr(address, "teredo", None)
                    and not getattr(address, "scope_id", None)
                    and not (
                        family == socket.AF_INET6
                        and (len(endpoint) < 4 or endpoint[2:] != (0, 0))
                    )
                    and not (
                        address.version == 6
                        and (
                            address in ipaddress.ip_network("64:ff9b::/96")
                            or address in ipaddress.ip_network("64:ff9b:1::/48")
                        )
                    )
                )
            except (ValueError, TypeError, IndexError):
                valid = False
            if not valid:
                raise _failure(
                    "unsafe_host_file_address",
                    "Every resolved host file address must be globally routable.",
                )
            addresses.append(str(address))
        return addresses[0]

    def _chunks(self, source, size, deadline):
        received = 0
        while received < size:
            chunk = self._read(source, min(CHUNK_BYTES, size - received), deadline)
            if (
                not isinstance(chunk, bytes)
                or not chunk
                or len(chunk) > min(CHUNK_BYTES, size - received)
            ):
                raise _failure(
                    "host_file_size_mismatch",
                    "The host file did not match its declared size.",
                )
            received += len(chunk)
            yield chunk
        if self._read(source, 1, deadline):
            raise _failure(
                "host_file_size_mismatch", "The host file exceeded its declared size."
            )

    def _read(self, source, count, deadline):
        try:
            source.set_timeout(min(self.read_timeout, self._remaining(deadline)))
            chunk = source.read(count)
            self._remaining(deadline)
            return chunk
        except TimeoutError:
            raise _failure(
                "host_file_timeout",
                "The host file source exceeded its transfer deadline.",
            ) from None
        except (OSError, http.client.HTTPException):
            self._remaining(deadline)
            # Normalize before an in-process HTTP transport sees this exception;
            # otherwise it can mistake a source socket error for server disk I/O.
            raise _failure(
                "host_file_transfer_failed",
                "The host file transfer failed. Request a new host file reference before retrying.",
            ) from None

    def publish(self, client, file, *, kind, metadata=None, entry_path="index.html"):
        target = self._target(file)
        if (
            kind not in ("file", "site")
            or (metadata is not None and not isinstance(metadata, dict))
            or _CONTROL_FIELDS.intersection(metadata or {})
        ):
            raise _failure(
                "invalid_host_file",
                "Choose file or site and provide publication metadata without upload control fields.",
            )
        deadline = self.clock() + self.total_seconds
        address = self._address(target, deadline)
        capabilities = client.request(
            "GET", "capabilities", timeout=self._remaining(deadline)
        )
        limits = capabilities.get("limits") if isinstance(capabilities, dict) else None
        if not isinstance(limits, dict) or any(
            type(limits.get(key)) is not int or limits[key] <= 0
            for key in (
                "file_bytes",
                "upload_payload_bytes",
                "path_depth",
                "path_utf8_bytes",
                "site_files",
            )
        ):
            raise _failure(
                "invalid_capabilities",
                "The instance returned incomplete upload limits.",
                status=502,
            )
        filename = file.get(
            "file_name", "artifact.zip" if kind == "site" else "artifact.bin"
        )
        filename = self._portable(filename, limits, "filename")
        if "/" in filename or any(c in filename for c in '<>"|?*'):
            raise _failure(
                "invalid_host_file",
                "Choose a portable file basename, without directory components.",
            )
        if sensitive(PurePosixPath(filename)):
            raise _failure(
                "sensitive_host_file",
                "The host file name identifies sensitive material. Publish a reviewed, sanitized result.",
            )
        plan = {**(metadata or {}), "kind": kind, "filename": filename}
        plan.update(link_name_upload_fields(capabilities))
        if kind == "site":
            plan["entry_path"] = self._portable(entry_path, limits, "entry_path")
        source, upload_id = None, None
        try:
            target = replace(target, deadline_seconds=self._remaining(deadline))
            source = self.opener(
                target, address, min(self.read_timeout, self._remaining(deadline))
            )
            self._remaining(deadline)
            headers = source.getheaders()
            lengths = [
                value for key, value in headers if key.lower() == "content-length"
            ]
            forbidden = any(
                key.lower() in ("content-encoding", "transfer-encoding")
                for key, _ in headers
            )
            maximum = (
                min(limits["file_bytes"], limits["upload_payload_bytes"])
                if kind == "file"
                else limits["upload_payload_bytes"]
            )
            if (
                source.status != 200
                or forbidden
                or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,20}", lengths[0])
                or not 0 <= int(lengths[0]) <= maximum
            ):
                raise _failure(
                    "invalid_host_file_response",
                    "The host file source must return an uncompressed, bounded response with one Content-Length and no redirect.",
                )
            plan["size_bytes"] = int(lengths[0])
            upload = client.request(
                "POST", "uploads", json=plan, timeout=self._remaining(deadline)
            )
            upload_id = identifier(upload["id"])
            with closing(self._chunks(source, plan["size_bytes"], deadline)) as chunks:
                client.request(
                    "PUT",
                    f"uploads/{upload_id}/content",
                    content=chunks,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(plan["size_bytes"]),
                    },
                    timeout=self._remaining(deadline),
                )
            source.close()
            source = None
            inspection = client.request(
                "POST",
                f"uploads/{upload_id}/inspect",
                json={},
                timeout=self._remaining(deadline),
            )
            if (
                not isinstance(inspection, dict)
                or inspection.get("state") != "prepared"
                or not isinstance(inspection.get("files"), list)
                or not isinstance(inspection.get("dependency_report"), dict)
                or len(inspection["files"]) > limits["site_files"]
            ):
                raise _failure(
                    "invalid_inspection",
                    "The instance returned an invalid prepared upload report.",
                    status=502,
                )
            for item in inspection["files"]:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise _failure(
                        "invalid_inspection",
                        "The instance returned an invalid file manifest.",
                        status=502,
                    )
                if sensitive(
                    PurePosixPath(self._portable(item["path"], limits, "path"))
                ):
                    raise _failure(
                        "sensitive_host_file",
                        "The prepared site contains a sensitive filename. Publish a reviewed, sanitized result.",
                    )
            commit_timeout = self._remaining(deadline)
        except BaseException as error:
            if upload_id:
                try:
                    client.request(
                        "DELETE", f"uploads/{upload_id}", timeout=self.read_timeout
                    )
                except Exception:
                    pass  # Revocation or network failure leaves the server lease to reclaim it.
            if isinstance(error, (TimeoutError, socket.timeout)):
                raise _failure(
                    "host_file_timeout",
                    "The host file source exceeded its transfer deadline.",
                ) from None
            if isinstance(error, (OSError, http.client.HTTPException)):
                self._remaining(deadline)
                raise _failure(
                    "host_file_transfer_failed",
                    "The host file transfer failed. Request a new host file reference before retrying.",
                ) from None
            raise
        finally:
            if source is not None:
                source.close()
        # The shared commit boundary preserves unknown outcomes without deleting
        # or retrying; keep it outside the known pre-commit cleanup above.
        result = client.commit_upload(upload_id, timeout=commit_timeout)
        result["dependency_report"] = inspection["dependency_report"]
        return result

    @staticmethod
    def _portable(value, limits, field):
        if isinstance(value, str):
            try:
                return portable_path(value, limits, field=field)
            except ClientError:
                pass
        # Host-supplied names can accidentally contain a signed download URL.
        # Reuse the shared validator without echoing its untrusted source value.
        raise _failure(
            "invalid_host_file",
            "The host file contains a non-portable filename or entry path.",
            field=field,
        ) from None
