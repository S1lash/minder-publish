"""Instance downloads are deterministic, bounded and contain no credentials."""

import hashlib
import io
import json
import runpy
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from minder_publish import __version__
from minder_publish.errors import PublishError
from minder_publish.server.distribution import Distribution


@pytest.fixture(scope="module")
def client_wheel(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path_factory.mktemp("client-wheel")
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts/build-client-wheel.py"),
            "--wheel-dir",
            str(output),
        ],
        check=True,
        cwd=root,
    )
    return next(output.glob("*.whl"))


def test_skill_downloads_include_references_and_are_deterministic():
    first = Distribution("https://publish.example")
    second = Distribution("https://publish.example")
    for name in ("minder-publish-skill.zip", "minder-publish-plugin.zip"):
        content, mime = first.asset(name)
        assert mime == "application/zip"
        assert content == second.asset(name)[0]
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            paths = archive.namelist()
            assert any(name.endswith("/references/cli.md") for name in paths)
            assert any(name.endswith("/references/mcp.md") for name in paths)
            assert all(
                not name.startswith("/") and ".." not in Path(name).parts
                for name in paths
            )
    plugin = first.asset("minder-publish-plugin.zip")[0]
    with zipfile.ZipFile(io.BytesIO(plugin)) as archive:
        manifest = json.loads(archive.read("plugin.json"))
        mcp = json.loads(archive.read("mcp.json"))
        assert manifest["name"] == "minder-publish"
        assert mcp["mcpServers"]["publish"]["url"] == "https://publish.example/mcp"
        auth = mcp["mcpServers"]["publish"]["extensions"]["com.openai"]["auth"]
        assert auth["type"] == "oauth"
        assert auth["client"]["mode"] == "dcr"
        assert auth["defaultScopes"] == ["materials:manage"]
        assert "Bearer " not in str(mcp) and "clientSecret" not in str(mcp)


def test_missing_wheel_has_no_fake_install_prompt_or_download():
    distribution = Distribution("https://publish.example")
    metadata = distribution.metadata()
    assert metadata["client"]["available"] is False
    assert metadata["install_prompts"] == {}
    with pytest.raises(PublishError):
        distribution.asset("install-client.py")
    with pytest.raises(PublishError):
        distribution.asset("../../server.secret")


def test_client_metadata_pins_actual_asset_and_generated_installer(client_wheel):
    distribution = Distribution("https://publish.example", client_wheel=client_wheel)
    metadata = distribution.metadata()
    client = metadata["client"]
    assert client["available"] is True
    assert client["sha256"] == hashlib.sha256(client_wheel.read_bytes()).hexdigest()
    assert (
        client["url"]
        == f"https://publish.example/downloads/minder_publish-{__version__}-py3-none-any.whl"
    )
    assert distribution.asset(client["filename"])[0] == client_wheel.read_bytes()
    installer = distribution.asset("install-client.py")[0].decode()
    compile(installer, "install-client.py", "exec")
    assert client["url"] in installer and client["sha256"] in installer
    for runtime in ("codex", "claude"):
        prompt = metadata["install_prompts"][runtime]
        assert "https://publish.example/downloads/install-client.py" in prompt
        assert "curl" not in prompt and "pip install minder-publish" not in prompt
        assert "token" in prompt and "private" in prompt


def test_installer_rejects_changed_download_before_creating_environment(
    tmp_path, monkeypatch, client_wheel
):
    distribution = Distribution(
        "http://127.0.0.1:8080", client_wheel=client_wheel
    )
    script = tmp_path / "install.py"
    script.write_bytes(distribution.asset("install-client.py")[0])

    class ChangedDownload(io.BytesIO):
        status = 200

    class Opener:
        def open(self, *args, **kwargs):
            return ChangedDownload(b"different bytes")

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    monkeypatch.setattr(
        sys, "argv", [str(script), "--venv", str(tmp_path / "client-venv")]
    )
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(script), run_name="__main__")
    assert error.value.code == 2
    assert not (tmp_path / "client-venv").exists()


def test_invalid_wheel_fails_before_publishing_code_assets(tmp_path):
    path = tmp_path / "client.whl"
    path.write_bytes(b"not a wheel")
    with pytest.raises(PublishError) as error:
        Distribution("https://publish.example", client_wheel=path)
    assert error.value.code == "invalid_client_release"


def test_unified_installer_runs_absolute_setup_and_keeps_prompt_visible(tmp_path, monkeypatch, capsys, client_wheel):
    from types import SimpleNamespace
    distribution = Distribution('https://publish.example', client_wheel=client_wheel)
    script = tmp_path / 'installer.py'
    script.write_bytes(distribution.asset('install-client.py')[0])
    destination = tmp_path / 'path with spaces' / 'venv'

    class Download(io.BytesIO):
        status = 200
    class Opener:
        def open(self, *args, **kwargs): return Download(client_wheel.read_bytes())
    class Builder:
        def __init__(self, **kwargs): pass
        def create(self, path):
            binary = path / ('Scripts' if sys.platform == 'win32' else 'bin')
            binary.mkdir(parents=True)
            (binary / ('minder-publish.exe' if sys.platform == 'win32' else 'minder-publish')).touch()
            (binary / ('python.exe' if sys.platform == 'win32' else 'python')).touch()
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if '-I' in command:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'prefix': str(destination), 'version': [3, 11]}))
        if 'setup' in command:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'data': {'authenticated': True}}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    monkeypatch.setattr('venv.EnvBuilder', Builder)
    monkeypatch.setattr('subprocess.run', run)
    monkeypatch.setattr(sys, 'argv', [str(script), '--venv', str(destination), '--runtime', 'codex', '--connect'])
    runpy.run_path(str(script), run_name='__main__')
    setup_call, setup_options = calls[-1]
    assert Path(setup_call[0]).is_absolute() and str(destination) in setup_call[0]
    assert setup_call[1:] == ['setup', '--runtime', 'codex', '--url', 'https://publish.example', '--name', 'personal', '--json', '--connect']
    assert setup_options['stdout'] == subprocess.PIPE and 'stderr' not in setup_options and 'stdin' not in setup_options
    assert json.loads(capsys.readouterr().out)['setup']['authenticated'] is True


@pytest.mark.parametrize("symlinks", [True, False])
def test_installer_reuses_existing_venv_without_rebuilding_interpreter(tmp_path, monkeypatch, capsys, symlinks, client_wheel):
    import venv
    from types import SimpleNamespace
    distribution = Distribution('https://publish.example', client_wheel=client_wheel)
    script = tmp_path / 'installer.py'
    script.write_bytes(distribution.asset('install-client.py')[0])
    destination = tmp_path / 'venv'
    venv.EnvBuilder(with_pip=False, symlinks=symlinks).create(destination)
    python = destination / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python3')
    if symlinks and not python.is_symlink():
        pytest.skip('This platform did not create a symlink interpreter')
    original = python.readlink() if python.is_symlink() else hashlib.sha256(python.read_bytes()).hexdigest()
    original_config = (destination / 'pyvenv.cfg').read_bytes()
    cli = python.parent / ('minder-publish.exe' if sys.platform == 'win32' else 'minder-publish')
    cli.touch()

    class Download(io.BytesIO):
        status = 200
    class Opener:
        def open(self, *args, **kwargs): return Download(client_wheel.read_bytes())
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    real_run = subprocess.run
    interpreter_runs = real_run([str(python), '-I', '-c', 'pass'], capture_output=True).returncode == 0
    pip_calls = []
    def fake_run(command, **kwargs):
        if '-I' in command:
            return real_run(command, **kwargs)
        pip_calls.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(sys, 'argv', [str(script), '--venv', str(destination)])
    if interpreter_runs:
        runpy.run_path(str(script), run_name='__main__')
        assert len(pip_calls) == 1
    else:
        with pytest.raises(SystemExit) as error:
            runpy.run_path(str(script), run_name='__main__')
        assert error.value.code == 2
        assert 'select another --venv path' in capsys.readouterr().err
        assert not pip_calls
    assert (python.readlink() if python.is_symlink() else hashlib.sha256(python.read_bytes()).hexdigest()) == original
    assert (destination / 'pyvenv.cfg').read_bytes() == original_config


def test_installer_creates_a_runnable_environment(tmp_path, monkeypatch, capsys, client_wheel):
    import os

    distribution = Distribution('https://publish.example', client_wheel=client_wheel)
    script = tmp_path / 'installer.py'
    script.write_bytes(distribution.asset('install-client.py')[0])
    destination = tmp_path / 'venv'

    class Download(io.BytesIO):
        status = 200

    class Opener:
        def open(self, *args, **kwargs):
            return Download(client_wheel.read_bytes())

    real_run = subprocess.run
    installed = []

    def run(command, **kwargs):
        if command[1:4] == ['-m', 'pip', 'install']:
            python = Path(command[0])
            installed.append(python)
            real_run([str(python), '-m', 'pip', '--version'], check=True, capture_output=True)
            cli = python.with_name('minder-publish.exe' if os.name == 'nt' else 'minder-publish')
            cli.touch()
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, **kwargs)

    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(sys, 'argv', [str(script), '--venv', str(destination)])
    runpy.run_path(str(script), run_name='__main__')

    assert json.loads(capsys.readouterr().out)['installed'] is True
    assert len(installed) == 1
    if os.name == 'posix':
        assert installed[0].is_symlink()


@pytest.mark.parametrize('probe_result', [
    {'prefix': '/some/other/environment', 'version': [3, 11]},
    {'prefix': None, 'version': [3, 9]},
])
def test_existing_incompatible_venv_is_rejected_before_pip(tmp_path, monkeypatch, probe_result, client_wheel):
    import venv
    from types import SimpleNamespace
    distribution = Distribution('https://publish.example', client_wheel=client_wheel)
    script = tmp_path / 'installer.py'
    script.write_bytes(distribution.asset('install-client.py')[0])
    destination = tmp_path / 'venv'
    venv.EnvBuilder(with_pip=False, symlinks=False).create(destination)
    probe_result = dict(probe_result)
    if probe_result['prefix'] is None:
        probe_result['prefix'] = str(destination)

    class Download(io.BytesIO):
        status = 200
    class Opener:
        def open(self, *args, **kwargs): return Download(client_wheel.read_bytes())
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        assert '-I' in command, 'pip must not run for an incompatible environment'
        return SimpleNamespace(returncode=0, stdout=json.dumps(probe_result))
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(sys, 'argv', [str(script), '--venv', str(destination)])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(script), run_name='__main__')
    assert error.value.code == 2 and len(calls) == 1


@pytest.mark.parametrize(
    "unexpected_path",
    [
        "minder_publish/server/app.py",
        "minder_publish/mcp/server.py",
        "minder_publish/server/web/index.html",
        "minder_publish/server/migrations/core/001_up.sql",
        f"minder_publish-{__version__}.data/scripts/server",
    ],
)
def test_nonclient_members_block_public_downloads(tmp_path, client_wheel, unexpected_path):
    path = tmp_path / "client.whl"
    path.write_bytes(client_wheel.read_bytes())
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(unexpected_path, b"unexpected")

    with pytest.raises(PublishError) as error:
        Distribution("https://publish.example", client_wheel=path)
    assert error.value.code == "invalid_client_release"
