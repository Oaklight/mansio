"""CLI for mansio-client — interact with a remote mansio server."""

from __future__ import annotations

import argparse
import json
import sys

from mansio_client import MansioClient, __version__
from mansio_client.env import env_or as _env_or


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
        default=_env_or("MANSIO_USER_ID"),
        help="Agent user ID (or MANSIO_USER_ID env)",
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
    dm.add_argument("--to", required=True, dest="to_user")
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

    ch_create = sub.add_parser("channel-create", help="Create a channel")
    ch_create.add_argument("name")
    ch_create.add_argument(
        "--visibility", default="public", choices=["public", "private"]
    )

    ch_delete = sub.add_parser("channel-delete", help="Delete a channel")
    ch_delete.add_argument("name")

    msg_delete = sub.add_parser("message-delete", help="Delete a message")
    msg_delete.add_argument("message_id")

    acl = sub.add_parser("acl", help="Manage channel ACL")
    acl_sub = acl.add_subparsers(dest="acl_action")
    acl_get = acl_sub.add_parser("get", help="Get ACL entries")
    acl_get.add_argument("channel")
    acl_set = acl_sub.add_parser("set", help="Replace ACL entries (JSON)")
    acl_set.add_argument("channel")
    acl_set.add_argument("entries_json", help="JSON array of {user_id, permission}")
    acl_add_p = acl_sub.add_parser("add", help="Add an ACL entry")
    acl_add_p.add_argument("channel")
    acl_add_p.add_argument("user_id")
    acl_add_p.add_argument(
        "--permission", default="read", choices=["read", "write", "admin"]
    )
    acl_rm = acl_sub.add_parser("remove", help="Remove an ACL entry")
    acl_rm.add_argument("channel")
    acl_rm.add_argument("user_id")

    reg = sub.add_parser("registry-lookup", help="Check if a user is registered")
    reg.add_argument("user_id")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if not args.server or not args.agent:
        print(
            "error: --server and --agent required (or set MANSIO_URL and MANSIO_USER_ID)",
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
        "channel-create": _cmd_channel_create,
        "channel-delete": _cmd_channel_delete,
        "message-delete": _cmd_message_delete,
        "acl": _cmd_acl,
        "registry-lookup": _cmd_registry_lookup,
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
    print(client.dm_send(args.to_user, args.message))


def _cmd_check(_args: argparse.Namespace, client: MansioClient) -> None:
    unread = client.check_unread()
    total = sum(unread.values())
    print(json.dumps({"unread": unread, "total_unread": total}, ensure_ascii=False))
    if total == 0:
        sys.exit(1)


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


def _cmd_channel_create(args: argparse.Namespace, client: MansioClient) -> None:
    result = client.channel_create(args.name, visibility=args.visibility)
    print(json.dumps(result, ensure_ascii=False))


def _cmd_channel_delete(args: argparse.Namespace, client: MansioClient) -> None:
    count = client.channel_delete(args.name)
    print(f"Deleted channel {args.name!r} ({count} messages removed)")


def _cmd_message_delete(args: argparse.Namespace, client: MansioClient) -> None:
    client.message_delete(args.message_id)
    print(f"Deleted message {args.message_id}")


def _cmd_acl(args: argparse.Namespace, client: MansioClient) -> None:
    if args.acl_action == "get":
        for entry in client.acl_get(args.channel):
            print(
                json.dumps(
                    {"user_id": entry.user_id, "permission": entry.permission},
                    ensure_ascii=False,
                )
            )
    elif args.acl_action == "set":
        from mansio_client.types import ACLEntry

        raw = json.loads(args.entries_json)
        entries = [
            ACLEntry(
                channel=args.channel,
                user_id=e["user_id"],
                permission=e.get("permission", "read"),
            )
            for e in raw
        ]
        count = client.acl_set(args.channel, entries)
        print(f"Set {count} ACL entries")
    elif args.acl_action == "add":
        entry = client.acl_add(args.channel, args.user_id, args.permission)
        print(
            json.dumps(
                {"user_id": entry.user_id, "permission": entry.permission},
                ensure_ascii=False,
            )
        )
    elif args.acl_action == "remove":
        client.acl_remove(args.channel, args.user_id)
        print(f"Removed ACL entry for {args.user_id!r}")
    else:
        print("usage: mansio-client acl {get,set,add,remove}", file=sys.stderr)
        sys.exit(1)


def _cmd_registry_lookup(args: argparse.Namespace, client: MansioClient) -> None:
    found = client.registry_lookup(args.user_id)
    print(json.dumps({"user_id": args.user_id, "found": found}, ensure_ascii=False))


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
