"""Command-line interface for mansio server and client.

Subcommands:
    mansio serve   — start Bus + AdminServer (+ optional HttpFrontend)
    mansio client  — send/poll/list/dm via a remote HttpFrontend
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
import urllib.error
from typing import Any

from mansio import SQLiteBus, __version__


def _redact_token(token: str, head: int = 8, tail: int = 4) -> str:
    """Redact a token for safe logging, showing only head and tail.

    Args:
        token: The full token string.
        head: Number of leading characters to show.
        tail: Number of trailing characters to show.

    Returns:
        Redacted string like ``"sk-abcd1...xyz9"``.
    """
    if len(token) <= head + tail:
        return token
    return f"{token[:head]}...{token[-tail:]}"


def _parse_host_port(value: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    """Parse a ``[HOST:]PORT`` string.

    Args:
        value: Either ``"PORT"`` or ``"HOST:PORT"``.
        default_host: Host to use when only port is given.

    Returns:
        Tuple of (host, port).

    Raises:
        argparse.ArgumentTypeError: If the value cannot be parsed.
    """
    if ":" in value:
        parts = value.rsplit(":", 1)
        try:
            return parts[0], int(parts[1])
        except ValueError:
            raise argparse.ArgumentTypeError(f"Invalid host:port: {value!r}") from None
    try:
        return default_host, int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid port: {value!r}") from None


def _msg_to_json(msg) -> str:
    """Serialize a Message to a JSON string.

    Args:
        msg: A Message object.

    Returns:
        JSON string representation.
    """
    return json.dumps(
        {
            "id": msg.id,
            "channel": msg.channel,
            "sender": msg.sender,
            "msg_type": msg.msg_type,
            "payload": msg.payload,
            "timestamp": msg.timestamp,
            "metadata": msg.metadata,
        },
        ensure_ascii=False,
    )


# ── Argument Parsing ──────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="mansio",
        description="Mansio — agent messaging hub.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    sub = parser.add_subparsers(dest="command")

    # ── serve ─────────────────────────────────────────────────────
    serve = sub.add_parser("serve", help="Start the mansio server")
    serve.add_argument(
        "-d",
        "--db",
        default="mansio.db",
        help="SQLite database path (default: mansio.db)",
    )
    serve.add_argument(
        "--http",
        metavar="[HOST:]PORT",
        default=None,
        help="Enable HttpFrontend on [HOST:]PORT (e.g. 8742 or 0.0.0.0:8742)",
    )
    serve.add_argument(
        "--admin-port",
        type=int,
        default=8741,
        help="Admin panel port (default: 8741)",
    )
    serve.add_argument(
        "--remote",
        action="store_true",
        help="Allow remote connections for admin (binds to 0.0.0.0, enables auth)",
    )
    serve.add_argument(
        "--token",
        default=None,
        help="Admin password for admin panel (auto-generated if --remote)",
    )
    serve.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable API token auth and admin auth (for local dev)",
    )
    serve.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable admin web UI, serve API only",
    )
    serve.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    serve.add_argument(
        "--irc",
        metavar="HOST:PORT",
        default=None,
        help="Enable IrcFrontend connecting to HOST:PORT (e.g. irc.example.com:6667)",
    )
    serve.add_argument(
        "--irc-nick",
        default="mansio-bot",
        help="IRC bot nickname (default: mansio-bot)",
    )
    serve.add_argument(
        "--irc-channels",
        nargs="*",
        default=None,
        help="Mansio channels to bridge to IRC (auto-mapped to #<name>)",
    )
    serve.add_argument(
        "--irc-ssl",
        action="store_true",
        help="Use SSL/TLS for IRC connection",
    )

    # ── client ────────────────────────────────────────────────────
    client = sub.add_parser("client", help="Interact with a remote mansio server")
    client_sub = client.add_subparsers(dest="action")

    # Shared arguments for all client actions
    _client_common = argparse.ArgumentParser(add_help=False)
    _client_common.add_argument(
        "-s",
        "--server",
        required=True,
        help="Server URL (e.g. http://localhost:8742)",
    )
    _client_common.add_argument(
        "-a",
        "--agent",
        required=True,
        help="Agent ID",
    )
    _client_common.add_argument(
        "--api-token",
        default=None,
        help="Bearer token for API auth (pzt-...)",
    )

    # client send
    send = client_sub.add_parser("send", parents=[_client_common], help="Send a message")
    send.add_argument("-c", "--channel", required=True, help="Target channel")
    send.add_argument("-t", "--type", default="chat", dest="msg_type", help="Message type")
    send.add_argument("message", help="Message content")

    # client poll
    poll = client_sub.add_parser("poll", parents=[_client_common], help="Poll messages")
    poll.add_argument("-c", "--channel", required=True, help="Channel to poll")
    poll.add_argument(
        "-n",
        "--limit",
        type=int,
        default=10,
        help="Max messages to return (default: 10)",
    )
    poll.add_argument(
        "--follow",
        action="store_true",
        help="Keep listening for new messages via SSE",
    )

    # client channels
    client_sub.add_parser("channels", parents=[_client_common], help="List channels")

    # client dm
    dm = client_sub.add_parser("dm", parents=[_client_common], help="Send a direct message")
    dm.add_argument("--to", required=True, dest="to_agent", help="Recipient agent ID")
    dm.add_argument("message", help="Message content")

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    When no subcommand is given, defaults to ``serve``.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Parsed arguments namespace.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Default to serve when no subcommand given
    if args.command is None:
        return parser.parse_args(["serve"] + (argv if argv is not None else []))

    return args


# ── Subcommand Handlers ───────────────────────────────────────────


def _cmd_serve(args: argparse.Namespace) -> None:
    """Handle the ``serve`` subcommand.

    Args:
        args: Parsed arguments namespace.
    """
    import logging

    from mansio._vendor.structlog import configure, get_logger

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    configure(logger_factory=lambda *a: logging.getLogger(a[0] if a else "mansio"))
    logger = get_logger("mansio")

    # --no-auth + --remote is dangerous — refuse to start
    if args.no_auth and args.remote:
        logger.error("--no-auth cannot be used with --remote (would expose unauthenticated API)")
        sys.exit(1)

    bus = SQLiteBus(args.db)
    logger.info("Bus started", db=args.db)

    # Set up token store (unless --no-auth)
    token_store = None
    if not args.no_auth:
        from mansio.token_store import TokenStore

        token_store = TokenStore(args.db)
        token_count = len(token_store.list_tokens())
        if token_count > 0:
            logger.info("Token auth enabled", token_count=token_count)
        else:
            logger.info("Token auth ready (no tokens yet — API open until first token created)")

    # Start HttpFrontend if requested
    http_frontend = None
    if args.http:
        from mansio.frontends.http import HttpFrontend

        host, port = _parse_host_port(args.http)
        http_frontend = HttpFrontend(host=host, port=port, token_store=token_store)
        http_frontend.attach(bus)
        http_thread = threading.Thread(
            target=http_frontend.serve_forever,
            name="mansio-http-frontend",
            daemon=True,
        )
        http_thread.start()
        # Wait for server to bind (port changes from 0 to actual)
        for _ in range(50):
            _, actual_port = http_frontend.address
            if actual_port != 0:
                break
            time.sleep(0.01)
        actual_host, actual_port = http_frontend.address
        logger.info("HttpFrontend started", url=f"http://{actual_host}:{actual_port}")

    # Start IrcFrontend if requested
    irc_frontend = None
    if args.irc:
        from mansio.frontends.irc import IrcFrontend

        if ":" not in args.irc:
            logger.error("--irc requires HOST:PORT format (e.g. irc.example.com:6667)")
            sys.exit(1)
        irc_host, irc_port = _parse_host_port(args.irc)
        irc_frontend = IrcFrontend(
            irc_host=irc_host,
            irc_port=irc_port,
            nickname=args.irc_nick,
            channels=args.irc_channels or [],
            use_ssl=args.irc_ssl,
        )
        irc_frontend.attach(bus)
        irc_thread = threading.Thread(
            target=irc_frontend.serve_forever,
            name="mansio-irc-frontend",
            daemon=True,
        )
        irc_thread.start()
        logger.info(
            "IrcFrontend started",
            host=irc_host,
            port=irc_port,
            nick=args.irc_nick,
            channels=args.irc_channels or [],
        )

    # Start AdminServer
    admin_kwargs: dict[str, Any] = {
        "host": "0.0.0.0" if args.remote else "127.0.0.1",
        "port": args.admin_port,
        "serve_ui": not args.no_ui,
        "remote": args.remote,
        "token_store": token_store,
    }
    if args.token:
        admin_kwargs["auth_password"] = args.token

    info = bus.start_admin(**admin_kwargs)
    logger.info("Admin panel", url=info.url)
    if info.password:
        logger.info("Admin password", password=_redact_token(info.password))

    # Block until SIGINT/SIGTERM.
    # Signal handler only sets an Event — all cleanup runs in the main
    # thread to avoid reentrant calls into Bus/HTTPServer from a signal
    # handler context.
    stop = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    stop.wait()
    logger.info("Shutting down...")
    if irc_frontend:
        irc_frontend.shutdown()
    if http_frontend:
        http_frontend.shutdown()
    bus.close()


def _cmd_client_send(args: argparse.Namespace) -> None:
    """Handle ``client send``.

    Args:
        args: Parsed arguments namespace.
    """
    from mansio.transport_http import HttpTransport

    transport = HttpTransport(args.server, agent_id=args.agent, token=args.api_token)
    msg_id = transport.publish(args.channel, args.agent, args.msg_type, args.message)
    print(msg_id)
    transport.close()


def _cmd_client_poll(args: argparse.Namespace) -> None:
    """Handle ``client poll``.

    Args:
        args: Parsed arguments namespace.
    """
    from mansio.transport_http import HttpTransport

    transport = HttpTransport(args.server, agent_id=args.agent, token=args.api_token)

    if args.follow:
        # SSE mode: subscribe and print each message as JSON line
        stop = threading.Event()

        def on_msg(msg):
            print(_msg_to_json(msg), flush=True)

        transport.subscribe(args.channel, on_msg)

        def handle_signal(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        stop.wait()
    else:
        # One-shot poll
        for msg in transport.query(args.channel, limit=args.limit):
            print(_msg_to_json(msg))

    transport.close()


def _cmd_client_channels(args: argparse.Namespace) -> None:
    """Handle ``client channels``.

    Args:
        args: Parsed arguments namespace.
    """
    from mansio.transport_http import HttpTransport

    transport = HttpTransport(args.server, agent_id=args.agent, token=args.api_token)
    for ch in transport.list_channels():
        print(ch)
    transport.close()


def _cmd_client_dm(args: argparse.Namespace) -> None:
    """Handle ``client dm``.

    Args:
        args: Parsed arguments namespace.
    """
    from mansio.transport_http import HttpTransport

    # Compute canonical DM channel (same logic as MansioClient._dm_channel)
    pair = sorted([args.agent, args.to_agent])
    channel = f"dm:{pair[0]}:{pair[1]}"

    transport = HttpTransport(args.server, agent_id=args.agent, token=args.api_token)
    msg_id = transport.publish(channel, args.agent, "chat", args.message)
    print(msg_id)
    transport.close()


# ── Entry Point ───────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """Entry point for the mansio CLI.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).
    """
    args = parse_args(argv)

    if args.command == "serve":
        _cmd_serve(args)
    elif args.command == "client":
        if args.action is None:
            print("usage: mansio client {send,poll,channels,dm} ...", file=sys.stderr)
            sys.exit(1)
        handlers = {
            "send": _cmd_client_send,
            "poll": _cmd_client_poll,
            "channels": _cmd_client_channels,
            "dm": _cmd_client_dm,
        }
        try:
            handlers[args.action](args)
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            print(f"error: cannot connect to {args.server}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        _build_parser().print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
