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

_ASSET_TYPES: Final[dict[str, str]] = {
    "admin.css": "text/css; charset=utf-8",
    "api.js": "text/javascript; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "auth.js": "text/javascript; charset=utf-8",
    "refresh.js": "text/javascript; charset=utf-8",
    "state.js": "text/javascript; charset=utf-8",
    "theme-init.js": "text/javascript; charset=utf-8",
    "util.js": "text/javascript; charset=utf-8",
    "views/channels.js": "text/javascript; charset=utf-8",
    "views/dashboard.js": "text/javascript; charset=utf-8",
    "views/messages.js": "text/javascript; charset=utf-8",
    "views/subscriptions.js": "text/javascript; charset=utf-8",
    "views/system.js": "text/javascript; charset=utf-8",
    "views/tokens.js": "text/javascript; charset=utf-8",
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
