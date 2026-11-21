"""One-prompt setup preserves credentials and validates before persisting."""
import os
from pathlib import Path

import pytest

from minder_publish.client import config
from minder_publish.client import setup as setup_client
from minder_publish.client.errors import ClientError


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    home = tmp_path / 'home with spaces'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setenv('MINDER_PUBLISH_CONFIG_DIR', str(home / 'config'))
    monkeypatch.setenv('LOCALAPPDATA', str(home))
    for name in ('URL', 'TOKEN', 'TOKEN_FILE', 'INSTANCE'):
        monkeypatch.delenv(f'MINDER_PUBLISH_{name}', raising=False)
    monkeypatch.setattr(config, 'executable_path', lambda: home / 'bin with spaces' / 'minder-publish')
    return home


def client_stub(monkeypatch, verify):
    class Client:
        def __init__(self, url, token): self.url, self.token = url, token
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def capabilities(self): return verify(self.url, self.token)
    monkeypatch.setattr(setup_client, 'PublishClient', Client)


def test_setup_without_token_installs_and_returns_absolute_shell_command(setup_env, monkeypatch):
    result = setup_client.setup('codex', 'https://publish.test', 'personal', connect=False, read_token=lambda: pytest.fail('unexpected token prompt'))
    assert result['needs_token'] and not result['authenticated']
    assert (setup_env / '.agents/skills/minder-publish/SKILL.md').is_file()
    assert result['connection_command'][0].startswith(str(setup_env))
    assert "'" in result['connection_shell_command']
    assert config.read_config()['instances'] == {}


def test_setup_installs_skill_in_claude_config_directory(setup_env, monkeypatch, tmp_path):
    claude_config = tmp_path / "separate claude profile"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))

    result = setup_client.setup(
        "claude", "https://publish.test", "personal",
        connect=False, read_token=lambda: pytest.fail("unexpected token prompt"),
    )

    entry = claude_config / "skills" / "minder-publish" / "SKILL.md"
    assert result["skills"][0]["path"] == str(entry)
    assert "name: minder:publish" in entry.read_text(encoding="utf-8")
    assert result["connection_command"][-2:] == ["--claude-config-dir", str(claude_config)]
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    connected = setup_client.setup(
        "claude", "https://publish.test", "personal",
        connect=False, read_token=lambda: pytest.fail("unexpected token prompt"),
        claude_config_dir=Path(result["connection_command"][-1]),
    )
    assert connected["skills"][0]["path"] == str(entry)
    assert not (setup_env / ".claude").exists()


def test_setup_pins_the_parent_of_an_external_claude_profile(setup_env, monkeypatch, tmp_path):
    parent = tmp_path / "profiles"
    parent.mkdir()
    alias = tmp_path / "profile alias"
    if os.name == "nt":
        pytest.skip("POSIX alias required")
    alias.symlink_to(parent, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alias / "claude"))

    result = setup_client.setup(
        "claude", "https://publish.test", "personal",
        connect=False, read_token=lambda: pytest.fail("unexpected token prompt"),
    )

    directory = parent / "claude"
    assert result["connection_command"][-2:] == ["--claude-config-dir", str(directory)]
    assert Path(result["skills"][0]["path"]).is_relative_to(directory)


def test_invalid_token_is_never_saved(setup_env, monkeypatch):
    def reject(url, token): raise ClientError('invalid_credentials', 'invalid')
    client_stub(monkeypatch, reject)
    with pytest.raises(ClientError, match='invalid'):
        setup_client.setup('claude', 'https://publish.test', 'personal', connect=True, read_token=lambda: 'synthetic-invalid-token')
    assert config.read_config()['instances'] == {}


def test_existing_instance_uses_saved_token_even_with_environment_override(setup_env, monkeypatch):
    config.save_instance('personal', 'https://publish.test', 'synthetic-saved-token')
    monkeypatch.setenv('MINDER_PUBLISH_URL', 'https://elsewhere.test')
    monkeypatch.setenv('MINDER_PUBLISH_TOKEN', 'synthetic-environment-token')
    checked = []
    client_stub(monkeypatch, lambda url, token: checked.append((url, token)))
    result = setup_client.setup('codex', 'https://publish.test', 'personal', connect=True, read_token=lambda: pytest.fail('existing must not prompt'))
    assert checked == [('https://publish.test', 'synthetic-saved-token')]
    assert result['authenticated'] and result['reused_configuration']


def test_racing_instance_is_not_overwritten(setup_env, monkeypatch):
    def race(url, token): config.save_instance('personal', url, 'other-synthetic-token')
    client_stub(monkeypatch, race)
    with pytest.raises(ClientError):
        setup_client.setup('codex', 'https://publish.test', 'personal', connect=True, read_token=lambda: 'first-synthetic-token')
    assert config.connection('personal', use_environment=False).token == 'other-synthetic-token'


def test_existing_profile_other_server_stops_before_skill_write(setup_env):
    config.save_instance('personal', 'https://elsewhere.test', 'synthetic-token')
    with pytest.raises(ClientError) as error:
        setup_client.setup('codex', 'https://publish.test', 'personal', connect=False, read_token=lambda: '')
    assert error.value.code == 'instance_conflict'
    assert not (setup_env / '.agents').exists()


def test_existing_profile_changed_before_credential_snapshot_is_not_sent(setup_env, monkeypatch):
    config.save_instance('personal', 'https://publish.test', 'first-synthetic-token')
    connection = config.connection
    def switched(name, **kwargs):
        config.save_instance('personal', 'https://elsewhere.test', 'different-synthetic-token')
        return connection(name, **kwargs)
    monkeypatch.setattr(config, 'connection', switched)
    client_stub(monkeypatch, lambda url, token: pytest.fail('must not send credentials to old server'))
    with pytest.raises(ClientError):
        setup_client.setup('codex', 'https://publish.test', 'personal', connect=True, read_token=lambda: '')


def test_reading_during_configuration_write_fails_closed(setup_env, monkeypatch):
    monkeypatch.setattr(config, 'LOCK_TIMEOUT_SECONDS', .05)
    config.save_instance('personal', 'https://publish.test', 'synthetic-token')
    with config.configuration_lock():
        with pytest.raises(ClientError) as error:
            config.connection('personal', use_environment=False)
    assert error.value.code == 'configuration_busy'


@pytest.mark.parametrize('use_file', [False, True])
def test_complete_environment_never_reads_or_locks_profile(setup_env, monkeypatch, use_file):
    monkeypatch.setenv('MINDER_PUBLISH_URL', 'https://publish.test')
    if use_file:
        token_path = config.config_directory() / 'fixture-token'
        token_path.parent.mkdir(parents=True)
        token_path.write_text('synthetic-token')
        token_path.chmod(0o600)
        monkeypatch.setenv('MINDER_PUBLISH_TOKEN_FILE', 'fixture-token')
    else:
        monkeypatch.setenv('MINDER_PUBLISH_TOKEN', 'synthetic-token')
    monkeypatch.setattr(config, 'read_config', lambda: pytest.fail('profile read'))
    monkeypatch.setattr(config, 'configuration_lock', lambda: pytest.fail('profile lock'))
    assert config.connection().token == 'synthetic-token'


def test_saved_profile_read_does_not_write_into_profile_directory(setup_env):
    config.save_instance('personal', 'https://publish.test', 'synthetic-token')
    directory = config.config_directory()
    before = {p.relative_to(directory) for p in directory.rglob('*')}
    assert config.connection('personal', use_environment=False).token == 'synthetic-token'
    assert {p.relative_to(directory) for p in directory.rglob('*')} == before


def test_config_path_aliases_and_tempdir_share_lock(setup_env, monkeypatch):
    monkeypatch.setattr(config, 'LOCK_TIMEOUT_SECONDS', .05)
    config.save_instance('personal', 'https://publish.test', 'synthetic-token')
    original = config.config_directory()
    alias = setup_env / 'alias'
    try:
        alias.symlink_to(original, target_is_directory=True)
    except OSError as error:
        if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
            pytest.skip('Windows host does not grant permission to create symlinks')
        raise
    with config.configuration_lock():
        monkeypatch.setenv('MINDER_PUBLISH_CONFIG_DIR', str(alias))
        monkeypatch.setenv('TMPDIR', str(setup_env / 'other-temp'))
        with pytest.raises(ClientError) as error:
            config.connection('personal', use_environment=False)
    assert error.value.code == 'configuration_busy'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mode and hardlink semantics')
@pytest.mark.parametrize('kind', ['symlink', 'permissive', 'hardlink', 'directory'])
def test_unsafe_lock_file_is_rejected(setup_env, monkeypatch, tmp_path, kind):
    import hashlib
    lock_dir = tmp_path / 'locks'
    lock_dir.mkdir(mode=0o700)
    monkeypatch.setattr(config, '_configuration_lock_directory', lambda: lock_dir)
    identity = os.path.normcase(str(config.config_directory().resolve()))
    lock = lock_dir / (hashlib.sha256(os.fsencode(identity)).hexdigest() + '.lock')
    if kind == 'directory': lock.mkdir()
    elif kind == 'symlink': lock.symlink_to(tmp_path / 'missing')
    elif kind == 'hardlink':
        source = tmp_path / 'source'; source.write_text(''); source.chmod(0o600); os.link(source, lock)
    else:
        lock.write_text(''); lock.chmod(0o644)
    with pytest.raises(ClientError) as error:
        with config.configuration_lock(): pass
    assert error.value.code == 'unsafe_config'


def test_setup_binds_skill_to_selected_instance_without_changing_default(setup_env, monkeypatch):
    import re
    config.save_instance('first', 'https://first.test', 'first-synthetic-token')
    config.save_instance('personal', 'https://publish.test', 'second-synthetic-token', make_default=False)
    client_stub(monkeypatch, lambda url, token: None)
    result = setup_client.setup('codex', 'https://publish.test', 'personal', connect=False, read_token=lambda: pytest.fail('unexpected prompt'))
    assert result['authenticated']
    assert config.read_config()['default_instance'] == 'first'
    entry = setup_env / '.agents/skills/minder-publish/SKILL.md'
    references = re.findall(r'\]\(([^)]+)\)', entry.read_text())
    installation = next(entry.parent / ref for ref in references if ref.endswith('/installation.md'))
    text = installation.read_text()
    assert '--instance personal' in text and 'https://publish.test' in text


def test_skill_repair_selects_explicit_instance_or_persisted_default(setup_env, monkeypatch):
    from minder_publish.client import cli
    config.save_instance('first', 'https://first.test', 'first-synthetic-token')
    config.save_instance('personal', 'https://publish.test', 'second-synthetic-token', make_default=False)
    monkeypatch.setattr(config, '_read_token', lambda path: pytest.fail('skill repair must not read tokens'))
    parser = cli.build_parser()
    default = cli.run(parser.parse_args(['skills', '--runtime', 'codex', '--home', str(setup_env)]))
    explicit = cli.run(parser.parse_args(['--instance', 'personal', 'skills', '--runtime', 'codex', '--home', str(setup_env)]))
    assert default[0]['instance'] == 'first' and default[0]['base_url'] == 'https://first.test'
    assert explicit[0]['instance'] == 'personal' and explicit[0]['base_url'] == 'https://publish.test'


def test_skill_binding_requires_complete_instance_pair(setup_env):
    from minder_publish.client import skills
    for kwargs in ({'instance_name': 'personal'}, {'instance_url': 'https://publish.test'}):
        with pytest.raises(ClientError):
            skills.install(skills.canonical_source(), home=setup_env, runtimes=['codex'], apply=True, **kwargs)
    assert not (setup_env / '.agents').exists()


def test_doctor_brief_preserves_identity_without_full_policy(setup_env, monkeypatch):
    from minder_publish.client import cli
    config.save_instance('personal', 'https://publish.test', 'synthetic-token')
    class Client:
        def __init__(self, url, token): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def capabilities(self): return {'supported_kinds':['file','site'], 'limits':{'file_bytes':99}, 'defaults':{'external_ttl_seconds':604800, 'x':1}}
    monkeypatch.setattr(cli, 'PublishClient', Client)
    parser = cli.build_parser()
    brief = cli.run(parser.parse_args(['--instance','personal','doctor','--brief']))
    full = cli.run(parser.parse_args(['--instance','personal','doctor']))
    assert brief == {'instance':'personal', 'base_url':'https://publish.test', 'authenticated':True, 'supported_kinds':['file','site'], 'defaults':{'external_ttl_seconds':604800}}
    assert full['capabilities']['limits']['file_bytes'] == 99
