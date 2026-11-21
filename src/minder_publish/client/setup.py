"""Connect an installed client and its skill without replacing existing credentials."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from . import config
from .api import PublishClient
from .errors import ClientError
from .skills import canonical_source, claude_directory, install


def setup(
    runtime: str, url: str, name: str, *, connect: bool, read_token,
    claude_config_dir: Path | None = None,
) -> dict:
    url = config.validate_base_url(url)
    name = config._safe_name(name)
    saved = config.read_config()
    existing = saved.get("instances", {}).get(name)
    if name in saved.get("instances", {}) and not isinstance(existing, dict):
        raise ClientError("invalid_config", "The selected instance configuration is invalid.")
    if existing is not None and config.validate_base_url(existing.get("base_url", "")) != url:
        raise ClientError(
            "instance_conflict",
            "That instance name already points to another server. Choose another --name; existing credentials were not changed.",
            field="name",
        )
    claude_config_dir = claude_directory(claude_config_dir) if runtime == "claude" else None
    plans = install(
        canonical_source(), runtimes=[runtime], apply=True,
        instance_name=name, instance_url=url,
        claude_config_dir=claude_config_dir,
    )
    command = [str(config.executable_path()), "setup", "--runtime", runtime,
               "--url", url, "--name", name, "--connect"]
    if claude_config_dir is not None:
        command.extend(["--claude-config-dir", str(claude_config_dir)])
    shell_command = (
        "& " + " ".join("'" + value.replace("'", "''") + "'" for value in command)
        if os.name == "nt" else shlex.join(command)
    )
    result = {
        "instance": name, "base_url": url, "runtime": runtime,
        "executable": str(config.executable_path()),
        "skills": [{key: plan[key] for key in ("runtime", "path", "changed", "applied")} for plan in plans],
        "authenticated": False, "needs_token": False,
    }
    if existing is not None:
        selected = config.connection(name, use_environment=False)
        if selected.base_url != url:
            raise ClientError("instance_conflict", "The saved instance changed during setup. Retry using its intended server; credentials were not sent.")
        with PublishClient(url, selected.token) as client:
            client.capabilities()
        result.update(authenticated=True, reused_configuration=True)
        return result
    if not connect:
        result.update(
            needs_token=True,
            connection_command=command,
            connection_shell_command=shell_command,
            connection_shell="powershell" if os.name == "nt" else "posix",
        )
        return result
    token = read_token()
    # Verify before storing: a typo or unreachable server must not leave bad setup.
    with PublishClient(url, token) as client:
        client.capabilities()
    # Recheck under the shared writer lock after network/private-prompt latency.
    config.save_instance(name, url, token, make_default=False, require_new=True)
    result.update(authenticated=True, reused_configuration=False)
    return result
