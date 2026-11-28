"""R7 ratchet: adapters and admin must not touch the Bus directly.

The rule (docs_arch section 09, R7) is that a wire adapter reaches the Bus
only through Operations. Until the operations exist, this test freezes the
current set of direct accesses. Each stage that migrates a call site must
remove its entry in the same change; the test then fails until it does.

Planned replacements, per the target architecture:

- ``adapters/http.py`` ``_write_presence``: ``bus.heartbeat`` becomes
  ``operations.presence.touch``.
- ``adapters/irc.py`` inbound: ``bus.publish`` becomes
  ``operations.messages.publish``; outbound ``bus.subscribe`` /
  ``bus.unsubscribe`` become ``operations.subscribe``.
- ``adapters/routes/sse.py``: ``bus.subscribe`` / ``bus.unsubscribe`` /
  ``bus.query`` become ``operations.subscribe``.
- ``adapters/routes/*`` and ``admin/view.py``: the remaining calls become
  message, channel, queue, presence, and admin operations (M6b/M6c).

The scanner counts ``<bus>.<attr>`` loads, where ``<bus>`` is the name
``bus``, a local alias of it (``x = self._bus`` also counts), or an
attribute ``.bus`` / ``._bus`` (``self._bus.attr``). Only the lifecycle
call ``close`` is allowed.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[1] / "src" / "mansio_web"
SCAN_DIRS = ("adapters", "admin")
BUS_BASE_ATTRS = {"bus", "_bus"}
ALLOWED_LIFECYCLE = {"close"}

# file -> attribute -> number of accesses. Shrinks stage by stage.
KNOWN_VIOLATIONS: dict[str, dict[str, int]] = {
    "adapters/http.py": {"heartbeat": 1},
    "adapters/irc.py": {"publish": 1, "subscribe": 1, "unsubscribe": 2},
    "adapters/routes/channel_admin.py": {
        "add_acl_entry": 1,
        "create_channel": 1,
        "get_acl": 1,
        "get_channel_meta": 3,
        "remove_acl_entry": 1,
        "set_acl": 1,
    },
    "adapters/routes/channels.py": {"channels": 2, "channels_detail": 2},
    "adapters/routes/messages.py": {
        "channels": 1,
        "channels_detail": 1,
        "compact": 1,
        "delete_channel": 2,
        "delete_message": 1,
        "get_message": 1,
    },
    "adapters/routes/presence.py": {"heartbeat": 1, "user_status": 1, "users": 1},
    "adapters/routes/publish.py": {
        "message_count": 1,
        "publish": 1,
        "query": 1,
        "user_status": 1,
    },
    "adapters/routes/queue.py": {
        "get_message": 2,
        "queue_ack": 1,
        "queue_claim": 1,
        "queue_status": 1,
    },
    "adapters/routes/sse.py": {"query": 1, "subscribe": 1, "unsubscribe": 1},
    "admin/routes/messages.py": {"publish": 1},
    "admin/view.py": {
        "channels": 1,
        "info": 1,
        "metrics": 1,
        "query": 2,
        "search": 1,
        "stats": 1,
        "subscription_counts": 3,
        "user_status": 1,
        "users": 1,
    },
}


def _is_bus_ref(node: ast.expr, aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr in BUS_BASE_ATTRS
    return False


def _direct_bus_accesses(path: Path) -> collections.Counter[str]:
    """Count ``<bus>.<attr>`` loads in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Aliases assigned from a bus reference, in source order.
    aliases = {"bus"}
    assignments = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Assign | ast.AnnAssign)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for node in assignments:
        if node.value is None or not _is_bus_ref(node.value, aliases):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)

    found: collections.Counter[str] = collections.Counter()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr not in ALLOWED_LIFECYCLE
            and _is_bus_ref(node.value, aliases)
        ):
            found[node.attr] += 1
    return found


def _scan() -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for directory in SCAN_DIRS:
        for path in sorted((WEB_ROOT / directory).rglob("*.py")):
            counts = _direct_bus_accesses(path)
            if counts:
                found[str(path.relative_to(WEB_ROOT))] = dict(sorted(counts.items()))
    return found


def test_scanner_counts_self_bus_and_aliases(tmp_path) -> None:
    path = tmp_path / "sample.py"
    path.write_text(
        "class A:\n"
        "    def f(self):\n"
        "        x = self._bus\n"
        "        x.query('c')\n"
        "        self._bus.publish('c')\n"
        "        self._bus.close()\n",
        encoding="utf-8",
    )
    assert _direct_bus_accesses(path) == collections.Counter({"publish": 1, "query": 1})


def test_no_new_direct_bus_access() -> None:
    """The set of direct Bus accesses equals the frozen set, and shrinks."""
    found = _scan()

    new = {
        path: {
            attr: n for attr, n in counts.items() if KNOWN_VIOLATIONS.get(path, {}).get(attr, 0) < n
        }
        for path, counts in found.items()
    }
    new = {path: counts for path, counts in new.items() if counts}
    fixed = {
        path: {
            attr: n - found.get(path, {}).get(attr, 0)
            for attr, n in counts.items()
            if n > found.get(path, {}).get(attr, 0)
        }
        for path, counts in KNOWN_VIOLATIONS.items()
    }
    fixed = {path: counts for path, counts in fixed.items() if counts}

    assert found == KNOWN_VIOLATIONS, (
        "R7 ratchet changed.\n"
        f"New direct Bus access (move it into an operation): {new}\n"
        f"Migrated access still listed (remove it from KNOWN_VIOLATIONS): {fixed}\n"
        f"Current scan: {found}"
    )
