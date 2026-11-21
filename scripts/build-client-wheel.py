#!/usr/bin/env python3
"""Build a standalone CLI wheel with the canonical CLI code and skill sources."""

from __future__ import annotations

import argparse
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def release_project(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as source:
        configuration = tomllib.load(source)
    project = configuration["project"]
    setuptools = configuration["tool"]["setuptools"]
    lines = [
        "[build-system]",
        *(
            f"{key} = {json.dumps(value)}"
            for key, value in configuration["build-system"].items()
        ),
        "",
        "[project]",
        *(
            f"{key} = {json.dumps(project[key])}"
            for key in (
                "name",
                "dynamic",
                "requires-python",
                "dependencies",
            )
        ),
        'license = "MIT"',
        'license-files = ["LICENSE"]',
        'description = "Command-line client for Minder Publish"',
        "",
        "[project.scripts]",
        f"minder-publish = {json.dumps(project['scripts']['minder-publish'])}",
        "",
        "[tool.setuptools.dynamic]",
        f"version = {{ attr = {json.dumps(setuptools['dynamic']['version']['attr'])} }}",
        "",
        "[tool.setuptools.packages.find]",
        'where = ["src"]',
        "",
        "[tool.setuptools.data-files]",
        *(
            f"{json.dumps(key)} = {json.dumps(value)}"
            for key, value in setuptools["data-files"].items()
        ),
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", default=Path("dist/client"), type=Path)
    options = parser.parse_args()
    output = options.wheel_dir.resolve()
    with tempfile.TemporaryDirectory(prefix="minder-publish-client-") as directory:
        project = Path(directory)
        (project / "pyproject.toml").write_text(release_project(ROOT), encoding="utf-8")
        shutil.copyfile(ROOT / "LICENSE", project / "LICENSE")
        source = ROOT / "src"
        package = source / "minder_publish"
        boundary = runpy.run_path(str(package / "server/client_wheel.py"))
        required = boundary["REQUIRED_CLIENT_FILES"]
        optional = {
            module.relative_to(source).as_posix()
            for module in (package / "client").glob("*.py")
            if boundary["is_client_module"](module.relative_to(source).as_posix())
        }
        for module in sorted(required | optional):
            destination = project / "src" / module
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / module, destination)
        shutil.copytree(ROOT / "skill/minder-publish", project / "skill/minder-publish")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(output),
                ".",
            ],
            cwd=project,
            check=True,
        )


if __name__ == "__main__":
    main()
