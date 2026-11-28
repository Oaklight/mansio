"""The ``mansio-agent doctor`` command."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from mansio_client.transport import HttpTransport, MansioAPIError

from . import config, runtimes


@dataclass
class Check:
    """One diagnostic line."""

    name: str
    ok: bool
    detail: str
    fix: str | None = None


def _binary_check(name: str) -> Check:
    path = shutil.which(name)
    if path is None:
        return Check(
            name,
            False,
            f"{name} not found on PATH",
            f"install mansio-client so {name} is on PATH",
        )
    return Check(name, True, path)


def _network_checks(url: str, user_id: str | None, token: str | None) -> list[Check]:
    checks: list[Check] = []
    transport: HttpTransport | None = None
    try:
        transport = HttpTransport(url, user_id=user_id or "", token=token)
        try:
            require_auth = transport.require_auth
        except MansioAPIError as exc:
            checks.append(Check("reachable", True, url))
            checks.append(
                Check(
                    "auth",
                    False,
                    f"HTTP {exc.status_code}: {exc}",
                    "check the server's /v1/auth/check endpoint",
                )
            )
            return checks
        except Exception as exc:
            checks.append(
                Check(
                    "reachable",
                    False,
                    f"{type(exc).__name__}: {exc}",
                    "check MANSIO_URL and that the server is running",
                )
            )
            return checks

        checks.append(Check("reachable", True, url))
        checks.append(Check("auth", True, "required" if require_auth else "disabled"))

        if require_auth and not token:
            checks.append(
                Check(
                    "token",
                    False,
                    "server requires authentication but MANSIO_TOKEN is not set",
                    "export MANSIO_TOKEN=...",
                )
            )
        else:
            try:
                channels = transport.channels()
            except MansioAPIError as exc:
                checks.append(
                    Check(
                        "channels",
                        False,
                        f"HTTP {exc.status_code}: {exc}",
                        "check the token and channel permissions",
                    )
                )
            except Exception as exc:
                checks.append(
                    Check(
                        "channels",
                        False,
                        f"{type(exc).__name__}: {exc}",
                        "check connectivity",
                    )
                )
            else:
                checks.append(Check("channels", True, f"{len(channels)} visible"))

        if not user_id:
            checks.append(
                Check(
                    "heartbeat",
                    False,
                    "MANSIO_USER_ID is not set",
                    "export MANSIO_USER_ID=...",
                )
            )
        else:
            try:
                transport.heartbeat(user_id)
            except NotImplementedError:
                checks.append(
                    Check(
                        "heartbeat",
                        False,
                        "backend does not support presence",
                        "use a presence-capable backend",
                    )
                )
            except Exception as exc:
                checks.append(
                    Check(
                        "heartbeat",
                        False,
                        f"{type(exc).__name__}: {exc}",
                        "check the token",
                    )
                )
            else:
                checks.append(Check("heartbeat", True, user_id))

        try:
            agents = transport.users(1)
        except NotImplementedError:
            checks.append(
                Check(
                    "agent roster (mansio_agents)",
                    False,
                    "backend does not support presence",
                    "use a presence-capable backend",
                )
            )
        except Exception as exc:
            checks.append(
                Check(
                    "agent roster (mansio_agents)",
                    False,
                    f"{type(exc).__name__}: {exc}",
                    "check the token",
                )
            )
        else:
            checks.append(Check("agent roster (mansio_agents)", True, f"{len(agents)} agents"))
    finally:
        if transport is not None:
            transport.close()
    return checks


def _codex_notes(cwd: Path | None) -> list[str]:
    spec = runtimes.RUNTIMES["codex"]
    for scope in ("project", "user"):
        if runtimes.has_installed_hook(spec, scope, cwd=cwd):
            return [
                "Codex: review the installed hook with /hooks before it runs "
                "(Codex does not run unreviewed command hooks)."
            ]
    return []


def run(
    *,
    url: str | None,
    user_id: str | None,
    token: str | None,
    out=print,
    cwd: Path | None = None,
) -> int:
    """Run all checks and print one line per check."""
    checks: list[Check] = [
        _binary_check("mansio-mcp"),
        _binary_check("mansio-agent"),
    ]
    notes = _codex_notes(cwd)

    agent = checks[1]
    running = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if agent.ok and running is not None and running.exists():
        resolved = Path(agent.detail).resolve()
        if resolved != running:
            notes.append(f"this run is {running}; hooks call {resolved}")

    checks.append(
        Check(
            "url",
            bool(url),
            url or "MANSIO_URL is not set",
            None if url else "export MANSIO_URL=...",
        )
    )
    checks.append(
        Check(
            "user-id",
            bool(user_id),
            user_id or "MANSIO_USER_ID is not set",
            None if user_id else "export MANSIO_USER_ID=...",
        )
    )

    if url:
        checks.extend(_network_checks(url, user_id, token))

    secrets = (token,) if token else ()
    for check in checks:
        status = "ok" if check.ok else "FAIL"
        detail = config.redact(check.detail, secrets)
        line = f"[{status}] {check.name}: {detail}"
        if not check.ok and check.fix:
            line += f" — fix: {check.fix}"
        out(line)
    for note in notes:
        out(f"[note] {config.redact(note, secrets)}")

    return 0 if all(check.ok for check in checks) else 1
