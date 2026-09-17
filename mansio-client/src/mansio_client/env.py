"""Environment-variable resolution shared by the CLI and MCP entry points.

Canonical names are ``MANSIO_URL``, ``MANSIO_USER_ID``, and ``MANSIO_TOKEN``.
Two generations of older names are still accepted, each with a
``DeprecationWarning``:

- ``MANSIO_AGENT_ID`` — pre-``user_id`` rename (see the agent_id → user_id
  refactor); documented by the adapter examples as ``MANSIO_USER_ID`` since
  the mansio-mcp entry point landed.
- ``PIAZZA_*`` — pre-project-rename.
"""

from __future__ import annotations

import os
import warnings

# Canonical name -> deprecated aliases, highest precedence first.
_LEGACY_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "MANSIO_URL": ("PIAZZA_URL",),
    "MANSIO_USER_ID": ("MANSIO_AGENT_ID", "PIAZZA_AGENT_ID"),
    "MANSIO_TOKEN": ("PIAZZA_TOKEN",),
}


def env_or(name: str, default: str | None = None) -> str | None:
    """Read ``name`` from the environment, falling back to deprecated aliases.

    Args:
        name: Canonical environment variable name.
        default: Returned when neither the canonical name nor any alias is set.

    Returns:
        The resolved value, or ``default``.
    """
    val = os.environ.get(name)
    if val is not None:
        return val
    for legacy in _LEGACY_ENV_ALIASES.get(name, ()):
        legacy_val = os.environ.get(legacy)
        if legacy_val:
            warnings.warn(
                f"{legacy} is deprecated, use {name} instead",
                DeprecationWarning,
                stacklevel=2,
            )
            return legacy_val
    return default
