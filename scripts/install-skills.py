#!/usr/bin/env python3
"""Portable source-release skill installer. Dry-run is the default."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minder_publish.client.errors import ClientError  # noqa: E402 - source-release bootstrap
from minder_publish.client.skills import install  # noqa: E402 - source-release bootstrap


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=["codex", "claude", "all"], required=True)
    parser.add_argument(
        "--home", type=Path, default=Path.home(), help="runtime home directory"
    )
    parser.add_argument(
        "--apply", action="store_true", help="apply the shown changes; default: dry-run"
    )
    args = parser.parse_args()
    try:
        plans = install(
            ROOT / "skill" / "minder-publish" / "SKILL.md",
            home=args.home,
            runtimes=list(
                ("codex", "claude") if args.runtime == "all" else (args.runtime,)
            ),
            apply=args.apply,
        )
        for plan in plans:
            print(
                f"{plan['runtime']}: {'updated' if plan['applied'] else 'would update' if plan['changed'] else 'unchanged'} {plan['path']}"
            )
            print(plan["diff"], end="")
        return 0
    except (ClientError, OSError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
