import io
import json
import os
from pathlib import Path

import pytest

from minder_publish.client import cli, config
from minder_publish.client.errors import ClientError
from minder_publish.client.skills import install


@pytest.fixture(autouse=True)
def isolated_client_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDER_PUBLISH_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for name in (
        "MINDER_PUBLISH_URL",
        "MINDER_PUBLISH_TOKEN",
        "MINDER_PUBLISH_TOKEN_FILE",
        "MINDER_PUBLISH_INSTANCE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_configure_multi_instance_keeps_token_out_of_config_and_output(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin", io.StringIO("device-secret\n"))
    assert (
        cli.main(
            [
                "configure",
                "home",
                "--url",
                "https://home.example",
                "--token-stdin",
                "--json",
            ]
        )
        == 0
    )
    assert "device-secret" not in capsys.readouterr().out
    assert "device-secret" not in (tmp_path / "config.json").read_text()
    assert config.connection().token == "device-secret"
    config.save_instance(
        "work", "https://work.example", "second-secret", make_default=False
    )
    assert config.connection("work").base_url == "https://work.example"
    assert config.connection().base_url == "https://home.example"
    if os.name != "nt":
        token_file = config.read_config()["instances"]["home"]["token_file"]
        assert (tmp_path / token_file).stat().st_mode & 0o777 == 0o600


def test_no_owner_host_or_keystore_fallback():
    with pytest.raises(ClientError, match="Configure"):
        config.connection()


@pytest.mark.parametrize("setting", ["URL", "TOKEN", "TOKEN_FILE"])
def test_incomplete_environment_connection_never_uses_saved_profile(
    setting, tmp_path, monkeypatch
):
    config.save_instance("work", "https://work.example", "saved-test-token")
    token_file = tmp_path / "environment.token"
    token_file.write_text("environment-test-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    values = {
        "URL": "https://other.example",
        "TOKEN": "environment-test-token",
        "TOKEN_FILE": str(token_file),
    }
    monkeypatch.setenv(f"MINDER_PUBLISH_{setting}", values[setting])

    with pytest.raises(ClientError) as failure:
        config.connection("work")

    assert failure.value.code == "incomplete_environment_connection"
    assert config.connection("work", use_environment=False).token == "saved-test-token"


def test_interrupted_profile_replacement_preserves_old_connection(tmp_path, monkeypatch):
    config.save_instance("work", "https://work.example", "first-test-token")
    expected = config.connection("work")
    token_files = set((tmp_path / "tokens").iterdir())
    replace = os.replace

    def reject_config_replace(source, destination):
        if Path(destination) == tmp_path / "config.json":
            raise OSError("synthetic configuration write failure")
        return replace(source, destination)

    with monkeypatch.context() as failure:
        failure.setattr(os, "replace", reject_config_replace)
        with pytest.raises(OSError, match="synthetic configuration write failure"):
            config.save_instance("work", "https://other.example", "second-test-token")

    assert config.connection("work") == expected
    assert set((tmp_path / "tokens").iterdir()) == token_files
    config.save_instance("work", "https://other.example", "second-test-token")
    assert config.connection("work") == config.Connection(
        "work", "https://other.example", "second-test-token"
    )
    assert len(list((tmp_path / "tokens").iterdir())) == 1


def test_replacing_profile_preserves_shared_credential(tmp_path):
    config.save_instance("work", "https://work.example", "shared-test-token")
    settings = config.read_config()
    work = settings["instances"]["work"]
    settings["instances"]["other"] = {**work, "token_file": str(tmp_path / work["token_file"])}
    (tmp_path / "config.json").write_text(json.dumps(settings), encoding="utf-8")

    config.save_instance("work", "https://new.example", "new-test-token")

    assert config.connection("other") == config.Connection(
        "other", "https://work.example", "shared-test-token"
    )


@pytest.mark.parametrize(
    ("configured", "canonical"),
    [
        ("https://Owner.Example:443/", "https://owner.example"),
        ("https://Owner.Example:8443/publish/", "https://owner.example:8443/publish"),
        ("http://[::1]:80/", "http://[::1]"),
    ],
)
def test_client_base_url_matches_canonical_server_authority(configured, canonical):
    assert config.validate_base_url(configured) == canonical


def test_untrusted_http_and_credentials_in_url_rejected():
    for value in (
        "http://public.example",
        "https://token@example.test",
        "https://example.test/?token=secret",
    ):
        with pytest.raises(ClientError):
            config.validate_base_url(value)


def test_skills_dry_run_diff_idempotent_and_preserve_other_files(tmp_path):
    source = Path(__file__).parents[1] / "skill" / "minder-publish" / "SKILL.md"
    other = tmp_path / ".codex" / "skills" / "other" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text("leave alone")
    plans = install(source, home=tmp_path, runtimes=["codex", "claude"])
    assert all(
        plan["changed"] and plan["diff"] and not plan["applied"] for plan in plans
    )
    assert not (tmp_path / ".claude").exists()
    install(source, home=tmp_path, runtimes=["codex", "claude"], apply=True)
    assert all(
        not plan["changed"]
        for plan in install(
            source, home=tmp_path, runtimes=["codex", "claude"], apply=True
        )
    )
    assert other.read_text() == "leave alone"


def test_cli_delete_requires_explicit_confirmation(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def delete(self, id):
            pytest.fail("delete was not authorized")

    monkeypatch.setattr(cli, "PublishClient", FakeClient)
    monkeypatch.setattr(
        config,
        "connection",
        lambda instance=None: config.Connection("home", "https://home.test", "token"),
    )
    assert cli.main(["delete", "one", "--json"]) == 2
    assert (
        json.loads(capsys.readouterr().out)["error"]["code"] == "confirmation_required"
    )


def test_cli_invalid_arguments_still_return_json(capsys):
    assert cli.main(["publish", "x.txt", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"


def test_publication_password_file_preserves_passphrase_but_token_remains_strict(
    tmp_path, monkeypatch, capsys
):
    password = "  two word passphrase  "
    source = tmp_path / "password.txt"
    source.write_text(password + "\n", encoding="utf-8")
    source.chmod(0o600)
    received = []

    class FakeClient:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def patch(self, identifier, changes):
            received.append(changes)
            return {"id": identifier}

    monkeypatch.setattr(cli, "PublishClient", FakeClient)
    monkeypatch.setattr(
        config,
        "connection",
        lambda instance=None: config.Connection("home", "https://home.test", "token"),
    )
    assert (
        cli.main(
            [
                "set",
                "one",
                "--access",
                "password",
                "--password-file",
                str(source),
                "--json",
            ]
        )
        == 0
    )
    assert received == [{"access": "password", "password": password}]
    assert password not in capsys.readouterr().out
    with pytest.raises(ClientError) as failure:
        config._read_token(source)
    assert failure.value.code == "invalid_token"


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-file modes and symlinks")
def test_password_file_keeps_private_regular_file_safeguards(tmp_path):
    source = tmp_path / "password.txt"
    source.write_text("two word passphrase\n", encoding="utf-8")
    source.chmod(0o644)
    with pytest.raises(ClientError):
        cli._secret(file=str(source), field="password")
    source.chmod(0o600)
    link = tmp_path / "password-link.txt"
    link.symlink_to(source)
    with pytest.raises(ClientError):
        cli._secret(file=str(link), field="password")


def test_secret_file_limits_bytes_and_requires_utf8(tmp_path):
    source = tmp_path / "secret.txt"
    source.touch(mode=0o600)
    source.write_bytes(b"synthetic-token\r\n")
    assert config.read_private_secret_file(source) == "synthetic-token"

    source.write_bytes(b"x" * (config.MAX_SECRET_FILE_BYTES + 1))
    with pytest.raises(ClientError, match="too large") as oversized:
        config.read_private_secret_file(source)
    assert oversized.value.code == "invalid_token"

    source.write_bytes(b"\xff")
    with pytest.raises(ClientError, match="UTF-8") as invalid:
        config.read_private_secret_file(source, field="password")
    assert invalid.value.code == "invalid_password"


def test_duration_overflow_keeps_cli_json_error_envelope(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def patch(self, *args):
            pytest.fail("invalid expiry must not reach the API")

    monkeypatch.setattr(cli, "PublishClient", FakeClient)
    monkeypatch.setattr(
        config,
        "connection",
        lambda instance=None: config.Connection("home", "https://home.test", "token"),
    )
    assert (
        cli.main(
            ["set", "one", "--expiry", "999999999999999999999999999999d", "--json"]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_expiry"


def test_cli_upload_status_maps_only_to_read_endpoint(monkeypatch, capsys):
    requested = []

    class FakeClient:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_upload(self, identifier):
            requested.append(identifier)
            return {
                "id": identifier,
                "state": "committed",
                "publication_id": "result",
                "stable_url": "https://home.test/p/result",
            }

    monkeypatch.setattr(cli, "PublishClient", FakeClient)
    monkeypatch.setattr(
        config,
        "connection",
        lambda instance=None: config.Connection("home", "https://home.test", "token"),
    )
    assert cli.main(["upload-status", "operation", "--json"]) == 0
    assert requested == ["operation"]
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "committed"
