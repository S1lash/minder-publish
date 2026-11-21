"""Publish files and explicitly selected static sites to your own Minder Publish instance."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import webbrowser
from pathlib import Path

from . import config
from .api import PublishClient, duration_seconds, expiry
from .errors import ClientError


def _secret(
    *,
    stdin: bool = False,
    file: str | None = None,
    prompt: str = "Secret: ",
    field: str = "token",
) -> str:
    if file:
        path = Path(file).expanduser()
        return (
            config._read_token(path)
            if field == "token"
            else config.read_private_secret_file(path, field=field)
        )
    if stdin:
        value = sys.stdin.readline(8194)
        if len(value) > 8193:
            raise ClientError("secret_too_long", "Secret exceeds the input limit.")
        return value.rstrip("\r\n")
    if not sys.stdin.isatty():
        raise ClientError(
            "secret_required",
            "Use the explicit stdin or private-file option in a noninteractive session.",
        )
    return getpass.getpass(prompt)


def _metadata(args) -> dict:
    changes = {}
    for field in (
        "key",
        "password_hint",
        "title",
        "summary",
        "change_summary",
        "access",
        "purge_mode",
        "metadata_policy",
        "site_routing",
        "external_resources",
        "preview",
        "indexing",
        "guest_counts",
        "owner_counts",
    ):
        value = getattr(args, field, None)
        if value is not None:
            changes[field] = value
    if getattr(args, "expiry", None) is not None:
        changes["expiry"] = expiry(args.expiry)
    for argument, field in (
        ("purge_after", "purge_after_seconds"),
        ("metadata_after", "metadata_after_purge_seconds"),
    ):
        if getattr(args, argument, None) is not None:
            changes[field] = duration_seconds(getattr(args, argument))
    if (
        getattr(args, "password_stdin", False)
        or getattr(args, "password_file", None)
        or getattr(args, "password_prompt", False)
    ):
        changes["password"] = _secret(
            stdin=args.password_stdin,
            file=args.password_file,
            prompt="Publication password: ",
            field="password",
        )
        if not changes["password"]:
            raise ClientError(
                "password_required", "Publication password must not be empty."
            )
        changes.setdefault("access", "password")
    return changes


def _safe(value) -> str:
    return "".join(
        character if character.isprintable() else " " for character in str(value)
    )


def _print_data(data, as_json: bool):
    if as_json:
        print(json.dumps({"data": data}, ensure_ascii=False))
    elif (
        isinstance(data, dict)
        and data.get("stable_url")
        and "access" in data
        and "expires_at" in data
    ):
        print(_safe(data["stable_url"]))
        print(
            f"id={_safe(data.get('id', ''))} access={_safe(data.get('access', ''))} expires={_safe(data.get('expires_at') or 'never')}",
            file=sys.stderr,
        )
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ClientError("invalid_arguments", message)


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(prog="minder-publish", description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="machine-readable data/error envelope"
    )
    parser.add_argument("--instance", help="named configured instance")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name, help):
        child = sub.add_parser(name, help=help)
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        child.add_argument("--instance", default=argparse.SUPPRESS)
        return child

    def metadata(child):
        child.add_argument(
            "--hint",
            dest="password_hint",
            help="public password clue; empty string clears it",
        )
        child.add_argument("--title")
        child.add_argument(
            "--summary", help="optional description; empty string clears on patch"
        )
        child.add_argument("--access", choices=["private", "link", "password"])
        child.add_argument(
            "--expiry",
            "--ttl",
            dest="expiry",
            help="default, never, 7d, or RFC3339 with timezone",
        )
        password = child.add_mutually_exclusive_group()
        password.add_argument("--password-stdin", action="store_true")
        password.add_argument(
            "--password-file",
            help="owner-only file containing the publication password",
        )
        password.add_argument(
            "--password-prompt",
            action="store_true",
            help="hidden interactive password prompt",
        )
        child.add_argument("--purge-mode", choices=["immediate", "after", "never"])
        child.add_argument("--purge-after", help="recovery duration such as 30d or 12h")
        child.add_argument(
            "--metadata-policy", choices=["keep", "with_content", "after_purge"]
        )
        child.add_argument(
            "--metadata-after", help="card retention after file cleanup, e.g. 365d"
        )
        child.add_argument("--site-routing", choices=["strict", "spa"])
        child.add_argument("--external-resources", choices=["allow", "self_only"])
        child.add_argument("--preview", choices=["auto", "download"])
        child.add_argument(
            "--indexing", action=argparse.BooleanOptionalAction, default=None
        )
        child.add_argument(
            "--guest-counts", action=argparse.BooleanOptionalAction, default=None
        )
        child.add_argument(
            "--owner-counts", action=argparse.BooleanOptionalAction, default=None
        )

    onboarding = command("setup", "install the publishing skill and verify or connect this client")
    onboarding.add_argument("--runtime", required=True, choices=["codex", "claude"])
    onboarding.add_argument("--url", required=True)
    onboarding.add_argument("--name", default="personal")
    onboarding.add_argument("--connect", action="store_true", help="ask for a token in a private interactive terminal if not configured")
    setup = command("configure", "connect a named instance using a device token")
    setup.add_argument("name")
    setup.add_argument("--url", required=True)
    credentials = setup.add_mutually_exclusive_group()
    credentials.add_argument("--token-stdin", action="store_true")
    credentials.add_argument("--token-file")
    setup.add_argument("--no-default", action="store_true")
    skills = command(
        "skills", "install the managed Codex/Claude skill; dry-run by default"
    )
    skills.add_argument("--runtime", required=True, choices=["codex", "claude", "all"])
    skills.add_argument("--home", type=Path, default=Path.home())
    skills.add_argument("--apply", action="store_true")
    command("config", "show redacted named-instance configuration")
    doctor = command("doctor", "check configuration, authentication and server capabilities")
    doctor.add_argument("--brief", action="store_true", help="return connection identity and supported kinds without full policy limits")

    for name in ("publish", "preflight"):
        item = command(
            name,
            "prepare and publish"
            if name == "publish"
            else "validate files without uploading content",
        )
        item.add_argument("path")
        item.add_argument("--kind", required=True, choices=["file", "site"])
        item.add_argument("--entry", default="index.html")
        item.add_argument(
            "--include-sensitive",
            action="store_true",
            help="explicitly include excluded names; inspect preflight first",
        )
        if name == "publish":
            item.add_argument("--expect-manifest", help="require the reviewed preflight manifest_sha256 before uploading")
            item.add_argument("--change-summary", help="optional note for this revision")
            item.add_argument(
                "--key", help="choose the initial lowercase link name (2–64 characters)"
            )
            item.add_argument("--dry-run", action="store_true")
            metadata(item)
    update = command(
        "update", "atomically replace content while preserving omitted policy fields"
    )
    update.add_argument("id", help="publication ID returned by list or info")
    update.add_argument("path")
    update.add_argument(
        "--kind", choices=["file", "site"], help="default: existing publication kind"
    )
    update.add_argument("--entry", default=None)
    update.add_argument("--expected-revision")
    update.add_argument("--expect-manifest", help="require the reviewed preflight manifest_sha256 before uploading")
    update.add_argument("--change-summary", help="what changed in this revision")
    update.add_argument("--include-sensitive", action="store_true")
    update.add_argument("--dry-run", action="store_true")
    metadata(update)
    patch = command("set", "patch selected metadata and policy fields")
    patch.add_argument("id")
    metadata(patch)
    listing = command("list", "find materials by title, summary or filename")
    listing.add_argument("--query", "-q", default="")
    listing.add_argument("--kind", choices=["file", "site"])
    listing.add_argument("--status", choices=["active", "paused", "expired", "purged"])
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=50)
    upload_status = command(
        "upload-status",
        "read a historical upload outcome; missing does not prove failure",
    )
    upload_status.add_argument("id")
    for name in (
        "info",
        "open",
        "pause",
        "resume",
        "expire",
        "delete",
        "extend",
        "download",
    ):
        child = command(name, f"{name} a material")
        child.add_argument("id")
        if name == "extend":
            child.add_argument(
                "duration", help="added to max(now, current expiry), e.g. 7d"
            )
        if name == "delete":
            child.add_argument(
                "--yes", action="store_true", help="confirm permanent deletion"
            )
        if name == "download":
            child.add_argument("destination")
            child.add_argument("--overwrite", action="store_true")
    return parser


def run(args):
    if args.command == "setup":
        from .setup import setup

        return setup(
            args.runtime, args.url, args.name, connect=args.connect,
            read_token=lambda: _secret(prompt="Device token: "),
        )
    if args.command == "configure":
        return config.save_instance(
            args.name,
            args.url,
            _secret(
                stdin=args.token_stdin, file=args.token_file, prompt="Device token: "
            ),
            make_default=not args.no_default,
        )
    if args.command == "skills":
        from .skills import canonical_source, install

        runtimes = ["codex", "claude"] if args.runtime == "all" else [args.runtime]
        saved = config.read_config()
        instance_name = args.instance or saved.get("default_instance")
        instance_url = None
        if instance_name is not None:
            instance = saved.get("instances", {}).get(instance_name)
            if not isinstance(instance, dict) or not isinstance(instance.get("base_url"), str):
                raise ClientError("invalid_config", "Choose a configured instance for the skill with --instance NAME.")
            instance_url = config.validate_base_url(instance["base_url"])
        plans = install(
            canonical_source(), home=args.home, runtimes=runtimes, apply=args.apply,
            instance_name=instance_name, instance_url=instance_url,
        )
        if not args.json:
            for plan in plans:
                print(plan["diff"], end="", file=sys.stderr)
        return plans
    if args.command == "config":
        return config.redacted_config()
    selected = config.connection(args.instance)
    with PublishClient(selected.base_url, selected.token) as client:
        if args.command == "doctor":
            capabilities = client.capabilities()
            result = {
                "instance": selected.name,
                "base_url": selected.base_url,
                "authenticated": True,
            }
            if args.brief:
                result["supported_kinds"] = capabilities.get("supported_kinds", [])
                defaults = capabilities.get("defaults", {})
                if isinstance(defaults, dict) and "external_ttl_seconds" in defaults:
                    result["defaults"] = {"external_ttl_seconds": defaults["external_ttl_seconds"]}
            else:
                result["capabilities"] = capabilities
            return result
        if args.command in ("publish", "preflight", "update"):
            current = client.get(args.id) if args.command == "update" else None
            kind = args.kind or current["kind"]
            entry = (
                args.entry
                if args.entry is not None
                else current.get("entry_path", "index.html")
            )

            def report(preflight):
                if not args.json:
                    print(
                        f"Preflight: {preflight['kind']}, {len(preflight['files'])} files, {preflight['size_bytes']} transport bytes",
                        file=sys.stderr,
                    )
                    print(f"Manifest SHA-256: {preflight['manifest_sha256']}", file=sys.stderr)
                    for item in preflight["manifest"]["files"]:
                        print(f"Included: {_safe(item['path'])} ({item['size_bytes']} bytes, {item['sha256']})", file=sys.stderr)
                    for excluded in preflight["excluded"]:
                        print(f"Excluded: {_safe(excluded)}", file=sys.stderr)
                    dependencies = preflight["dependencies"]
                    for origin in dependencies["external_origins"]:
                        print(f"External origin: {_safe(origin)}", file=sys.stderr)
                    for missing in dependencies["missing_assets"]:
                        print(f"Missing asset: {_safe(missing)}", file=sys.stderr)
                    print(
                        "Bounded static HTML/CSS hints only. Dynamic code, API calls and runtime behavior are not analyzed; no URL was fetched.",
                        file=sys.stderr,
                    )
                    if dependencies["truncated"]:
                        print(
                            "Static scan reached its safety bounds; the report is incomplete.",
                            file=sys.stderr,
                        )
                    if preflight["sensitive_override"]:
                        print(
                            "Sensitive-name exclusions explicitly overridden. Server guards still apply.",
                            file=sys.stderr,
                        )

            def inspected(value):
                if args.json:
                    return
                dependencies = value["dependency_report"]
                print(
                    f"Server preflight: {len(value.get('files', []))} validated files; ready to commit.",
                    file=sys.stderr,
                )
                for origin in dependencies.get("external_origins", []):
                    print(f"Server external origin: {_safe(origin)}", file=sys.stderr)
                for missing in dependencies.get("missing_assets", []):
                    print(f"Server missing asset: {_safe(missing)}", file=sys.stderr)
                print(
                    "Static hints remain incomplete for dynamic code. No URLs were fetched.",
                    file=sys.stderr,
                )

            return client.publish(
                args.path,
                kind=kind,
                entry_path=entry,
                allow_sensitive=args.include_sensitive,
                metadata=_metadata(args) if args.command != "preflight" else None,
                dry_run=args.command == "preflight" or args.dry_run,
                publication_id=args.id if current else None,
                expected_revision=(
                    args.expected_revision or current["current_revision"]
                )
                if current
                else None,
                expected_manifest=getattr(args, "expect_manifest", None),
                on_preflight=report,
                on_inspect=inspected,
            )
        if args.command == "list":
            if args.offset < 0 or not 1 <= args.limit <= 500:
                raise ClientError(
                    "invalid_pagination", "Use offset >= 0 and limit between 1 and 500."
                )
            return client.list(
                query=args.query,
                kind=args.kind,
                status=args.status,
                offset=args.offset,
                limit=args.limit,
            )
        if args.command == "info":
            return client.get(args.id)
        if args.command == "upload-status":
            return client.get_upload(args.id)
        if args.command == "set":
            changes = _metadata(args)
            if not changes:
                raise ClientError("empty_patch", "Choose at least one field to change.")
            return client.patch(args.id, changes)
        if args.command == "open":
            result = client.open(args.id)
            if not args.json:
                webbrowser.open(result["url"])
            return result
        if args.command in ("pause", "resume"):
            return client.pause(args.id, args.command == "pause")
        if args.command == "expire":
            return client.expire(args.id)
        if args.command == "extend":
            return client.extend(args.id, duration_seconds(args.duration))
        if args.command == "delete":
            if not args.yes:
                raise ClientError(
                    "confirmation_required",
                    "Permanent deletion requires --yes after reviewing the material.",
                )
            return client.delete(args.id)
        if args.command == "download":
            return client.download(args.id, args.destination, overwrite=args.overwrite)
    raise ClientError("unknown_command", "Unknown command.")


def main(argv: list[str] | None = None) -> int:
    json_requested = "--json" in (argv if argv is not None else sys.argv[1:])
    try:
        args = build_parser().parse_args(argv)
        result = run(args)
        _print_data(result, args.json)
        return 0
    except ClientError as error:
        if json_requested:
            print(json.dumps({"error": error.as_dict()}, ensure_ascii=False))
        else:
            print(f"error [{error.code}]: {_safe(error.detail)}", file=sys.stderr)
            if error.action:
                print(_safe(error.action), file=sys.stderr)
        return error.exit_code
    except (OSError, ValueError):
        failure = ClientError(
            "local_io_error", "A local file or configuration operation failed."
        )
        if json_requested:
            print(json.dumps({"error": failure.as_dict()}))
        else:
            print(f"error [{failure.code}]: {failure.detail}", file=sys.stderr)
        return failure.exit_code
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
