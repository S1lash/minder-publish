"""Public code downloads and credential-free, instance-specific setup instructions."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from minder_publish import __version__
from minder_publish.client.config import validate_base_url
from minder_publish.client.preflight import open_regular
from minder_publish.client.skills import read_skill_files
from minder_publish.errors import PublishError

from .client_wheel import MAX_WHEEL_BYTES, validate_client_wheel

WHEEL_NAME = f"minder_publish-{__version__}-py3-none-any.whl"
INSTALLER_NAME = "install-client.py"
SKILL_NAME = "minder-publish-skill.zip"
PLUGIN_NAME = "minder-publish-plugin.zip"


def _json(value: dict) -> bytes:
    return (
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode()


def _archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()


def _wheel(path: Path) -> bytes:
    try:
        with open_regular(path) as source:
            content = source.read(MAX_WHEEL_BYTES + 1)
        if len(content) > MAX_WHEEL_BYTES:
            raise ValueError("wheel size")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            validate_client_wheel(archive, __version__)
        return content
    except Exception as error:
        raise PublishError(
            "invalid_client_release",
            500,
            "Expected the CLI-only wheel built for this server release.",
        ) from error


INSTALLER = r'''#!/usr/bin/env python3
"""Install this instance's SHA-256-verified client in a user-owned virtual environment.

Requires Python 3.10+. This never embeds a token or changes global PATH.
--connect delegates secret entry to the CLI's private terminal prompt.
Dependencies are installed from the wheel's declared requirements by pip.
"""
import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import urllib.request
import venv
from pathlib import Path

WHEEL_URL = __WHEEL_URL__
WHEEL_SHA256 = __WHEEL_SHA256__
WHEEL_BYTES = __WHEEL_BYTES__
WHEEL_NAME = __WHEEL_NAME__
INSTANCE_URL = __INSTANCE_URL__

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("The release download redirected; inspect the instance release before retrying.")

def default_venv():
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return root / "minder-publish" / "venv"

def check_destination(path):
    for item in (path, *path.parents):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("The installation path contains a symlink or a non-directory.")
    if path.exists() and not (path / "pyvenv.cfg").is_file():
        raise ValueError("The destination exists and is not a virtual environment; select another --venv path.")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=default_venv())
    parser.add_argument("--runtime", choices=("codex", "claude"))
    parser.add_argument("--name", default="personal")
    parser.add_argument("--connect", action="store_true", help="ask for a token in a private interactive terminal if needed")
    args = parser.parse_args()
    if args.connect and not args.runtime:
        parser.error("--connect requires --runtime")
    if sys.version_info < (3, 10):
        raise ValueError("Python 3.10 or later is required.")
    with tempfile.TemporaryDirectory(prefix="minder-publish-client-") as temporary:
        wheel = Path(temporary) / WHEEL_NAME
        digest = hashlib.sha256()
        total = 0
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(WHEEL_URL, timeout=30) as response, wheel.open("xb") as output:
            if response.status != 200:
                raise ValueError("The release download did not return HTTP 200.")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > WHEEL_BYTES:
                    raise ValueError("The release download exceeds the expected size.")
                digest.update(chunk)
                output.write(chunk)
        if total != WHEEL_BYTES or digest.hexdigest() != WHEEL_SHA256:
            raise ValueError("Release SHA-256 verification failed; no environment was changed.")
        destination = args.venv.expanduser().absolute()
        check_destination(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        binary = destination / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        if not destination.exists():
            venv.EnvBuilder(with_pip=True, symlinks=os.name == "posix").create(destination)
        # Updating packages must preserve the existing interpreter and its links.
        if not python.is_file():
            raise ValueError("The existing environment has no Python executable; select another --venv path.")
        try:
            probe = subprocess.run([
                str(python), "-I", "-c",
                "import json,sys; print(json.dumps({'prefix':sys.prefix,'version':list(sys.version_info[:2])}))"
            ], text=True, stdout=subprocess.PIPE, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValueError(f"Python could not start in {destination}; select another --venv path.") from error
        environment = json.loads(probe.stdout)
        if Path(environment["prefix"]).resolve() != destination.resolve() or tuple(environment["version"]) < (3, 10):
            raise ValueError("The existing environment must use Python 3.10+ and belong to the selected destination; select another --venv path.")
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--force-reinstall", str(wheel)], check=True)
        cli = binary / ("minder-publish.exe" if os.name == "nt" else "minder-publish")
        if not cli.is_file():
            raise ValueError("The client executable is absent after installation.")
        result = {"installed": True, "executable": str(cli), "instance_url": INSTANCE_URL,
            "configure_command": [str(cli), "configure", args.name, "--url", INSTANCE_URL]}
        if args.runtime:
            command = [str(cli), "setup", "--runtime", args.runtime, "--url", INSTANCE_URL, "--name", args.name, "--json"]
            if args.connect:
                command.append("--connect")
            # Keep stderr attached to the user's terminal for visible private prompts.
            completed = subprocess.run(command, text=True, stdout=subprocess.PIPE)
            outcome = json.loads(completed.stdout)
            if completed.returncode:
                print(json.dumps(outcome))
                raise SystemExit(completed.returncode)
            result["setup"] = outcome["data"]
        print(json.dumps(result))

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Client installation failed: " + str(error), file=sys.stderr)
        raise SystemExit(2)
'''


class Distribution:
    """Build allowlisted public code assets; never embed instance credentials."""

    def __init__(
        self,
        base_url: str,
        *,
        client_wheel: Path | None = None,
        skill_source: Path | None = None,
    ):
        self.base_url = validate_base_url(base_url)
        self.files = read_skill_files(skill_source)
        self._assets: dict[str, tuple[bytes, str]] = {
            SKILL_NAME: (
                _archive(
                    {
                        f"minder-publish/{name}": value
                        for name, value in self.files.items()
                    }
                ),
                "application/zip",
            ),
            PLUGIN_NAME: (self._plugin(), "application/zip"),
        }
        self.client = {"available": False, "version": __version__}
        if client_wheel is not None:
            content = _wheel(Path(client_wheel))
            self._assets[WHEEL_NAME] = (content, "application/octet-stream")
            self.client = {
                "available": True,
                "version": __version__,
                **self._describe(WHEEL_NAME),
            }
            self._assets[INSTALLER_NAME] = (
                self._installer(),
                "text/x-python; charset=utf-8",
            )

    def _plugin(self) -> bytes:
        files = {
            f"skills/minder-publish/{name}": value for name, value in self.files.items()
        }
        files["plugin.json"] = _json(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "minder-publish",
                "version": __version__,
                "description": "Publish finished files and static sites to your own instance.",
                "license": "MIT",
            }
        )
        files["mcp.json"] = _json(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "publish": {
                        "type": "streamable-http",
                        "url": self.base_url + "/mcp",
                        "extensions": {
                            "com.openai": {
                                "auth": {
                                    "type": "oauth",
                                    "client": {"mode": "dcr"},
                                    "defaultScopes": ["materials:manage"],
                                }
                            }
                        },
                    }
                },
            }
        )
        return _archive(files)

    def _describe(self, name: str) -> dict:
        content, _ = self._assets[name]
        return {
            "filename": name,
            "url": self.base_url + "/downloads/" + name,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def _installer(self) -> bytes:
        substitutions = {
            "__WHEEL_URL__": json.dumps(self.client["url"]),
            "__WHEEL_SHA256__": json.dumps(self.client["sha256"]),
            "__WHEEL_BYTES__": str(self.client["size_bytes"]),
            "__WHEEL_NAME__": json.dumps(WHEEL_NAME),
            "__INSTANCE_URL__": json.dumps(self.base_url),
        }
        result = INSTALLER
        for name, value in substitutions.items():
            result = result.replace(name, value)
        return result.encode("utf-8")

    def metadata(self) -> dict:
        return {
            "version": __version__,
            "instance_url": self.base_url,
            "mcp_url": self.base_url + "/mcp",
            "client": dict(self.client),
            "assets": {name: self._describe(name) for name in sorted(self._assets)},
            "install_prompts": {
                runtime: self.install_prompt(runtime) for runtime in ("codex", "claude")
            }
            if self.client["available"]
            else {},
            "skill_prompts": {
                runtime: self.skill_prompt(runtime) for runtime in ("codex", "claude")
            },
            "cloud_setup": {
                "plugin_requires_host_installation_and_authenticated_mcp": True,
                "zip_download_does_not_connect_a_cloud_account": True,
            },
        }

    def asset(self, name: str) -> tuple[bytes, str]:
        if name not in self._assets:
            raise PublishError("not_found", 404)
        return self._assets[name]

    def install_prompt(self, runtime: str) -> str:
        if runtime not in ("codex", "claude"):
            raise PublishError("invalid_runtime", 400, "Choose Codex or Claude Code.")
        if not self.client["available"]:
            raise PublishError(
                "client_release_unavailable",
                503,
                "The operator must provide this release's client wheel.",
            )
        asset = self._describe(INSTALLER_NAME)
        return (
            f"Set up Minder Publish for {'Codex' if runtime == 'codex' else 'Claude Code'} on this computer: install both the CLI and its publishing skill.\n"
            f"Instance: {self.base_url}\n"
            "Use Python 3.10 or later on Linux or macOS. Download the installer to a temporary file with Python's urllib.request, "
            f"read it, and verify its SHA-256 before running it with that interpreter and --runtime {runtime}. Do not pipe downloaded code into a shell.\n"
            f"Installer URL: {asset['url']}\nInstaller SHA-256: {asset['sha256']}\n"
            f"The installer verifies the client wheel SHA-256: {self.client['sha256']}. "
            "It installs a per-user virtual environment and reports the absolute CLI executable; it does not change global PATH. "
            "Inspect any existing installation before updating it. Dependencies resolve through the wheel's declared pip requirements.\n"
            "The verified release and its dependencies are reinstalled inside that private environment, including when its version is unchanged.\n"
            "The runtime setup installs the managed skill, records the local executable for future chats, and checks an existing matching instance without replacing its token. "
            "Preserve unmanaged skill files and stop on a conflict.\n"
            "Read the final JSON setup result. If authenticated is true, report completion without requesting another token. "
            "If needs_token is true, hand the reported connection_command to a private user-interactive terminal. "
            "The user enters one device token into the hidden prompt; this command validates and saves it and reports the connection result. "
            "If the runtime cannot hand over a private terminal, show the exact connection_shell_command and its shell so the user can run it there. "
            "Never request a token in chat or expose it in tool arguments, logs or skills. Do not invent a PATH-dependent command. "
            "Use the reported absolute executable and explicit --instance followed by the returned instance name for every later operation. "
            "The generated skill reference records that instance and expected URL; confirm doctor returns that URL before publishing. "
            "Setup preserves an unrelated global default. Start a fresh chat if the runtime has not refreshed its skill catalog; "
            "report the installed skill path and actual connection result, not inferred success."
        )

    def skill_prompt(self, runtime: str) -> str:
        if runtime not in ("codex", "claude"):
            raise PublishError("invalid_runtime", 400, "Choose Codex or Claude Code.")
        invocation = (
            "/minder:publish in Claude Code"
            if runtime == "claude"
            else "$minder-publish in Codex"
        )
        return (
            "Use the installed Minder Publish CLI in this computer's environment. "
            f"The intended server is {self.base_url}. Inspect redacted config to select its existing named instance; "
            "never read token files. If several profiles match, ask which one; if none match, use the combined setup instructions. "
            f"Run minder-publish --instance NAME skills --runtime {runtime}, replacing NAME with that selected profile, to inspect the proposed managed-skill changes. "
            "Preserve unmanaged files and stop on a conflict. "
            f"Then run minder-publish --instance NAME skills --runtime {runtime} --apply with the same selected profile to install the reviewed skill and references. "
            "This selects the skill's preferred instance without changing the CLI global default. "
            f"Invoke it as {invocation}. "
            "Report the actual installed path. No token or owner password belongs in the skill. "
            "If the executable is not on PATH, use the absolute executable reported by the client installer."
        )
