"""Bounded static HTML/CSS hints. Never execute code, resolve DNS, or fetch a URL.

The opener accepts a validated manifest path and returns a binary context manager;
clients can provide a directory or ZipFile opener without installing server extras.
"""

from __future__ import annotations

import ipaddress
import math
import posixpath
import re
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

DEFAULT_LIMITS = {
    "total_bytes": 16 * 1024**2,
    "file_bytes": 4 * 1024**2,
    "files": 128,
    "seconds": 5,
    "external_origins": 128,
    "missing_assets": 64,
}


class ScanDeadline(Exception):
    pass


class _Scanner:
    def __init__(self, manifest, opener, base_origin, entry_path, limits, clock):
        self.manifest = manifest
        self.paths = {item["path"] for item in manifest}
        self.opener = opener
        self.base_origin = self.origin(base_origin) or "https://publication.invalid"
        self.entry_path = entry_path
        self.limits = limits
        self.clock = clock
        self.deadline = clock() + limits["seconds"]
        self.external = set()
        self.missing = set()
        self.reasons = {"dynamic_behavior_not_analyzed"}
        self.truncated = False
        self.scanned_files = 0
        self.scanned_bytes = 0

    def check(self):
        if self.clock() >= self.deadline:
            self.truncated = True
            self.reasons.add("scan_deadline")
            raise ScanDeadline()

    @staticmethod
    def origin(value):
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in ("https", "http") or not parsed.hostname:
                return None
            host = parsed.hostname
            if ":" in host:
                host = "[" + str(ipaddress.IPv6Address(host)) + "]"
            else:
                host = host.encode("idna").decode("ascii").lower()
                if len(host) > 253 or not re.fullmatch(r"[a-z0-9.-]+", host):
                    return None
            port = parsed.port
            suffix = (
                f":{port}"
                if port and port != (443 if parsed.scheme.lower() == "https" else 80)
                else ""
            )
            return f"{parsed.scheme.lower()}://{host}{suffix}"
        except (ValueError, UnicodeError):
            return None

    def reference(self, value, document_url):
        self.check()
        value = value.strip()
        if not value or value.startswith("#"):
            return
        try:
            original = urlsplit(value)
            if original.scheme.lower() in (
                "data",
                "blob",
                "javascript",
                "mailto",
                "tel",
                "about",
            ):
                return
            resolved = urljoin(document_url, value)
            parsed = urlsplit(resolved)
            origin = self.origin(resolved)
            if not origin:
                self.reasons.add("unsupported_reference")
                return
            if origin != self.base_origin:
                if (
                    origin not in self.external
                    and len(self.external) >= self.limits["external_origins"]
                ):
                    self.truncated = True
                    self.reasons.add("external_origin_limit")
                else:
                    self.external.add(origin)
                return
            # A malformed encoded path cannot cause a filesystem access: lookup is only in the manifest set.
            path = unicodedata.normalize("NFC", unquote(parsed.path, errors="strict"))
            path = posixpath.normpath("/" + path.lstrip("/")).lstrip("/")
            if path == ".":
                path = ""
            if not path and self.entry_path in self.paths:
                return
            if path in self.paths or (path.rstrip("/") + "/index.html") in self.paths:
                return
            if len(path.encode("utf-8")) > 1024 or "\x00" in path:
                self.reasons.add("reference_path_limit")
                return
            if path and path not in self.missing:
                if len(self.missing) >= self.limits["missing_assets"]:
                    self.truncated = True
                    self.reasons.add("missing_asset_limit")
                else:
                    self.missing.add(path)
        except (ValueError, UnicodeError):
            self.reasons.add("malformed_reference")

    def quoted(self, text, start):
        quote = text[start]
        position = start + 1
        while position < len(text):
            if position % 1024 == 0:
                self.check()
            if text[position] == "\\":
                position += 2
            elif text[position] == quote:
                return text[start + 1 : position], position + 1
            else:
                position += 1
        self.reasons.add("unparsed_css")
        return None, len(text)

    @staticmethod
    def css_unescape(value):
        def replacement(match):
            if match.group(1):
                point = int(match.group(1), 16)
                return chr(point) if 0 < point <= 0x10FFFF else "\ufffd"
            return match.group(2) or ""

        return re.sub(
            r"\\([0-9a-fA-F]{1,6})(?:\r\n|\s)?|\\([^\r\n])|\\(?:\r\n|\r|\n)",
            replacement,
            value,
        )

    def css(self, text, document_url):
        # A forward-only tokenizer avoids catastrophic regex backtracking on malformed url(...).
        lower = text.lower()
        position = 0
        while position < len(text):
            if position % 1024 == 0:
                self.check()
            if text.startswith("/*", position):
                closing = text.find("*/", position + 2)
                if closing < 0:
                    self.reasons.add("unparsed_css")
                    return
                position = closing + 2
                continue
            if text[position] in ("'", '"'):
                _, position = self.quoted(text, position)
                continue
            if lower.startswith("@import", position):
                following = position + 7
                while following < len(text) and text[following].isspace():
                    following += 1
                if following < len(text) and text[following] in ("'", '"'):
                    value, position = self.quoted(text, following)
                    if value is not None:
                        self.reference(self.css_unescape(value), document_url)
                    continue
            if lower.startswith("url", position) and (
                position == 0
                or not (text[position - 1].isalnum() or text[position - 1] in "_-")
            ):
                following = position + 3
                while following < len(text) and text[following].isspace():
                    following += 1
                if following < len(text) and text[following] == "(":
                    following += 1
                    while following < len(text) and text[following].isspace():
                        following += 1
                    if following < len(text) and text[following] in ("'", '"'):
                        value, end = self.quoted(text, following)
                        while end < len(text) and text[end].isspace():
                            end += 1
                        if value is None or end >= len(text) or text[end] != ")":
                            self.reasons.add("unparsed_css")
                            position = max(end, following + 1)
                            continue
                    else:
                        end = following
                        while end < len(text):
                            if end % 1024 == 0:
                                self.check()
                            if text[end] == "\\":
                                end += 2
                            elif text[end] == ")":
                                break
                            else:
                                end += 1
                        if end >= len(text):
                            self.reasons.add("unparsed_css")
                            return
                        value = text[following:end].strip()
                    self.reference(self.css_unescape(value), document_url)
                    position = end + 1
                    continue
            if text[position] == "\\":
                self.reasons.add("escaped_css_identifier_not_analyzed")
            position += 1

    def run(self):
        try:
            for item in self.manifest:
                path = item["path"]
                suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                if suffix not in ("html", "htm", "css"):
                    continue
                self.check()
                if self.scanned_files >= self.limits["files"]:
                    self.truncated = True
                    self.reasons.add("file_count_limit")
                    break
                allowance = min(
                    self.limits["file_bytes"],
                    self.limits["total_bytes"] - self.scanned_bytes,
                )
                if allowance <= 0:
                    self.truncated = True
                    self.reasons.add("total_byte_limit")
                    break
                content = bytearray()
                with self.opener(path) as source:
                    while len(content) < allowance:
                        self.check()
                        chunk = source.read(min(65536, allowance - len(content)))
                        if not chunk:
                            break
                        content.extend(chunk)
                self.scanned_files += 1
                self.scanned_bytes += len(content)
                if item.get("size_bytes", len(content)) > len(content):
                    self.truncated = True
                    self.reasons.add(
                        "file_byte_limit"
                        if allowance == self.limits["file_bytes"]
                        else "total_byte_limit"
                    )
                try:
                    text = content.decode("utf-8-sig")
                except UnicodeError:
                    text = content.decode("utf-8-sig", errors="replace")
                    self.reasons.add("text_encoding_not_utf8")
                document_url = self.base_origin + "/" + path
                if suffix == "css":
                    self.css(text, document_url)
                else:
                    parser = _HTML(self, document_url)
                    for offset in range(0, len(text), 8192):
                        self.check()
                        if (
                            len(parser.rawdata) + getattr(parser, "_pending_len", 0)
                            > 65536
                        ):
                            parser.reset()
                            self.truncated = True
                            self.reasons.add("html_token_limit")
                        parser.feed(text[offset : offset + 8192])
                    self.check()
                    parser.close()
        except ScanDeadline:
            pass
        except (OSError, ValueError, RecursionError):
            self.reasons.add("static_parse_incomplete")
        static_scan_complete = (
            not self.truncated
            and self.reasons == {"dynamic_behavior_not_analyzed"}
        )
        summary = (
            "Static HTML/CSS scan completed."
            if static_scan_complete
            else "Static HTML/CSS scan is incomplete; findings may be partial."
        )
        if not self.scanned_files and static_scan_complete:
            summary += " No HTML/CSS files were scanned."
        elif self.missing or self.external:
            summary += (
                f" Missing local assets: {len(self.missing)}."
                f" External origins: {len(self.external)}."
            )
        else:
            summary += " No missing local assets or external origins were found."
        return {
            "scope": "static_html_css_only",
            "summary": summary,
            "limitations": (
                "Only static HTML/CSS references are checked. JavaScript, API calls "
                "and runtime behavior are not analyzed; no URL was fetched. "
                "An empty finding list does not prove the material is self-contained."
            ),
            "complete": False,
            "static_scan_complete": static_scan_complete,
            "dynamic_code_analyzed": False,
            "truncated": self.truncated,
            "reasons": sorted(self.reasons),
            "external_origins": sorted(self.external),
            "missing_assets": sorted(self.missing),
            "scanned_files": self.scanned_files,
            "scanned_bytes": self.scanned_bytes,
            "limits": dict(self.limits),
        }


class _HTML(HTMLParser):
    RESOURCES = {
        "script": {"src"},
        "img": {"src"},
        "source": {"src"},
        "audio": {"src"},
        "video": {"src", "poster"},
        "iframe": {"src"},
        "embed": {"src"},
        "object": {"data"},
        "input": {"src"},
        "form": {"action"},
        "image": {"href", "xlink:href"},
        "use": {"href", "xlink:href"},
    }

    def __init__(self, scanner, document_url):
        super().__init__(convert_charrefs=True)
        self.scanner = scanner
        self.document_url = document_url
        self.base_seen = False
        self.style_depth = 0

    def handle_starttag(self, tag, attrs):
        self.scanner.check()
        values = dict(attrs)
        if tag == "base" and not self.base_seen and values.get("href"):
            self.base_seen = True
            value = urljoin(self.document_url, values["href"])
            if self.scanner.origin(value):
                self.document_url = value
            else:
                self.scanner.reasons.add("unsupported_base_url")
        if tag == "style":
            self.style_depth += 1
        resources = self.RESOURCES.get(tag, set())
        if tag == "link" and set((values.get("rel") or "").lower().split()) & {
            "stylesheet",
            "preload",
            "modulepreload",
            "icon",
            "manifest",
            "prefetch",
        }:
            resources = {"href"}
        for name in resources:
            if values.get(name):
                self.scanner.reference(values[name], self.document_url)
        if tag in ("img", "source") and values.get("srcset"):
            srcset = values["srcset"]
            if "data:" in srcset.lower():
                self.scanner.reasons.add("complex_srcset_not_analyzed")
            else:
                for candidate in srcset.split(","):
                    parts = candidate.strip().split()
                    if parts:
                        self.scanner.reference(parts[0], self.document_url)
        if values.get("style"):
            self.scanner.css(values["style"], self.document_url)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "style" and self.style_depth:
            self.style_depth -= 1

    def handle_data(self, data):
        if self.style_depth:
            self.scanner.css(data, self.document_url)


def scan_dependencies(
    manifest,
    opener,
    *,
    base_origin="https://publication.invalid",
    entry_path="index.html",
    limits=None,
    clock=time.monotonic,
):
    configured = dict(DEFAULT_LIMITS)
    if limits:
        # Caller overrides can only tighten resource bounds, never silently broaden them.
        for key, value in limits.items():
            if (
                key not in configured
                or not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or (key != "seconds" and not isinstance(value, int))
                or value <= 0
                or value > configured[key]
            ):
                raise ValueError("Invalid dependency scan limit")
            configured[key] = value
    return _Scanner(manifest, opener, base_origin, entry_path, configured, clock).run()
