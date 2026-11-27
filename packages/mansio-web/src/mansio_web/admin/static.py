"""Static assets for the admin panel UI.

The HTML and its css/js files ship as package data under ``assets/``.  The
asset map is a fixed whitelist: an unknown name is rejected before any
package resource is read, so a request cannot walk out of the assets tree.
"""

from __future__ import annotations

import importlib.resources
from typing import Final

_PACKAGE = __package__ or __name__

_ADMIN_HTML: Final[str] = (
    importlib.resources.files(_PACKAGE).joinpath("admin.html").read_text("utf-8")
)

_JS = "text/javascript; charset=utf-8"

_ASSET_TYPES: Final[dict[str, str]] = {
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


def _read_asset(name: str) -> str:
    path = name.split("/")
    return importlib.resources.files(_PACKAGE).joinpath("assets", *path).read_text("utf-8")


ADMIN_HTML: Final[str] = _ADMIN_HTML
ADMIN_ASSETS: Final[dict[str, tuple[str, str]]] = {
    name: (_read_asset(name), content_type) for name, content_type in _ASSET_TYPES.items()
}


def get_asset(name: str) -> tuple[str, str] | None:
    """Return ``(body, content_type)`` for a whitelisted asset.

    ``name`` is matched against the fixed map before any file access, so
    traversal segments and unknown suffixes return ``None``.
    """
    return ADMIN_ASSETS.get(name)
