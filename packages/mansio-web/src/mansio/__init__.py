"""Compatibility import root for the pre-split ``mansio`` package.

The implementation moved to :mod:`mansio_core` (types, bus, backends,
authorization) and :mod:`mansio_web` (frontends, admin panel, server, CLI).
This module keeps ``import mansio`` and the documented ``mansio.*``
submodule paths working for one release.  New code should import
``mansio_core`` or ``mansio_web`` directly.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from typing import Any

from mansio_core.backends import MemoryBackend, SQLiteBackend
from mansio_core.backends.sqlite import SCHEMA_VERSION, SchemaVersionError, backup_database
from mansio_core.bus import Bus, SQLiteBus
from mansio_core.metrics import MetricsCollector
from mansio_core.protocols import (
    Backend,
    ChannelStore,
    Compactable,
    Deletable,
    Presenceable,
)
from mansio_core.system_policy import CompactionPolicy, system_channel_policy
from mansio_core.types import (
    ALLOWED_PERMISSIONS,
    PERMISSION_LEVELS,
    ROLE_PRESETS,
    VERBS,
    ACLEntry,
    ChannelMeta,
    ClaimResult,
    Message,
    UserPresence,
    permission_allows,
)
from mansio_web import __version__  # noqa: F401
from mansio_web.server import MansioServer
from mansio_web.transport_http import HttpTransport, MansioAPIError

__all__ = [
    "ALLOWED_PERMISSIONS",
    "ACLEntry",
    "PERMISSION_LEVELS",
    "ROLE_PRESETS",
    "UserPresence",
    "VERBS",
    "Backend",
    "ChannelMeta",
    "ChannelStore",
    "Bus",
    "ClaimResult",
    "Compactable",
    "Deletable",
    "CompactionPolicy",
    "MaildirBackend",
    "MetricsCollector",
    "NATSBackend",
    "HttpTransport",
    "MansioAPIError",
    "MemoryBackend",
    "MemoryStorage",
    "Message",
    "MansioServer",
    "Presenceable",
    "SCHEMA_VERSION",
    "SQLiteBackend",
    "SQLiteBus",
    "SQLiteStorage",
    "SchemaVersionError",
    "StorageBackend",
    "backup_database",
    "permission_allows",
    "system_channel_policy",
]


# Old submodule path -> new module.  Longest prefix wins, so
# ``mansio.admin.metrics`` resolves to core rather than to mansio_web.admin.
_ROUTES = {
    "mansio.types": "mansio_core.types",
    "mansio.protocols": "mansio_core.protocols",
    "mansio.bus": "mansio_core.bus",
    "mansio.authz": "mansio_core.authz",
    "mansio.token_store": "mansio_core.token_store",
    "mansio.system_policy": "mansio_core.system_policy",
    "mansio.metrics": "mansio_core.metrics",
    "mansio.admin.metrics": "mansio_core.metrics",
    "mansio.backends": "mansio_core.backends",
    "mansio.transport": "mansio_core.transport",
    "mansio.server": "mansio_web.server",
    "mansio.cli": "mansio_web.cli",
    "mansio.frontends": "mansio_web.frontends",
    "mansio.admin": "mansio_web.admin",
    "mansio.transport_http": "mansio_web.transport_http",
}
_ROUTES_BY_LENGTH = sorted(_ROUTES.items(), key=lambda item: -len(item[0]))


def _target_for(fullname: str) -> str | None:
    for old, new in _ROUTES_BY_LENGTH:
        if fullname == old:
            return new
        if fullname.startswith(old + "."):
            return new + fullname[len(old) :]
    return None


_MODULE_METADATA = {
    "__name__",
    "__loader__",
    "__package__",
    "__spec__",
    "__path__",
    "__file__",
    "__cached__",
    "__builtins__",
}


class _AliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolve ``mansio.*`` through non-mutating proxy modules.

    The proxy copies the target's attributes so class and function identity
    is preserved, but it never hands the target module object to the import
    machinery.  That keeps ``target.__spec__`` intact for reload,
    introspection and pickling.
    """

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: Any = None,
    ) -> importlib.machinery.ModuleSpec | None:
        target_name = _target_for(fullname)
        if target_name is None:
            return None
        target_spec = importlib.util.find_spec(target_name)
        return importlib.machinery.ModuleSpec(
            fullname,
            self,
            origin=target_spec.origin if target_spec else None,
            is_package=bool(target_spec and target_spec.submodule_search_locations is not None),
        )

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> Any:
        """Return None so importlib creates a fresh, ordinary module."""
        return None

    def exec_module(self, module: Any) -> None:
        target_name = _target_for(module.__name__)
        if target_name is None:  # pragma: no cover - find_spec already checked
            raise ImportError(module.__name__)
        target = importlib.import_module(target_name)
        for name, value in vars(target).items():
            if name not in _MODULE_METADATA:
                setattr(module, name, value)

        def __getattr__(name: str) -> Any:
            nested = f"{module.__name__}.{name}"
            if nested in _ROUTES:
                return importlib.import_module(nested)
            return getattr(target, name)

        module.__getattr__ = __getattr__

    def get_code(self, fullname: str) -> Any:
        """Return the aliased module's code object (needed by ``python -m``)."""
        target = _target_for(fullname)
        if target is None:  # pragma: no cover - find_spec already checked
            raise ImportError(fullname)
        module = importlib.import_module(target)
        spec = module.__spec__
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ImportError(fullname)
        return spec.loader.get_code(spec.name)

    def get_source(self, fullname: str) -> str | None:
        target = _target_for(fullname)
        if target is None:  # pragma: no cover
            raise ImportError(fullname)
        module = importlib.import_module(target)
        spec = module.__spec__
        if spec is None or spec.loader is None:  # pragma: no cover
            return None
        return spec.loader.get_source(spec.name)

    def is_package(self, fullname: str) -> bool:
        target = _target_for(fullname)
        if target is None:  # pragma: no cover
            raise ImportError(fullname)
        module = importlib.import_module(target)
        spec = module.__spec__
        return bool(spec and spec.submodule_search_locations is not None)


if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())


_DEPRECATED_ALIASES = {
    "SQLiteStorage": ("SQLiteBackend", SQLiteBackend),
    "MemoryStorage": ("MemoryBackend", MemoryBackend),
    "StorageBackend": ("Backend", Backend),
}


def __getattr__(name: str):
    """Lazy import for submodules, optional backends and deprecated aliases."""
    import warnings

    if f"mansio.{name}" in _ROUTES:
        return importlib.import_module(f"mansio.{name}")

    if name in _DEPRECATED_ALIASES:
        new_name, obj = _DEPRECATED_ALIASES[name]
        warnings.warn(
            f"mansio.{name} is deprecated, use mansio.{new_name} instead. "
            "It will be removed in mansio 1.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return obj

    if name == "MaildirBackend":
        from mansio_core.backends.maildir import MaildirBackend

        return MaildirBackend
    if name == "NATSBackend":
        from mansio_core.backends.nats import NATSBackend

        return NATSBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
