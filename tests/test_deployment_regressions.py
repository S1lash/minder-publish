"""Exercise the documented operator entry points with synthetic data."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from minder_publish.server.app import create_app
from minder_publish.server.backup import instance_lock
from minder_publish.server.commands import doctor
from minder_publish.server.config import Config, packaged_defaults
from minder_publish.server.core import Core


ROOT = Path(__file__).resolve().parents[1]


async def test_browser_can_sign_in_when_https_url_includes_default_port(tmp_path):
    app = create_app(
        Config(
            data_dir=tmp_path,
            base_url="https://PUBLISH.test:443",
            secret="test-instance-secret-" * 4,
            testing=True,
        )
    )
    async with app.router.lifespan_context(app):
        password = "test-only-owner-passphrase"
        app.state.auth.bootstrap("owner", password)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://publish.test"
        ) as client:
            credentials = {"username": "owner", "password": password}
            rejected = await client.post(
                "/api/v1/auth/login",
                json=credentials,
                headers={"Origin": "https://other.test"},
            )
            assert rejected.status_code == 403
            accepted = await client.post(
                "/api/v1/auth/login",
                json=credentials,
                headers={"Origin": "https://publish.test"},
            )
            assert accepted.status_code == 200, accepted.json()["code"]


def test_direct_localhost_keeps_a_nondefault_port(tmp_path):
    config = Config(data_dir=tmp_path, base_url="http://Publish.localhost:8080")
    assert config.control_origin == "http://publish.localhost:8080"
    revision = "0" * 32
    assert config.content_origin(revision) == f"http://r-{revision}.publish.localhost:8080"


def test_rendered_proxy_uses_service_env_file_despite_inherited_origin(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PUBLISH_BASE_URL=https://file.publish.test\n"
        "PUBLISH_CONTENT_DOMAIN=file.publish.test\n",
        encoding="utf-8",
    )
    output = tmp_path / "nginx.conf"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/publish/render_config.py"),
            "--env-file",
            str(env_file),
            "--output",
            str(output),
        ],
        env={
            **os.environ,
            "PUBLISH_BASE_URL": "https://shell.publish.test",
            "PUBLISH_CONTENT_DOMAIN": "shell.publish.test",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    config = output.read_text(encoding="utf-8")
    assert "server_name file.publish.test *.file.publish.test;" in config
    assert "shell.publish.test" not in config
    defaults = packaged_defaults()
    body_maximum = (
        defaults["ceilings"]["upload_payload_bytes"]
        + defaults["limits"]["multipart_overhead_bytes"]
    )
    assert f"client_max_body_size {body_maximum};" in config


def test_proxy_renderer_pipes_configuration_without_status_text():
    environment = (
        "PUBLISH_BASE_URL=https://compose.publish.test\n"
        "PUBLISH_CONTENT_DOMAIN=compose.publish.test\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/publish/render_config.py"),
            "--env-file",
            "-",
            "--output",
            "-",
        ],
        input=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "server_name compose.publish.test *.compose.publish.test;" in result.stdout
    assert "Rendered" not in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="Operator server targets POSIX")
def test_doctor_accepts_database_larger_than_its_scratch_budget(tmp_path):
    config = Config(
        data_dir=tmp_path / "data",
        base_url="https://publish.test",
        secret="test-instance-secret-" * 4,
    )
    Core(config).initialize()
    scratch_limit = 1024**2
    connection = sqlite3.connect(config.db_path)
    try:
        connection.execute("CREATE TABLE synthetic_size_only (payload BLOB)")
        connection.execute(
            "INSERT INTO synthetic_size_only VALUES (zeroblob(?))", (scratch_limit * 2,)
        )
        connection.commit()
    finally:
        connection.close()
    assert config.db_path.stat().st_size > scratch_limit

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = {
        name: value for name, value in os.environ.items() if not name.startswith("PUBLISH_")
    }
    env.update(
        PUBLISH_DATA_DIR=str(config.data_dir),
        PUBLISH_BASE_URL=config.base_url,
        PUBLISH_SECRET=config.secret,
        TMPDIR=str(scratch),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import resource, signal, sys\n"
            "from minder_publish.server.commands import main\n"
            "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
            "limit = int(sys.argv[1])\n"
            "resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))\n"
            "raise SystemExit(main(['doctor']))\n",
            str(scratch_limit),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["healthy"]


def test_doctor_reads_latest_committed_wal_while_instance_is_running(tmp_path):
    config = Config(
        data_dir=tmp_path,
        base_url="https://publish.test",
        secret="test-instance-secret-" * 4,
    )
    with instance_lock(config):
        Core(config).initialize()
        database = config.db_path
        checkpointed = database.read_bytes()
        with closing(sqlite3.connect(database)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            settings = json.loads(
                writer.execute("SELECT data FROM core_settings WHERE id=1").fetchone()[0]
            )
            quota = 9 * 1024**3
            settings["limits"]["content_quota_bytes"] = quota
            writer.execute(
                "UPDATE core_settings SET data=? WHERE id=1", (json.dumps(settings),)
            )
            writer.commit()
            assert database.read_bytes() == checkpointed

            report = doctor(config)

            assert report["healthy"]
            assert report["storage"]["quota_bytes"] == quota
            assert database.read_bytes() == checkpointed
