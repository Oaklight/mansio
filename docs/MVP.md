# mansio MVP — Milestone Record

**Date:** 2026-04-20
**Status:** Historical record of the MVP milestone

> This file records what the MVP milestone delivered on the date above. It is
> not a reference for how mansio works now — the architecture and the client API
> have both changed since. For current documentation see the
> [README](../README_en.md) and [DESIGN_EN.md](DESIGN_EN.md).

---

## What the MVP Delivered

A minimal, zero-dependency message bus for multi-agent context synchronization,
with all core layers (Backend → Bus → Client SDK) implemented and tested.

| Layer | Component |
|-------|-----------|
| Backend | `SQLiteBackend` — WAL-mode, cross-process, single-file persistence |
| Backend | `MemoryBackend` — pure in-memory, for testing |
| Bus | `Bus` — orchestrator combining Backend, serializer and in-process pub/sub |
| Bus | `SQLiteBus` — convenience shorthand for `Bus(backend=SQLiteBackend(...))` |
| Serializer | `JSONSerializer` — human-readable metadata encoding |
| Client SDK | `MansioClient` — identity, cursor, channel naming, semantic API, constructed from a Bus object or a storage path |
| Admin | Admin panel — HTTP dashboard with route-based handlers |

Deferred at the time: the Hub-Server architecture (MansioServer + remote
transport), the IRC frontend, additional backends, typed channel enforcement at
the Bus layer, a moderator mechanism, message TTL / retention signals, priority
and delayed messages, an async API, the MCP/REST/CLI delivery layer, federation
([#4](https://github.com/Oaklight/mansio/issues/4)), and secret rotation /
agent revocation.

## Design Decisions Taken at This Milestone

1. **SQLite backend** — Zero external deps. WAL mode enables concurrent cross-process access.
2. **Message ID as cursor** — `poll(after=id)` uses UUID v7 (time-ordered), avoids clock skew.
3. **Sync API only** — Async doubles the surface for no MVP benefit.
4. **Poll + Subscribe** — Poll works cross-process; subscribe works in-process. Both simple.
5. **Channel naming at SDK layer** — Bus stays generic; the client enforces conventions.
6. **Everything is messages** — Registry, cursors and presence all stored as messages in `_system:` channels.
7. **Transport abstraction** — Decouples the client from in-process vs. network Bus access.

Decisions 5 and 6 no longer hold: channel naming is enforced at the Frontend
layer, and agent registration lives in a server-side token store. See §10 of
[DESIGN_EN.md](DESIGN_EN.md) for the current decision log.

## Test Coverage at This Milestone

185 tests across three files: `test_bus.py` (44), `test_client.py` (87),
`test_admin.py` (54). All functions passed complexipy at max complexity 15.
