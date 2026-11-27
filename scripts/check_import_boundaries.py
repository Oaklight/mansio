#!/usr/bin/env python3
"""Check the package dependency rules of the core/web split.

Rules:
  R1  mansio_core imports neither mansio_web nor mansio_client.
  R2  mansio_web imports mansio_core, never mansio_client.
  R3  mansio_client imports mansio_core, never mansio_web.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ROOT / "packages"

RULES = {
    "mansio-core": {"mansio_web", "mansio_client"},
    "mansio-web": {"mansio_client"},
    "mansio-client": {"mansio_web"},
}


def imported_roots(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        return [node.module.split(".")[0]]
    return []


def main() -> int:
    violations: list[str] = []
    for package, forbidden in RULES.items():
        src = PACKAGES / package / "src"
        if not src.exists():
            violations.append(f"{package}: missing source tree {src}")
            continue
        for path in sorted(src.rglob("*.py")):
            if "_vendor" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                for root in imported_roots(node):
                    if root in forbidden:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: "
                            f"{package} must not import {root}"
                        )
    if violations:
        print("Import boundary violations:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1
    print("Import boundaries OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
