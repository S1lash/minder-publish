"""Operator session settings map to the canonical session policy."""

import pytest

from minder_publish.errors import PublishError
from minder_publish.server.auth import AuthStore
from minder_publish.server.config import Config, packaged_defaults


ENVIRONMENT = {
    "PUBLISH_OWNER_SESSION_IDLE_DAYS": "owner_session_idle_days",
    "PUBLISH_OWNER_SESSION_ABSOLUTE_DAYS": "owner_session_absolute_days",
    "PUBLISH_OWNER_SESSION_REFRESH_SECONDS": "owner_session_refresh_seconds",
    "PUBLISH_OWNER_RECENT_AUTH_SECONDS": "owner_recent_auth_seconds",
}


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLISH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLISH_BASE_URL", "https://publish.test")
    monkeypatch.setenv("PUBLISH_CONTENT_DOMAIN", "publish.test")
    monkeypatch.setenv("PUBLISH_SECRET", "test-secret-" * 8)
    monkeypatch.delenv("PUBLISH_LEGACY_API_KEY", raising=False)
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    return tmp_path, monkeypatch


def test_nonempty_legacy_api_key_is_rejected_with_migration_guidance(isolated_env):
    _, monkeypatch = isolated_env
    monkeypatch.setenv("PUBLISH_LEGACY_API_KEY", "synthetic-legacy-key")
    with pytest.raises(PublishError) as rejected:
        Config.from_env()
    assert rejected.value.code == "legacy_api_key_removed"
    assert "scoped device token" in rejected.value.detail
    assert "synthetic-legacy-key" not in rejected.value.detail

    monkeypatch.setenv("PUBLISH_LEGACY_API_KEY", "")
    assert Config.from_env().control_origin == "https://publish.test"


def test_session_env_overrides_are_effective_without_mutating_packaged_defaults(
    isolated_env,
):
    _, monkeypatch = isolated_env
    baseline = packaged_defaults()["operations"]
    values = [14, 120, 600, 300]
    for name, value in zip(ENVIRONMENT, values):
        monkeypatch.setenv(name, str(value))
    configured = Config.from_env()
    assert [configured.operations[key] for key in ENVIRONMENT.values()] == values
    assert packaged_defaults()["operations"] == baseline
    assert AuthStore(configured)._session_policy() == (
        14 * 86400,
        120 * 86400,
        600,
        300,
    )
    for name in ENVIRONMENT:
        monkeypatch.delenv(name)
    fresh = Config.from_env()
    assert all(fresh.operations[key] == baseline[key] for key in ENVIRONMENT.values())


@pytest.mark.parametrize("value", ["", "0", "-1", "2.5", "NaN", "ninety"])
def test_invalid_owner_session_env_is_a_structured_configuration_error(
    isolated_env, value
):
    _, monkeypatch = isolated_env
    name = "PUBLISH_OWNER_SESSION_IDLE_DAYS"
    monkeypatch.setenv(name, value)
    with pytest.raises(PublishError) as failure:
        Config.from_env()
    assert (
        failure.value.code == "invalid_session_policy" and failure.value.field == name
    )


def test_cross_field_operator_policy_is_rejected_by_auth_before_initialization(
    isolated_env,
):
    directory, monkeypatch = isolated_env
    monkeypatch.setenv("PUBLISH_OWNER_SESSION_IDLE_DAYS", "120")
    monkeypatch.setenv("PUBLISH_OWNER_SESSION_ABSOLUTE_DAYS", "30")
    with pytest.raises(PublishError) as failure:
        AuthStore(Config.from_env()).initialize()
    assert failure.value.code == "invalid_session_policy"
    assert not (directory / "publish.sqlite3").exists()
