"""CLI that installs MANSIO into agent runtimes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mansio_client import __version__
from mansio_client.env import env_or
from mansio_client.transport import HttpTransport, MansioAPIError

from . import config, doctor, poll, runtimes


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mansio-agent",
        description="Install and diagnose MANSIO in agent CLIs",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="Install the MANSIO MCP server and hook")
    install.add_argument("runtime", choices=sorted(runtimes.RUNTIMES))
    install.add_argument("--scope", choices=["project", "user"], default="project")
    install.add_argument("--url", default=None, help="Server URL (or MANSIO_URL)")
    install.add_argument(
        "--id", dest="user_id", default=None, help="User id (reuses the installed value by default)"
    )
    install.add_argument("--channel", default="agents", help="Default channel (default: agents)")
    install.add_argument("--token", default=None, help="API token; written only with --write-token")
    install.add_argument(
        "--token-env", action="store_true", help="Read the token from MANSIO_TOKEN"
    )
    install.add_argument("--write-token", action="store_true", help="Write --token into the config")
    install.add_argument(
        "--create-channel", action="store_true", help="Create --channel through the API"
    )
    install.add_argument("--dry-run", action="store_true", help="Print the diff; write nothing")

    poll_parser = sub.add_parser("poll", help="Poll for unread messages (hook command)")
    poll_parser.add_argument(
        "--channels", default=None, help="Comma-separated channels (or MANSIO_CHANNELS)"
    )
    poll_parser.add_argument("--format", choices=["claude", "codex", "plain"], default="plain")
    poll_parser.add_argument("--token", default=None, help="API token (or MANSIO_TOKEN)")

    doctor_parser = sub.add_parser("doctor", help="Check the installation and connection")
    doctor_parser.add_argument("--url", default=None, help="Server URL (or MANSIO_URL)")
    doctor_parser.add_argument(
        "--id", dest="user_id", default=None, help="User id (or MANSIO_USER_ID)"
    )
    doctor_parser.add_argument("--token", default=None, help="API token (or MANSIO_TOKEN)")

    uninstall = sub.add_parser("uninstall", help="Remove the MANSIO MCP server and hook")
    uninstall.add_argument("runtime", choices=sorted(runtimes.RUNTIMES))
    uninstall.add_argument("--scope", choices=["project", "user"], default="project")
    uninstall.add_argument("--dry-run", action="store_true", help="Print the diff; write nothing")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the ``mansio-agent`` command."""
    args = _build_parser().parse_args(argv)
    handlers = {
        "install": _install,
        "poll": _poll,
        "doctor": _doctor,
        "uninstall": _uninstall,
    }
    code = handlers[args.command](args)
    if code:
        sys.exit(code)


def _install(args: argparse.Namespace) -> int:
    spec = runtimes.get_runtime(args.runtime)
    url = args.url or env_or("MANSIO_URL")
    if not url:
        return _error("--url or MANSIO_URL is required")
    if args.token and args.token_env:
        return _error("use either --token or --token-env, not both")
    if args.write_token and not args.token:
        return _error("--write-token requires --token")

    token_for_api: str | None = None
    token_for_write: str | None = None
    if args.token:
        token_for_api = args.token
        if args.write_token:
            token_for_write = args.token
    elif args.token_env:
        token_for_api = env_or("MANSIO_TOKEN")
        if not token_for_api:
            return _error("--token-env requires MANSIO_TOKEN to be set")

    existing = runtimes.read_user_id(spec, args.scope)
    if args.user_id:
        if not config.is_valid_user_id(args.user_id):
            return _error(f"invalid user id {args.user_id!r}; expected {config.USER_ID_RE.pattern}")
        user_id = args.user_id
    elif existing:
        user_id = existing
    else:
        used = runtimes.used_user_ids(spec)
        user_id = config.generate_user_id(spec.name, config.project_slug(Path.cwd()), used)

    try:
        results = runtimes.install(
            spec,
            args.scope,
            url=url,
            user_id=user_id,
            token=token_for_write,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        return _error(str(exc))

    _print_results(results, dry_run=args.dry_run)
    print(f"user id: {user_id}")
    print(f"url: {url}")

    if args.create_channel and not args.dry_run:
        code = _create_channel(url, user_id, token_for_api, args.channel)
        if code:
            return code

    if not args.dry_run:
        _print_next_steps(
            spec,
            token_written=token_for_write is not None,
            token_available=token_for_api is not None,
        )
    return 0


def _create_channel(url: str, user_id: str, token: str | None, channel: str) -> int:
    transport = HttpTransport(url, user_id=user_id, token=token)
    try:
        transport.channel_create(channel, user_id)
    except MansioAPIError as exc:
        if exc.status_code == 403:
            print(
                f"channel '{channel}': permission denied (HTTP 403). "
                "Create it in the admin panel or use an owner token.",
                file=sys.stderr,
            )
            return 1
        if exc.status_code == 409:
            print(f"channel: {channel} already exists")
            return 0
        print(f"channel '{channel}': HTTP {exc.status_code}: {exc}", file=sys.stderr)
        return 1
    except NotImplementedError:
        print(
            f"channel '{channel}': backend does not support channel management",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"channel '{channel}': {exc}", file=sys.stderr)
        return 1
    finally:
        transport.close()
    print(f"channel: {channel} ready")
    return 0


def _print_results(results: list[config.WriteResult], *, dry_run: bool) -> None:
    for result in results:
        if not result.changed:
            print(f"unchanged: {result.path}")
        elif dry_run:
            print(result.diff, end="" if result.diff.endswith("\n") else "\n")
        else:
            print(f"wrote: {result.path}")
            if result.backup is not None:
                print(f"backup: {result.backup}")


def _print_next_steps(
    spec: runtimes.RuntimeSpec, *, token_written: bool, token_available: bool
) -> None:
    print("next steps:")
    step = 1
    if token_written:
        print(f"  {step}. token written to the config env block")
    else:
        suffix = "" if token_available else " (no token was provided)"
        print(f"  {step}. export MANSIO_TOKEN in the environment that launches the agent{suffix}")
    step += 1
    if spec.name == "codex":
        print(f"  {step}. review the hook in Codex with /hooks before it runs")
        step += 1
    print(f"  {step}. run: mansio-agent doctor")


def _poll(args: argparse.Namespace) -> int:
    token = args.token or env_or("MANSIO_TOKEN")
    channels = poll.parse_channels(args.channels)
    return poll.run(poll_format=args.format, channels=channels, token=token)


def _doctor(args: argparse.Namespace) -> int:
    return doctor.run(
        url=args.url or env_or("MANSIO_URL"),
        user_id=args.user_id or env_or("MANSIO_USER_ID"),
        token=args.token or env_or("MANSIO_TOKEN"),
    )


def _uninstall(args: argparse.Namespace) -> int:
    spec = runtimes.get_runtime(args.runtime)
    try:
        results = runtimes.uninstall(spec, args.scope, dry_run=args.dry_run)
    except ValueError as exc:
        return _error(str(exc))
    if not results:
        print("nothing to remove")
        return 0
    _print_results(results, dry_run=args.dry_run)
    return 0


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1
