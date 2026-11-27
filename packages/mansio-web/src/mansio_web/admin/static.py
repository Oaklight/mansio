"""Static assets for the admin panel UI.

The HTML and its css/js files ship as package data under ``assets/``.  The
asset map is a fixed whitelist: an unknown name is rejected before any
package resource is read, so a request cannot walk out of the assets tree.

Files are read on first use and cached by path for the process lifetime;
there is no invalidation because package assets do not change at runtime.
Importing this module does not touch the package data.
"""

from __future__ import annotations

import importlib.resources
from typing import Final

_PACKAGE = __package__ or __name__

_JS = "text/javascript; charset=utf-8"

ASSET_TYPES: Final[dict[str, str]] = {
    "admin.css": "text/css; charset=utf-8",
    "api.js": _JS,
    "app.js": _JS,
    "auth.js": _JS,
    "contracts.js": _JS,
    "format.js": _JS,
    "models.js": _JS,
    "router.js": _JS,
    "state.js": _JS,
    "theme-init.js": _JS,
    "ui.js": _JS,
    "views/channels.js": _JS,
    "views/messages.js": _JS,
    "views/overview.js": _JS,
    "views/prefixes.js": _JS,
    "views/subscriptions.js": _JS,
    "views/system.js": _JS,
    "views/tokens.js": _JS,
    "views/users.js": _JS,
}

_CACHE: dict[str, str] = {}


def _read(name: str) -> str:
    """Read one package resource."""
    return importlib.resources.files(_PACKAGE).joinpath(*name.split("/")).read_text("utf-8")


def _cached(name: str) -> str:
    """Read one package resource once, caching its text for the process."""
    cached = _CACHE.get(name)
    if cached is None:
        cached = _read(name)
        _CACHE[name] = cached
    return cached


def get_admin_html() -> str:
    """Return the admin HTML shell, reading it on first use."""
    return _cached("admin.html")


def get_asset(name: str) -> tuple[str, str] | None:
    """Return ``(body, content_type)`` for a whitelisted asset.

    ``name`` is matched against the fixed map before any file access, so
    traversal segments and unknown suffixes return ``None``.
    """
    content_type = ASSET_TYPES.get(name)
    if content_type is None:
        return None
    return _cached(f"assets/{name}"), content_type
