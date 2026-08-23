---
hide:
  - navigation
---
# Changelog

## Unreleased

## 0.2.3 (2026-07-09)

### Added

- **HTTPS support** — HttpTransport now works over HTTPS (previously HTTP-only)
- **`token` param** — `MansioClient("https://...", "agent-id", token="pzt-xxx")` for authenticated remote access
- **`--api-token` CLI flag** — all `mansio client` subcommands support Bearer token auth

### Changed

- **Vendored zerodep modules** — `httpclient` (v0.4.4) and `sse` (v0.3.2) replace hand-written HTTP/SSE code in HttpTransport
- SSE streaming uses `SSEClient` with auto-reconnect and W3C-compliant event parsing

### Fixed

- Admin login overlay now hides page content until authenticated (no flash of unauthenticated dashboard)

## 0.2.1 (2026-07-08)

### Added

- **HttpFrontend token auth** — Bearer token validation on all `/v1/*` endpoints, sender match enforcement on publish, read isolation on query/subscribe per agent_id
- **TokenStore** — SQLite-backed agent token management with SHA-256 hashing, `last_used_at` tracking, supertoken support (`agent_id=NULL` for wildcard access)
- **Admin token management UI** — Tokens tab with create/delete/rotate/copy-to-clipboard, plaintext shown once at creation
- **Session cookie auth** — admin panel migrated from Bearer header to `HttpOnly; SameSite=Strict` session cookies, freeing `Authorization` header for agent tokens
- **Login overlay** — replaces `prompt()` with proper login card UI
- **Login rate limiting** — per-IP exponential backoff (1s→30s) after 5 consecutive failures
- **`--no-auth` CLI flag** — disables all auth for local dev (cannot be used with `--remote`)

### Fixed

- Admin panel now serves HTML/JS/CSS without auth so login overlay can render
- `WWW-Authenticate: Bearer` header included on 401 responses (HTTP spec)

## 0.2.0 (2026-07-08)

### Added

- **Docker packaging**: `oaklight/mansio` image (Alpine, non-root, PUID/PGID mapping, multi-arch amd64+arm64), docker-compose reference with healthcheck, CI auto-build on release
- **CLI subcommands**: `mansio serve` (with `--http`, `--irc` frontends) and `mansio client` (send/poll/channels/dm)
- **HttpFrontend**: REST API + SSE push for remote agent access (`/v1/publish`, `/v1/query`, `/v1/subscribe`)
- **IrcFrontend**: bidirectional bridge between mansio channels and IRC (optional `mansio[irc]` extra)
- **MansioServer**: orchestrator for Bus + multiple Frontends
- **HttpTransport**: client-side HTTP transport — `MansioClient("http://...")` just works
- **Transport protocol**: `subscribe`/`unsubscribe` promoted to protocol contract
- **MansioClient SDK**: identity management, cursor tracking, DMs, notes, memory, thoughts
- **Admin panel**: web dashboard + REST API for monitoring
- **Security hardening**: configurable request body limits, query caps, CORS origin
- **Reliability**: atomic drop counter for SSE slow consumers, WAL retry logging

### Fixed

- SQLite WAL `journal_mode` lock contention on concurrent cold-start
- Signal handler safety — cleanup in main thread, not signal context
- Token redaction in CLI logs (`sk-abcd1...xyz9`)
- IRC reconnect duplicate subscription bug
- UTF-8 safe truncation for IRC message length limits

### Refactored

- Backend architecture: `Backend` protocol with `SQLiteBackend` / `MemoryBackend`
- Admin handlers split into `routes/` subpackage
- Pre-commit config aligned with toolregistry

## 0.0.1a999 (2026-04-14)

- Initial MVP with SQLite-backed message bus
- Modular architecture with Backend and Serializer protocols
- SQLiteBackend and MemoryBackend implementations
- JSONSerializer implementation
