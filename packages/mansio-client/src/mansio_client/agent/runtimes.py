"""Runtime-specific config locations and install/uninstall operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mansio_client.env import env_or

from . import config, toml_edit


@dataclass(frozen=True)
class RuntimeSpec:
    """Where one agent runtime keeps its config."""

    name: str
    config_relpath: Path
    hooks_relpath: Path | None
    config_format: str
    poll_format: str


RUNTIMES: dict[str, RuntimeSpec] = {
    "claude-code": RuntimeSpec(
        name="claude-code",
        config_relpath=Path(".claude") / "settings.json",
        hooks_relpath=None,
        config_format="json",
        poll_format="claude",
    ),
    "codex": RuntimeSpec(
        name="codex",
        config_relpath=Path(".codex") / "config.toml",
        hooks_relpath=Path(".codex") / "hooks.json",
        config_format="toml",
        poll_format="codex",
    ),
}


def get_runtime(name: str) -> RuntimeSpec:
    """Return the runtime spec for *name*."""
    try:
        return RUNTIMES[name]
    except KeyError:
        raise config.ConfigError(
            f"unsupported runtime {name!r}; choose one of: {', '.join(sorted(RUNTIMES))}"
        ) from None


def scope_root(scope: str, *, cwd: Path | None = None) -> Path:
    """Return the root directory for *scope*."""
    if scope == "project":
        return cwd or Path.cwd()
    if scope == "user":
        return Path.home()
    raise config.ConfigError(f"unsupported scope {scope!r}; choose 'project' or 'user'")


def config_path(spec: RuntimeSpec, scope: str, *, cwd: Path | None = None) -> Path:
    """Return the runtime's config path for *scope*."""
    return scope_root(scope, cwd=cwd) / spec.config_relpath


def hooks_path(spec: RuntimeSpec, scope: str, *, cwd: Path | None = None) -> Path | None:
    """Return the runtime's separate hooks path, if it has one."""
    if spec.hooks_relpath is None:
        return None
    return scope_root(scope, cwd=cwd) / spec.hooks_relpath


def _json_env(path: Path) -> dict | None:
    if not path.exists():
        return None
    data = config.load_json(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    entry = servers.get(config.MCP_NAME)
    if not isinstance(entry, dict):
        return None
    env = entry.get("env")
    return env if isinstance(env, dict) else None


def read_env(spec: RuntimeSpec, scope: str, *, cwd: Path | None = None) -> dict | None:
    """Return the managed env block for one runtime/scope."""
    path = config_path(spec, scope, cwd=cwd)
    if spec.config_format == "json":
        return _json_env(path)
    if not path.exists():
        return None
    return toml_edit.read_env(path.read_text(encoding="utf-8"))


def read_user_id(spec: RuntimeSpec, scope: str, *, cwd: Path | None = None) -> str | None:
    """Return the installed MANSIO user id, if any."""
    env = read_env(spec, scope, cwd=cwd)
    if not env:
        return None
    value = env.get("MANSIO_USER_ID")
    return value if isinstance(value, str) and value else None


def read_url(spec: RuntimeSpec, scope: str, *, cwd: Path | None = None) -> str | None:
    """Return the installed MANSIO URL, if any."""
    env = read_env(spec, scope, cwd=cwd)
    if not env:
        return None
    value = env.get("MANSIO_URL")
    return value if isinstance(value, str) and value else None


def used_user_ids(spec: RuntimeSpec, *, cwd: Path | None = None) -> set[str]:
    """Return every user id installed for *spec* in either scope."""
    used: set[str] = set()
    for scope in ("project", "user"):
        user_id = read_user_id(spec, scope, cwd=cwd)
        if user_id:
            used.add(user_id)
    return used


def resolve_connection(
    poll_format: str = "plain",
    *,
    cwd: Path | None = None,
    url: str | None = None,
    user_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the URL and user id for ``poll`` and ``doctor``.

    Explicit *url*/*user_id* win, then the environment, then the installed
    project config, then the user config.  ``plain`` searches both runtimes.
    """
    url = url or env_or("MANSIO_URL")
    user_id = user_id or env_or("MANSIO_USER_ID")
    if url and user_id:
        return url, user_id

    if poll_format == "claude":
        specs = [RUNTIMES["claude-code"]]
    elif poll_format == "codex":
        specs = [RUNTIMES["codex"]]
    else:
        specs = [RUNTIMES["claude-code"], RUNTIMES["codex"]]

    for scope in ("project", "user"):
        for spec in specs:
            if not url:
                url = read_url(spec, scope, cwd=cwd)
            if not user_id:
                user_id = read_user_id(spec, scope, cwd=cwd)
    return url, user_id


def install(
    spec: RuntimeSpec,
    scope: str,
    *,
    url: str,
    user_id: str,
    token: str | None,
    cwd: Path | None = None,
    dry_run: bool = False,
) -> list[config.WriteResult]:
    """Install or update the runtime config and hook.

    *token* is written only when the caller passes a value (``--write-token``).
    """
    secrets = (token,) if token else ()
    results: list[config.WriteResult] = []
    path = config_path(spec, scope, cwd=cwd)

    if spec.config_format == "json":
        data = config.load_json(path)
        config.merge_claude_settings(data, url=url, user_id=user_id, token=token)
        results.append(
            config.write_if_changed(path, config.dump_json(data), dry_run=dry_run, secrets=secrets)
        )
        return results

    old = path.read_text(encoding="utf-8") if path.exists() else ""
    env = {"MANSIO_URL": url, "MANSIO_USER_ID": user_id}
    remove_keys: set[str] = {"MANSIO_TOKEN"}
    if token is not None:
        env["MANSIO_TOKEN"] = token
        remove_keys = set()
    new = toml_edit.upsert_server(
        old,
        command="mansio-mcp",
        args=[],
        env=env,
        remove_env_keys=remove_keys,
    )
    results.append(config.write_if_changed(path, new, dry_run=dry_run, secrets=secrets))

    hooks_file = hooks_path(spec, scope, cwd=cwd)
    if hooks_file is not None:
        hooks = config.load_json(hooks_file)
        config.merge_session_start_hook(hooks, config.hook_command(spec.poll_format))
        results.append(
            config.write_if_changed(
                hooks_file, config.dump_json(hooks), dry_run=dry_run, secrets=secrets
            )
        )
    return results


def uninstall(
    spec: RuntimeSpec, scope: str, *, cwd: Path | None = None, dry_run: bool = False
) -> list[config.WriteResult]:
    """Remove only the MANSIO MCP entry and managed hooks."""
    results: list[config.WriteResult] = []
    path = config_path(spec, scope, cwd=cwd)

    if spec.config_format == "json":
        if path.exists():
            data = config.load_json(path)
            config.remove_claude_settings(data)
            results.append(config.write_if_changed(path, config.dump_json(data), dry_run=dry_run))
        return results

    if path.exists():
        old = path.read_text(encoding="utf-8")
        results.append(config.write_if_changed(path, toml_edit.remove_server(old), dry_run=dry_run))

    hooks_file = hooks_path(spec, scope, cwd=cwd)
    if hooks_file is not None and hooks_file.exists():
        hooks = config.load_json(hooks_file)
        config.remove_session_start_hooks(hooks)
        results.append(
            config.write_if_changed(hooks_file, config.dump_json(hooks), dry_run=dry_run)
        )
    return results


def has_installed_hook(spec: RuntimeSpec, scope: str, *, cwd: Path | None = None) -> bool:
    """Whether the managed hook is present for *spec*/*scope*."""
    path = hooks_path(spec, scope, cwd=cwd) or config_path(spec, scope, cwd=cwd)
    if not path.exists():
        return False
    data = config.load_json(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    groups = hooks.get("SessionStart")
    if not isinstance(groups, list):
        return False
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_hooks = group.get("hooks")
        if not isinstance(group_hooks, list):
            continue
        if any(config.is_mansio_poll_hook(hook) for hook in group_hooks):
            return True
    return False
