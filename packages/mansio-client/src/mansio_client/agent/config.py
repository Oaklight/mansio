"""Config merging, atomic writes, and identity generation for ``mansio-agent``."""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MCP_NAME = "mansio"
HOOK_TIMEOUT_SECONDS = 15
HOOK_MATCHER = "startup|resume"
MANAGED_COMMAND_PREFIX = "mansio-agent poll"

USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
_TOKEN_RE = re.compile(r'(MANSIO_TOKEN"?\s*[=:]\s*)"[^"]*"')


class ConfigError(ValueError):
    """The target config has an unexpected shape."""


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one config write."""

    path: Path
    changed: bool
    diff: str
    backup: Path | None = None


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace token values in *text*."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return _TOKEN_RE.sub(r'\1"<redacted>"', text)


def diff_text(path: Path, old: str, new: str, secrets: Iterable[str] = ()) -> str:
    """Return a unified diff of *old* to *new* with tokens redacted."""
    return "".join(
        difflib.unified_diff(
            redact(old, secrets).splitlines(keepends=True),
            redact(new, secrets).splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


def _target_mode(path: Path, has_secret: bool) -> int:
    if has_secret:
        return 0o600
    if path.exists():
        return stat.S_IMODE(path.stat().st_mode)
    return 0o644


def write_if_changed(
    path: Path,
    new: str,
    *,
    dry_run: bool = False,
    secrets: Iterable[str] = (),
) -> WriteResult:
    """Write *new* atomically when it differs from the current file.

    A ``.bak`` copy is made only when the file already existed and changed.
    In dry-run mode nothing is written and the diff is returned.
    """
    secrets = tuple(secret for secret in secrets if secret)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == new:
        return WriteResult(path=path, changed=False, diff="")

    diff = diff_text(path, old or "", new, secrets)
    if dry_run:
        return WriteResult(path=path, changed=True, diff=diff)

    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if old is not None:
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)

    mode = _target_mode(path, bool(secrets))
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise

    return WriteResult(path=path, changed=True, diff=diff, backup=backup)


def load_json(path: Path) -> dict:
    """Load a JSON object, treating a missing or empty file as ``{}``."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def dump_json(data: dict) -> str:
    """Render *data* as the config file text."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def hook_command(poll_format: str) -> str:
    """Return the managed hook command for *poll_format*."""
    return f"{MANAGED_COMMAND_PREFIX} --format {poll_format}"


def is_mansio_poll_hook(hook: object) -> bool:
    """Whether *hook* is a command hook installed by ``mansio-agent``."""
    if not isinstance(hook, dict):
        return False
    command = hook.get("command")
    return isinstance(command, str) and command.strip().startswith(MANAGED_COMMAND_PREFIX)


def merge_session_start_hook(root: dict, command: str) -> None:
    """Insert or replace the managed SessionStart hook in *root*.

    The managed hook is collapsed to exactly one entry across all
    ``SessionStart`` groups.
    """
    hooks = root.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ConfigError("'hooks' must be an object")
    groups = hooks.setdefault("SessionStart", [])
    if not isinstance(groups, list):
        raise ConfigError("'hooks.SessionStart' must be an array")

    entry = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}
    placed = False
    empty_groups: list[int] = []

    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        group_hooks = group.get("hooks")
        if not isinstance(group_hooks, list):
            continue
        kept: list[object] = []
        for hook in group_hooks:
            if isinstance(hook, dict) and is_mansio_poll_hook(hook):
                if placed:
                    continue  # collapse duplicates
                merged = dict(hook)
                merged.update(entry)
                kept.append(merged)
                placed = True
            else:
                kept.append(hook)
        if kept:
            group["hooks"] = kept
        else:
            empty_groups.append(group_index)

    for group_index in reversed(empty_groups):
        del groups[group_index]

    if placed:
        return

    for group in groups:
        if isinstance(group, dict) and group.get("matcher") == HOOK_MATCHER:
            group_hooks = group.setdefault("hooks", [])
            if not isinstance(group_hooks, list):
                raise ConfigError("'hooks.SessionStart[].hooks' must be an array")
            group_hooks.append(entry)
            return

    groups.append({"matcher": HOOK_MATCHER, "hooks": [entry]})


def remove_session_start_hooks(root: dict) -> None:
    """Remove only the hooks whose command is ``mansio-agent poll``."""
    hooks = root.get("hooks")
    if not isinstance(hooks, dict):
        return
    groups = hooks.get("SessionStart")
    if not isinstance(groups, list):
        return

    kept_groups: list[object] = []
    for group in groups:
        if not isinstance(group, dict):
            kept_groups.append(group)
            continue
        group_hooks = group.get("hooks")
        if not isinstance(group_hooks, list):
            kept_groups.append(group)
            continue
        filtered = [hook for hook in group_hooks if not is_mansio_poll_hook(hook)]
        if filtered:
            group["hooks"] = filtered
            kept_groups.append(group)

    if kept_groups:
        hooks["SessionStart"] = kept_groups
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        root.pop("hooks", None)


def merge_claude_settings(root: dict, *, url: str, user_id: str, token: str | None) -> None:
    """Insert or update the MANSIO MCP entry and SessionStart hook."""
    servers = root.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ConfigError("'mcpServers' must be an object")
    entry = servers.get(MCP_NAME)
    if not isinstance(entry, dict):
        entry = {}
    env = entry.get("env")
    if not isinstance(env, dict):
        env = {}

    entry["command"] = "mansio-mcp"
    entry["args"] = []
    env["MANSIO_URL"] = url
    env["MANSIO_USER_ID"] = user_id
    if token is None:
        env.pop("MANSIO_TOKEN", None)
    else:
        env["MANSIO_TOKEN"] = token
    entry["env"] = env
    servers[MCP_NAME] = entry

    merge_session_start_hook(root, hook_command("claude"))


def remove_claude_settings(root: dict) -> None:
    """Remove the MANSIO MCP entry and managed hooks."""
    servers = root.get("mcpServers")
    if isinstance(servers, dict):
        servers.pop(MCP_NAME, None)
        if not servers:
            root.pop("mcpServers", None)
    remove_session_start_hooks(root)


def project_slug(directory: Path) -> str:
    """Return a user-id-safe slug for *directory*."""
    base = directory.resolve().name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    if not slug:
        slug = "project"
    return slug[:40].strip("-") or "project"


def is_valid_user_id(user_id: str) -> bool:
    """Whether *user_id* matches the server's identity pattern."""
    return bool(USER_ID_RE.fullmatch(user_id))


def generate_user_id(runtime: str, slug: str, used: set[str]) -> str:
    """Return the first unused ``<runtime>-<slug>-<n>`` identity."""
    for number in range(1, 1000):
        suffix = f"-{number}"
        max_slug = 64 - len(runtime) - len(suffix) - 1
        trimmed = slug[:max_slug].strip("-") or "project"
        candidate = f"{runtime}-{trimmed}{suffix}"
        if is_valid_user_id(candidate) and candidate not in used:
            return candidate
    raise ConfigError("could not allocate a unique user id")
