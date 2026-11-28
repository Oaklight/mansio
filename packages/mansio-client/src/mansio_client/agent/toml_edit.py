"""Minimal editing of the MANSIO TOML table.

Python 3.10 is supported, so ``tomllib`` is not available everywhere.  The
editor changes only the ``[mcp_servers.mansio]`` and
``[mcp_servers.mansio.env]`` blocks, leaving comments, unrelated tables,
and unknown keys untouched.  When ``tomllib`` is available (3.11+), the
full file is parsed before and after the edit and an unparsable result is
refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SERVER_TABLE = "mcp_servers.mansio"
ENV_TABLE = "mcp_servers.mansio.env"

_HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")
_STRING_RE = re.compile(r'^\s*([A-Za-z0-9_-]+)\s*=\s*"(.*)"\s*(?:#.*)?$')
_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


class TomlEditError(ValueError):
    """The config cannot be edited without losing information."""


@dataclass
class _Block:
    header: str | None
    lines: list[str]


def _split_blocks(text: str) -> list[_Block]:
    blocks: list[_Block] = []
    current = _Block(None, [])
    for line in text.splitlines(keepends=True):
        match = _HEADER_RE.match(line)
        if match:
            blocks.append(current)
            current = _Block(match.group(1).strip(), [line])
        else:
            current.lines.append(line)
    blocks.append(current)
    return blocks


def _join(blocks: list[_Block]) -> str:
    text = "".join("".join(block.lines) for block in blocks)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _find(blocks: list[_Block], table: str) -> int | None:
    for index, block in enumerate(blocks):
        if block.header == table:
            return index
    return None


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _unescape(value: str) -> str:
    return (
        value.replace("\\\\", "\\")
        .replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
    )


def _reject_unsupported(text: str) -> None:
    """Reject inline or dotted forms of the MANSIO server table."""
    table: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        header = _HEADER_RE.match(line)
        if header:
            table = header.group(1).strip()
            continue
        if re.match(r"^mcp_servers\s*=", stripped):
            raise TomlEditError(
                "found inline 'mcp_servers = {...}'; mansio-agent writes "
                "[mcp_servers.mansio] and [mcp_servers.mansio.env] tables. "
                "Convert the entry to table form or remove it and re-run."
            )
        if re.match(r"^mcp_servers\s*\.\s*mansio\s*=", stripped) or re.match(
            r"^mcp_servers\s*\.\s*mansio\s*\.", stripped
        ):
            raise TomlEditError(
                "found inline or dotted 'mcp_servers.mansio'; mansio-agent writes "
                "[mcp_servers.mansio] and [mcp_servers.mansio.env] tables. "
                "Convert the entry to table form or remove it and re-run."
            )
        if table == "mcp_servers" and re.match(r"^mansio\s*=\s*\{", stripped):
            raise TomlEditError(
                "found inline 'mansio = {...}' under [mcp_servers]; mansio-agent writes "
                "[mcp_servers.mansio] and [mcp_servers.mansio.env] tables. "
                "Convert the entry to table form or remove it and re-run."
            )


def _structural_check(text: str) -> None:
    _reject_unsupported(text)


def validate(text: str) -> None:
    """Raise :class:`TomlEditError` if *text* is not a valid config.

    With ``tomllib`` (3.11+) the whole file is parsed.  On 3.10 only the
    structural checks run.
    """
    if not text.strip():
        return
    _structural_check(text)
    try:
        import tomllib
    except ModuleNotFoundError:
        return
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"config does not parse as TOML: {exc}") from exc


def read_env(text: str) -> dict[str, str] | None:
    """Return the ``[mcp_servers.mansio.env]`` string values, if present."""
    blocks = _split_blocks(text)
    index = _find(blocks, ENV_TABLE)
    if index is None:
        return None
    env: dict[str, str] = {}
    for line in blocks[index].lines[1:]:
        match = _STRING_RE.match(line)
        if match:
            env[match.group(1)] = _unescape(match.group(2))
    return env


def _update_table(
    blocks: list[_Block],
    table: str,
    values: dict[str, str],
    *,
    remove_keys: set[str],
    newline: str,
) -> None:
    index = _find(blocks, table)
    if index is None:
        if not values:
            return
        if blocks and blocks[-1].lines and not blocks[-1].lines[-1].endswith(newline):
            blocks[-1].lines[-1] += newline
        if blocks and "".join(blocks[-1].lines).strip():
            blocks.append(_Block(None, [newline]))
        lines = [f"[{table}]{newline}"]
        lines.extend(f"{key} = {value}{newline}" for key, value in values.items())
        blocks.append(_Block(table, lines))
        return

    block = blocks[index]
    header = block.lines[0]
    body = block.lines[1:]
    updated_keys: set[str] = set()
    new_body: list[str] = []

    for line in body:
        match = _KEY_RE.match(line)
        if match:
            key = match.group(1)
            if key in values:
                new_body.append(f"{key} = {values[key]}{newline}")
                updated_keys.add(key)
                continue
            if key in remove_keys:
                continue
        new_body.append(line)

    missing = [key for key in values if key not in updated_keys]
    if missing:
        insert_at = len(new_body)
        while insert_at > 0 and not new_body[insert_at - 1].strip():
            insert_at -= 1
        additions = [f"{key} = {values[key]}{newline}" for key in missing]
        new_body[insert_at:insert_at] = additions

    block.lines = [header, *new_body]


def upsert_server(
    text: str,
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
    remove_env_keys: set[str] | None = None,
) -> str:
    """Insert or update the MANSIO server tables.

    Args:
        text: Existing TOML text (empty for a new file).
        command: Value for ``command``.
        args: Value for ``args`` (rendered as a TOML array of strings).
        env: Managed ``[mcp_servers.mansio.env]`` keys and values.
        remove_env_keys: Managed keys to delete from the env table.

    Returns:
        The edited text.

    Raises:
        TomlEditError: If the file uses an inline/dotted MANSIO entry, or the
            result does not parse.
    """
    validate(text)
    newline = _newline(text)
    blocks = _split_blocks(text)
    rendered_args = "[" + ", ".join(_quote(value) for value in args) + "]"
    _update_table(
        blocks,
        SERVER_TABLE,
        {"command": _quote(command), "args": rendered_args},
        remove_keys=set(),
        newline=newline,
    )
    _update_table(
        blocks,
        ENV_TABLE,
        {key: _quote(value) for key, value in env.items()},
        remove_keys=remove_env_keys or set(),
        newline=newline,
    )
    result = _join(blocks)
    if not text.strip():
        result = result.lstrip("\r\n")
    validate(result)
    if read_env(result) is None:
        raise TomlEditError("edited config has no [mcp_servers.mansio.env] table")
    return result


def remove_server(text: str) -> str:
    """Remove the MANSIO server tables, preserving everything else."""
    validate(text)
    blocks = _split_blocks(text)
    kept = [block for block in blocks if block.header not in (SERVER_TABLE, ENV_TABLE)]
    result = _join(kept)
    # Collapse the extra blank lines a removed table can leave behind.
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    validate(result)
    return result
