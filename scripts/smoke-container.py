#!/usr/bin/env python3
"""Exercise a locally built image using disposable containers and volumes.

Creates no public listener: the temporary port binds to 127.0.0.1. This is an HTTP,
container and offline-operation check, not a substitute for browser/TLS acceptance.
Only resources created by this invocation are stopped and removed.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import secrets
import socket
import subprocess
import time
import zipfile
from http.cookies import SimpleCookie
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="minder-publish:0.3.5")
    args = parser.parse_args()
    prefix = "minder-publish-smoke-" + secrets.token_hex(6)
    containers = []
    volumes = []
    gates = []

    def docker(*values, input=None, check=True):
        result = subprocess.run(
            ["docker", *values], input=input, capture_output=True, text=True
        )
        if check and result.returncode:
            raise RuntimeError("Docker operation failed: " + values[0])
        return result

    def volume(label):
        name = prefix + "-" + label
        docker("volume", "create", name)
        volumes.append(name)
        return name

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://publish.localhost:{port}"
    authority = urlsplit(origin).netloc
    environment = [
        "-e",
        "PUBLISH_BASE_URL=" + origin,
        "-e",
        "PUBLISH_STORAGE_BUDGET_BYTES=21474836480",
    ]
    limits = [
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
    ]
    password = secrets.token_urlsafe(32)
    cookie_jar = {}

    def request(
        method,
        path,
        body=None,
        *,
        token=None,
        csrf=None,
        host=authority,
        extra=None,
        include_cookies=True,
    ):
        headers = {"Host": host, "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if csrf:
            headers.update({"Origin": origin, "X-CSRF-Token": csrf})
        if include_cookies and cookie_jar.get(host):
            headers["Cookie"] = "; ".join(
                f"{key}={value}" for key, value in cookie_jar[host].items()
            )
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif isinstance(body, bytes):
            headers["Content-Type"] = "application/octet-stream"
        if extra:
            headers.update(extra)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            content = response.read()
            metadata = dict(response.getheaders())
            for key, value in response.getheaders():
                if key.lower() != "set-cookie":
                    continue
                cookies = SimpleCookie()
                cookies.load(value)
                for name, item in cookies.items():
                    assert not item["domain"], (
                        "Authentication cookie has a parent Domain"
                    )
                    if item["max-age"] == "0":
                        cookie_jar.setdefault(host, {}).pop(name, None)
                    else:
                        cookie_jar.setdefault(host, {})[name] = item.value
            return response.status, metadata, content
        finally:
            connection.close()

    def api(method, path, body=None, **kwargs):
        status, headers, content = request(method, "/api/v1" + path, body, **kwargs)
        assert 200 <= status < 300, f"API {method} {path} returned {status}"
        return json.loads(content)["data"]

    def start(data, label):
        name = prefix + "-" + label
        docker(
            "run",
            "-d",
            "--name",
            name,
            *limits,
            *environment,
            "-p",
            f"127.0.0.1:{port}:8080",
            "-v",
            data + ":/data",
            args.image,
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
        )
        containers.append(name)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if request("GET", "/healthz")[0] == 200:
                    return name
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        raise RuntimeError("The disposable server did not become ready")

    def publish(token, body, filename, kind="file", *, replace=None):
        intent = {
            "kind": kind,
            "filename": filename,
            "size_bytes": len(body),
            "expanded_size_bytes": len(body) * 10 if kind == "site" else len(body),
        }
        if replace is not None:
            intent.update(
                publication_id=replace["id"],
                expected_revision=replace["current_revision"],
            )
        upload = api(
            "POST",
            "/uploads",
            intent,
            token=token,
        )
        identifier = upload["id"]
        api("PUT", f"/uploads/{identifier}/content", body, token=token)
        inspection = api("POST", f"/uploads/{identifier}/inspect", {}, token=token)
        assert inspection["state"] == "prepared"
        return api("POST", f"/uploads/{identifier}/commit", {}, token=token)

    cleanup = []
    try:
        data = volume("data")
        saved = volume("backup")
        restored = volume("restored")
        docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            saved + ":/backup",
            "--entrypoint",
            "python",
            args.image,
            "-c",
            'import os; os.chown("/backup",10001,10001); os.chmod("/backup",0o700)',
        )
        docker(
            "run",
            "--rm",
            "-i",
            *limits,
            *environment,
            "-v",
            data + ":/data",
            args.image,
            "bootstrap",
            "--username",
            "smoke",
            "--password-stdin",
            input=password + "\n",
        )
        server = start(data, "server")
        gates.append("non_root_read_only_startup")
        session = api(
            "POST",
            "/auth/login",
            {"username": "smoke", "password": password},
            extra={"Origin": origin},
        )
        client = api("GET", "/distribution")["client"]
        assert client["available"]
        status, _, payload = request(
            "GET", "/downloads/" + client["filename"], include_cookies=False
        )
        assert status == 200 and hashlib.sha256(payload).hexdigest() == client["sha256"]
        with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
            files = wheel.namelist()
            assert "minder_publish/client/cli.py" in files
            assert not any(
                name.startswith(("minder_publish/server/", "minder_publish/mcp/"))
                for name in files
            )
            assert any(name.endswith(".dist-info/licenses/LICENSE") for name in files)
        gates.append("anonymous_cli_wheel")
        token = api(
            "POST",
            "/tokens",
            {"name": "disposable", "scope": "materials:manage"},
            csrf=session["csrf_token"],
        )["token"]
        original = b"Bytes survive publication, download and restore.\n"
        material = publish(token, original, "result.txt")
        assert (
            request(
                "GET", f"/api/v1/publications/{material['id']}/download", token=token
            )[2]
            == original
        )
        updated = b"Updated bytes survive at the same address.\n"
        replacement = publish(token, updated, "result.txt", replace=material)
        assert replacement["stable_url"] == material["stable_url"]
        assert replacement["current_revision"] != material["current_revision"]
        path = "/p/" + material["id"]
        host = authority
        for _ in range(6):
            status, headers, body = request("GET", path, host=host)
            if status not in (302, 303, 307, 308):
                break
            destination = urlsplit(headers["location"])
            host = destination.netloc
            path = destination.path + (
                "?" + destination.query if destination.query else ""
            )
        assert status == 200 and body == updated and host != authority
        assert (
            request("GET", path, host=host, extra={"Range": "bytes=0-3"})[2]
            == updated[:4]
        )
        assert request("GET", "/api/v1/session", host=host)[0] == 404
        gates.append("real_http_publish_inspect_handoff_range_download")
        gates.append("update_keeps_stable_url_and_serves_current_revision")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("index.html", "A complete site")
            output.writestr("assets/app.js", "/* static */")
        site = publish(token, archive.getvalue(), "site.zip", "site")
        payload = request(
            "GET", f"/api/v1/publications/{site['id']}/download", token=token
        )[2]
        with zipfile.ZipFile(io.BytesIO(payload)) as exported:
            assert set(exported.namelist()) == {"index.html", "assets/app.js"}
        api("POST", "/auth/logout", {}, csrf=session["csrf_token"])
        assert request("HEAD", path, host=host)[0] == 403
        gates.append("site_export_and_owner_logout_revocation")
        docker("stop", server)
        docker("rm", server)
        containers.remove(server)
        docker(
            "run",
            "--rm",
            *limits,
            *environment,
            "-v",
            data + ":/data",
            "-v",
            saved + ":/backup",
            args.image,
            "backup",
            "--output",
            "/backup/snapshot.tar",
        )
        docker(
            "run",
            "--rm",
            *limits,
            *environment,
            "-v",
            restored + ":/data",
            "-v",
            saved + ":/backup:ro",
            args.image,
            "restore",
            "--input",
            "/backup/snapshot.tar",
        )
        cookie_jar.clear()
        start(restored, "restored-server")
        assert request("GET", "/api/v1/publications", token=token)[0] == 401
        session = api(
            "POST",
            "/auth/login",
            {"username": "smoke", "password": password},
            extra={"Origin": origin},
        )
        new_token = api(
            "POST",
            "/tokens",
            {"name": "restored disposable", "scope": "materials:read"},
            csrf=session["csrf_token"],
        )["token"]
        assert (
            request(
                "GET",
                f"/api/v1/publications/{material['id']}/download",
                token=new_token,
            )[2]
            == updated
        )
        gates.append("offline_backup_read_only_root_restore_credential_revocation")
    finally:
        for name in reversed(containers):
            docker("stop", name, check=False)
            if docker("rm", name, check=False).returncode:
                cleanup.append(name)
        for name in reversed(volumes):
            if docker("volume", "rm", name, check=False).returncode:
                cleanup.append(name)
    assert not cleanup, "Some disposable resources could not be removed"
    print(
        json.dumps(
            {
                "passed": True,
                "image": args.image,
                "gates": gates,
                "disposable_resources_removed": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
