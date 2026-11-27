"""Command-line interface for the mansio server.

Subcommands:
    mansio serve   — start Bus + AdminServer (+ optional HttpAdapter)
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from typing import Any, NoReturn

from mansio_core import SQLiteBus, __version__

_LEGACY_DB = "piazza.db"
_DEFAULT_DB = "mansio.db"

# Default token store filename for backends with an operator-named local
# directory to put it in (currently just --maildir). Not a bare cwd-relative
# default: a server started from a different working directory must not
# silently open a different, empty token store.
_MAILDIR_TOKEN_DB_NAME = "tokens.db"

# Bind addresses that keep a listener on this machine only.
_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Opt-in flag for binding an API that enforces no authentication to a
# network-reachable address. Named here so the startup error can point at it.
_UNAUTH_OVERRIDE_FLAG = "--insecure-allow-unauthenticated-network-api"


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
    backend_group = serve.add_mutually_exclusive_group()
    backend_group.add_argument(
        "-d",
        "--db",
        default=None,
        help="SQLite database path (default: mansio.db)",
    )
    backend_group.add_argument(
        "--maildir",
        metavar="PATH",
        default=None,
        help="Use Maildir backend at PATH instead of SQLite",
    )
    backend_group.add_argument(
        "--nats",
        metavar="URL",
        default=None,
        help="Use NATS backend at URL (e.g. nats://localhost:4222)",
    )
    serve.add_argument(
        "--token-db",
        metavar="PATH",
        default=None,
        help=(
            "SQLite file for the token store, independent of the message "
            "backend. Defaults to the message database with --db, or "
            f"{_MAILDIR_TOKEN_DB_NAME!r} inside the --maildir directory. "
            "Required with --nats, which names no local directory to "
            "default into"
        ),
    )
    serve.add_argument(
        "--http",
        metavar="[HOST:]PORT",
        default=None,
        help="Enable HttpAdapter on [HOST:]PORT (e.g. 8742 or 0.0.0.0:8742)",
    )
    serve.add_argument(
        "--admin-port",
        type=int,
        default=8741,
        help="Admin panel port (default: 8741)",
    )
    serve.add_argument(
        "--host",
        default=None,
        help="Admin panel bind address (default: 127.0.0.1)",
    )
    serve.add_argument(
        "--admin-password",
        default=None,
        help="Admin panel password (auto-generated when --remote is used)",
    )
    serve.add_argument(
        "--remote",
        action="store_true",
        help=(
            "Shorthand: bind admin to 0.0.0.0 and auto-generate admin password. "
            "Equivalent to --host 0.0.0.0 with an auto-generated --admin-password"
        ),
    )
    serve.add_argument(
        "--token",
        default=None,
        help="Deprecated alias for --admin-password",
    )
    serve.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable all auth — API token auth and admin auth (dev only)",
    )
    serve.add_argument(
        _UNAUTH_OVERRIDE_FLAG,
        action="store_true",
        help=(
            "Allow binding an API that enforces no authentication to a "
            "network-reachable address. Without this, such a combination is "
            "refused at startup"
        ),
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
        help="Enable IrcAdapter connecting to HOST:PORT (e.g. irc.example.com:6667)",
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


def _migrate_legacy_db(db_path: str, logger) -> str:
    """Auto-rename piazza.db to mansio.db when using the default path."""
    if db_path == _DEFAULT_DB and not os.path.exists(_DEFAULT_DB) and os.path.exists(_LEGACY_DB):
        import warnings

        warnings.warn(
            f"Legacy database '{_LEGACY_DB}' detected and auto-renamed to "
            f"'{_DEFAULT_DB}'. This auto-migration will be removed in mansio 1.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        os.rename(_LEGACY_DB, _DEFAULT_DB)
        logger.info("Migrated database", old=_LEGACY_DB, new=_DEFAULT_DB)
    return db_path


def _create_bus(args: argparse.Namespace, logger: Any) -> Any:
    """Instantiate the message bus from CLI arguments."""
    if args.maildir:
        from mansio_core.backends.maildir import MaildirBackend
        from mansio_core.bus import Bus

        backend = MaildirBackend(args.maildir)
        logger.info("Bus started", backend="maildir", path=args.maildir)
        return Bus(backend=backend)

    if args.nats:
        from mansio_core.backends.nats import NATSBackend
        from mansio_core.bus import Bus

        backend = NATSBackend(url=args.nats)
        logger.info("Bus started", backend="nats", url=args.nats)
        return Bus(backend=backend)

    from mansio_core.backends.sqlite import SchemaVersionError

    db_path = _migrate_legacy_db(args.db or _DEFAULT_DB, logger)
    force_init = os.environ.get("MANSIO_FORCE_INIT", "") == "1"
    try:
        bus = SQLiteBus(db_path, force_init=force_init)
    except SchemaVersionError as exc:
        logger.error(
            str(exc),
            hint="Set MANSIO_FORCE_INIT=1 to force initialization (data may be lost)",
        )
        sys.exit(1)
    logger.info("Bus started", backend="sqlite", db=db_path)
    return bus


def _is_off_host(host: str) -> bool:
    """Report whether binding to ``host`` makes a listener reachable off-machine.

    Anything that is not an explicit loopback name or address counts as
    off-host, including wildcard binds such as ``0.0.0.0`` and ``::``.

    Args:
        host: Bind address as written on the command line.

    Returns:
        True when the address is reachable from outside this machine.
    """
    return host not in _LOCALHOST_HOSTS


def _admin_bind_host(args: argparse.Namespace) -> str:
    """Resolve the admin panel bind host: explicit --host > --remote > loopback.

    Args:
        args: Parsed arguments namespace.

    Returns:
        The address the admin panel will bind to.
    """
    return args.host if args.host else ("0.0.0.0" if args.remote else "127.0.0.1")


def _api_bind_host(args: argparse.Namespace) -> str | None:
    """Resolve the HTTP API bind host from --http.

    Args:
        args: Parsed arguments namespace.

    Returns:
        The address the API will bind to, or None when --http is not set.
    """
    if not args.http:
        return None
    return _parse_host_port(args.http)[0]


def _check_network_exposure(args: argparse.Namespace, logger: Any) -> None:
    """Refuse to start when a network-reachable surface enforces no auth.

    Exposure is decided from the addresses actually bound — the API host from
    --http as well as the admin host — because those determine who can reach
    the server. Every backend can have a token store now (see
    _create_token_store), so the only way left to run an unauthenticated API
    is the explicit --no-auth opt-out, and that is what this guard checks.

    Args:
        args: Parsed arguments namespace.
        logger: Structured logger.

    Returns:
        None. Calls sys.exit(1) on an unsafe combination.
    """
    admin_host = _admin_bind_host(args)
    api_host = _api_bind_host(args)
    admin_exposed = _is_off_host(admin_host)
    api_exposed = api_host is not None and _is_off_host(api_host)
    # --no-auth strips auth from both surfaces.
    no_auth_exposed = args.no_auth and (admin_exposed or api_exposed)
    if not no_auth_exposed:
        return

    if args.insecure_allow_unauthenticated_network_api:
        logger.warning(
            "Serving an unauthenticated API on a network-reachable address",
            admin_host=admin_host,
            api_host=api_host,
            override=_UNAUTH_OVERRIDE_FLAG,
        )
        return

    logger.error(
        "--no-auth cannot be used with a network-reachable bind address "
        "(would expose an unauthenticated API to the network)",
        admin_host=admin_host,
        api_host=api_host,
        hint=(
            "Bind to 127.0.0.1, or drop --no-auth, or pass "
            f"{_UNAUTH_OVERRIDE_FLAG} to accept the risk"
        ),
    )
    sys.exit(1)


def _resolve_admin_auth(args: argparse.Namespace, logger: Any) -> tuple[str, str | None]:
    """Resolve admin host and password from CLI flags.

    Handles --host, --remote, --admin-password and --token (deprecated).

    Args:
        args: Parsed arguments namespace.
        logger: Structured logger.

    Returns:
        Tuple of (admin_host, admin_password). Calls sys.exit on
        invalid combinations.
    """
    # Resolve --token as deprecated alias for --admin-password
    admin_password = args.admin_password
    if args.token:
        if admin_password:
            logger.error("Cannot use both --admin-password and --token (--token is deprecated)")
            sys.exit(1)
        import warnings

        warnings.warn(
            "--token is deprecated, use --admin-password instead. "
            "Support for --token will be removed in mansio 1.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        admin_password = args.token

    admin_host = _admin_bind_host(args)

    # Auto-generate password when exposed without explicit auth
    if _is_off_host(admin_host) and not admin_password and not args.no_auth:
        import secrets

        admin_password = secrets.token_urlsafe(16)
        logger.warning(
            "Admin panel exposed without --admin-password, auto-generated one",
            admin_host=admin_host,
        )

    return admin_host, admin_password


def _resolve_token_db_path(args: argparse.Namespace) -> str | None:
    """Resolve the SQLite file backing the token store.

    Independent of the message backend: an explicit --token-db always wins.
    Beyond that, only defaults that belong to the deployment rather than to
    the shell's cwd are trustworthy — a cwd-relative default means starting
    the same server from a different directory silently opens a different,
    empty token store, which looks like every token (and the ability to
    authenticate at all) just vanished. Concretely:

    - --db (SQLite backend): the token store reuses the message database
      file, so a single-file deployment stays single-file.
    - --maildir: the operator already named a directory that belongs to
      this deployment, so the token store's file lives inside it.
    - --nats: names a remote connection string, not a local directory —
      there is no honest default, so this returns None and the caller
      requires an explicit --token-db.

    Args:
        args: Parsed arguments namespace.

    Returns:
        Filesystem path to the token store's SQLite file, or None when
        the backend has no directory to default into (--nats without
        --token-db).
    """
    if args.token_db:
        return args.token_db
    if args.maildir:
        return os.path.join(args.maildir, _MAILDIR_TOKEN_DB_NAME)
    if args.nats:
        return None
    return args.db or _DEFAULT_DB


def _create_token_store(args: argparse.Namespace, logger: Any) -> Any:
    """Create the token store used to authenticate API requests.

    The token store's file is resolved independently of the message
    backend (see _resolve_token_db_path), so every backend can have working
    auth. The only way to run without a token store is the explicit
    --no-auth opt-out; --nats additionally requires an explicit --token-db,
    since it has no directory to default one into.

    Args:
        args: Parsed arguments namespace.
        logger: Structured logger.

    Returns:
        TokenStore instance, or None when --no-auth is set.
    """
    if args.no_auth:
        return None

    token_db_path = _resolve_token_db_path(args)
    if token_db_path is None:
        logger.error(
            "--nats names no local directory to default a token store into",
            hint="Pass --token-db PATH to choose where tokens are stored, or --no-auth to run without auth",
        )
        sys.exit(1)

    from mansio_core.token_store import TokenStore

    # Independent of whatever directory-creation the message backend does —
    # a --maildir path or an explicit --token-db may name a directory that
    # doesn't exist yet.
    os.makedirs(os.path.dirname(token_db_path) or ".", exist_ok=True)
    token_store = TokenStore(token_db_path)
    token_count = len(token_store.list_tokens())
    if token_count > 0:
        logger.info("Token auth enabled", token_db=token_db_path, token_count=token_count)
    else:
        logger.info(
            "Token auth ready (no tokens yet — API will reject requests until a token is created)",
            token_db=token_db_path,
        )
    return token_store


def _start_http_adapter(args: argparse.Namespace, bus: Any, token_store: Any, logger: Any) -> Any:
    """Start HttpAdapter if --http is set.

    Args:
        args: Parsed arguments namespace.
        bus: Message bus instance.
        token_store: Token store or None.
        logger: Structured logger.

    Returns:
        HttpAdapter instance or None.
    """
    if not args.http:
        return None

    from mansio_web.adapters.http import HttpAdapter

    host, port = _parse_host_port(args.http)
    http_adapter = HttpAdapter(
        host=host,
        port=port,
        token_store=token_store,
        allow_unauthenticated=(token_store is None),
    )
    http_adapter.attach(bus)
    http_thread = threading.Thread(
        target=http_adapter.serve_forever,
        name="mansio-http-adapter",
        daemon=True,
    )
    http_thread.start()
    # Wait for server to bind (port changes from 0 to actual)
    for _ in range(50):
        _, actual_port = http_adapter.address
        if actual_port != 0:
            break
        time.sleep(0.01)
    actual_host, actual_port = http_adapter.address
    logger.info("HttpAdapter started", url=f"http://{actual_host}:{actual_port}")
    return http_adapter


def _start_irc_adapter(args: argparse.Namespace, bus: Any, logger: Any) -> Any:
    """Start IrcAdapter if --irc is set.

    Args:
        args: Parsed arguments namespace.
        bus: Message bus instance.
        logger: Structured logger.

    Returns:
        IrcAdapter instance or None.
    """
    if not args.irc:
        return None

    from mansio_web.adapters.irc import IrcAdapter

    if ":" not in args.irc:
        logger.error("--irc requires HOST:PORT format (e.g. irc.example.com:6667)")
        sys.exit(1)
    irc_host, irc_port = _parse_host_port(args.irc)
    irc_adapter = IrcAdapter(
        irc_host=irc_host,
        irc_port=irc_port,
        nickname=args.irc_nick,
        channels=args.irc_channels or [],
        use_ssl=args.irc_ssl,
    )
    irc_adapter.attach(bus)
    irc_thread = threading.Thread(
        target=irc_adapter.serve_forever,
        name="mansio-irc-adapter",
        daemon=True,
    )
    irc_thread.start()
    logger.info(
        "IrcAdapter started",
        host=irc_host,
        port=irc_port,
        nick=args.irc_nick,
        channels=args.irc_channels or [],
    )
    return irc_adapter


def _cmd_serve(args: argparse.Namespace) -> None:
    """Handle the ``serve`` subcommand.

    Args:
        args: Parsed arguments namespace.
    """
    import logging

    from mansio_web._vendor.structlog import configure, get_logger

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    configure(logger_factory=lambda *a: logging.getLogger(a[0] if a else "mansio"))
    logger = get_logger("mansio")

    _check_network_exposure(args, logger)
    admin_host, admin_password = _resolve_admin_auth(args, logger)
    bus = _create_bus(args, logger)
    token_store = _create_token_store(args, logger)

    http_adapter = _start_http_adapter(args, bus, token_store, logger)
    irc_adapter = _start_irc_adapter(args, bus, logger)

    # Start AdminServer as an Adapter
    from mansio_web.admin import AdminServer

    admin_kwargs: dict[str, Any] = {
        "host": admin_host,
        "port": args.admin_port,
        "serve_ui": not args.no_ui,
        "remote": args.remote,
        "token_store": token_store,
    }
    if admin_password:
        admin_kwargs["auth_password"] = admin_password

    admin = AdminServer(**admin_kwargs)
    admin.attach(bus)
    info = admin.start()
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
    admin.shutdown()
    if irc_adapter:
        irc_adapter.shutdown()
    if http_adapter:
        http_adapter.shutdown()
    bus.close()


# ── Entry Point ───────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """Entry point for the mansio CLI.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).
    """
    args = parse_args(argv)
    _cmd_serve(args)


def _fail(message: str) -> NoReturn:
    """Print one error line to stderr and exit non-zero."""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
