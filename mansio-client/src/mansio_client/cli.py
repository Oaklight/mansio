"""CLI for mansio-client — interact with a remote mansio server."""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings

from mansio_client import MansioClient, __version__

_LEGACY_ENV_MAP = {
    "MANSIO_URL": "PIAZZA_URL",
    "MANSIO_AGENT_ID": "PIAZZA_AGENT_ID",
    "MANSIO_TOKEN": "PIAZZA_TOKEN",
}


def _env_or(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is not None:
        return val
    legacy = _LEGACY_ENV_MAP.get(name)
    if legacy and (legacy_val := os.environ.get(legacy)):
        warnings.warn(
            f"{legacy} is deprecated, use {name} instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return legacy_val
    return default


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mansio-client", description="Mansio agent client"
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-s",
        "--server",
        default=_env_or("MANSIO_URL"),
        help="Server URL (or MANSIO_URL env)",
    )
    parser.add_argument(
        "-a",
        "--agent",
        default=_env_or("MANSIO_AGENT_ID"),
        help="Agent ID (or MANSIO_AGENT_ID env)",
    )
    parser.add_argument(
        "-t",
        "--token",
        default=_env_or("MANSIO_TOKEN"),
        help="API token (or MANSIO_TOKEN env)",
    )

    sub = parser.add_subparsers(dest="command")

    send = sub.add_parser("send", help="Send a message")
    send.add_argument("-c", "--channel", required=True)
    send.add_argument("--type", default="chat", dest="msg_type")
    send.add_argument("message")

    poll = sub.add_parser("poll", help="Poll new messages")
    poll.add_argument("-c", "--channel", required=True)

    read = sub.add_parser("read", help="Read messages")
    read.add_argument("-c", "--channel", required=True)
    read.add_argument("-n", "--limit", type=int, default=10)

    sub.add_parser("channels", help="List channels")
    sub.add_parser("check", help="Quick check for new messages")

    dm = sub.add_parser("dm", help="Send a DM")
    dm.add_argument("--to", required=True, dest="to_agent")
    dm.add_argument("message")

    note = sub.add_parser("note", help="Write a note")
    note.add_argument("content")
    note.add_argument("--tags", nargs="*")

    mem = sub.add_parser("memory", help="Store or recall memory")
    mem_sub = mem.add_subparsers(dest="mem_action")
    store = mem_sub.add_parser("store")
    store.add_argument("content")
    recall = mem_sub.add_parser("recall")
    recall.add_argument("query")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if not args.server or not args.agent:
        print(
            "error: --server and --agent required (or set MANSIO_URL and MANSIO_AGENT_ID)",
            file=sys.stderr,
        )
        sys.exit(1)

    client = MansioClient(args.server, args.agent, token=args.token)
    try:
        _dispatch(args, client)
    finally:
        client.close()


def _dispatch(args: argparse.Namespace, client: MansioClient) -> None:
    handlers = {
        "send": _cmd_send,
        "poll": _cmd_poll,
        "read": _cmd_read,
        "channels": _cmd_channels,
        "dm": _cmd_dm,
        "check": _cmd_check,
        "note": _cmd_note,
        "memory": _cmd_memory,
    }
    handlers[args.command](args, client)


def _cmd_send(args: argparse.Namespace, client: MansioClient) -> None:
    print(client.channel_send(args.channel, args.message, msg_type=args.msg_type))


def _cmd_poll(args: argparse.Namespace, client: MansioClient) -> None:
    for msg in client.channel_poll(args.channel):
        print(json.dumps(_msg_dict(msg), ensure_ascii=False))


def _cmd_read(args: argparse.Namespace, client: MansioClient) -> None:
    for msg in client.channel_read(args.channel, limit=args.limit):
        print(json.dumps(_msg_dict(msg), ensure_ascii=False))


def _cmd_channels(_args: argparse.Namespace, client: MansioClient) -> None:
    for ch in client.channel_list():
        print(ch)


def _cmd_dm(args: argparse.Namespace, client: MansioClient) -> None:
    print(client.dm_send(args.to_agent, args.message))


def _cmd_check(_args: argparse.Namespace, client: MansioClient) -> None:
    channels = client.channel_list()
    user_channels = [ch for ch in channels if not ch.startswith("_system:")]
    print(
        json.dumps(
            {"channels": len(user_channels), "names": user_channels}, ensure_ascii=False
        )
    )


def _cmd_note(args: argparse.Namespace, client: MansioClient) -> None:
    print(client.note_write(args.content, tags=args.tags))


def _cmd_memory(args: argparse.Namespace, client: MansioClient) -> None:
    if args.mem_action == "store":
        print(client.memory_store(args.content))
    elif args.mem_action == "recall":
        for msg in client.memory_recall(args.query):
            print(json.dumps(_msg_dict(msg), ensure_ascii=False))
    else:
        print("usage: mansio-client memory {store,recall}", file=sys.stderr)
        sys.exit(1)


def _msg_dict(msg) -> dict:
    return {
        "id": msg.id,
        "channel": msg.channel,
        "sender": msg.sender,
        "msg_type": msg.msg_type,
        "payload": msg.payload,
        "timestamp": msg.timestamp,
        "metadata": msg.metadata,
    }


if __name__ == "__main__":
    main()
