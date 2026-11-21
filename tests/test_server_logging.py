"""The real operator entrypoint must never duplicate query strings into logs."""

import http.client
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid

import uvicorn

from minder_publish.server import app, commands


def isolated_environment(directory):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PUBLISH_")
    }
    environment.update(
        PUBLISH_DATA_DIR=str(directory),
        PUBLISH_BASE_URL="http://publish.localhost",
        PUBLISH_CONTENT_DOMAIN="publish.localhost",
        PUBLISH_SECRET="synthetic-logging-test-secret-" * 3,
        PUBLISH_REMOTE_MCP="false",
        PYTHONUNBUFFERED="1",
    )
    return environment


def test_serve_disables_raw_access_log_and_preserves_structured_logger(
    tmp_path, monkeypatch
):
    for name in tuple(os.environ):
        if name.startswith("PUBLISH_"):
            monkeypatch.delenv(name)
    for name, value in isolated_environment(tmp_path / "instance").items():
        if name.startswith("PUBLISH_"):
            monkeypatch.setenv(name, value)
    selected, captured = object(), {}
    monkeypatch.setattr(app, "create_app", lambda config: selected)
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda application, **options: captured.update(app=application, **options),
    )
    logger = logging.getLogger("minder_publish")
    original = list(logger.handlers), logger.level, logger.propagate
    try:
        assert commands.main(["serve"]) == 0
        assert captured["app"] is selected
        assert captured.get("access_log") is False, (
            "The operator CLI must disable Uvicorn's raw query-string access log"
        )
        assert logger.level == logging.INFO and logger.propagate is False
    finally:
        for handler in tuple(logger.handlers):
            if handler not in original[0]:
                logger.removeHandler(handler)
                handler.close()
        logger.handlers, logger.level, logger.propagate = original


def test_real_cli_logs_safe_request_event_without_query_marker(tmp_path):
    output = tmp_path / "operator-cli.log"
    environment = isolated_environment(tmp_path / "instance")
    marker = "synthetic-query-do-not-log-" + uuid.uuid4().hex
    # Port zero avoids a reserve/close race. The logical Host is independent of
    # the backend listening port, exactly as it is behind the production proxy.
    with output.open("w") as stream:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "minder_publish.server.commands",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ],
            cwd=tmp_path,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 20
            port = None
            while time.monotonic() < deadline and process.poll() is None:
                match = re.search(
                    r"Uvicorn running on http://127\.0\.0\.1:([0-9]+)",
                    output.read_text(),
                )
                if match:
                    port = int(match[1])
                    break
                time.sleep(0.05)
            assert port and process.poll() is None, (
                "The isolated production CLI did not start"
            )
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(
                    "GET",
                    "/healthz?ticket=" + marker,
                    headers={"Host": "publish.localhost"},
                )
                response = connection.getresponse()
                assert response.status == 200
                response.read()
            finally:
                connection.close()
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise AssertionError("The isolated CLI did not stop cleanly") from None
    assert process.returncode in (0, -signal.SIGTERM)
    captured = output.read_text()
    assert marker not in captured, (
        "Raw Uvicorn access logging exposed the synthetic sensitive query value"
    )
    events = []
    for line in captured.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    assert any(
        event.get("event") == "http_request"
        and event.get("path") == "/healthz"
        and event.get("status") == 200
        for event in events
    )
