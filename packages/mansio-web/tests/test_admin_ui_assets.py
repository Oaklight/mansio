"""Admin UI asset serving: whitelist, content types, module graph."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_web.admin.server import AdminServer
from mansio_web.admin.static import ASSET_TYPES, get_asset

ASSETS_DIR = Path(__file__).resolve().parents[1] / "src" / "mansio_web" / "admin" / "assets"
_IMPORT_RE = re.compile(r"from\s+['\"](\.[^'\"]+)['\"]")


@pytest.fixture()
def admin_url():
    bus = Bus(backend=MemoryBackend())
    admin = AdminServer(bus, port=0)
    info = admin.start()
    yield info.url
    admin.stop()
    bus.close()


def _fetch(url: str) -> tuple[int, dict, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8")


class TestAdminHtml:
    def test_has_no_inline_style_or_script(self, admin_url):
        status, headers, body = _fetch(admin_url + "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert "<style" not in body
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", body)

    def test_references_the_asset_files(self, admin_url):
        body = _fetch(admin_url + "/")[2]
        assert 'href="/assets/admin.css"' in body
        assert 'src="/assets/theme-init.js"' in body
        assert 'src="/assets/app.js"' in body

    @pytest.mark.parametrize("handler", ["onclick=", "onchange=", "oninput=", "onsubmit="])
    def test_has_no_inline_event_handlers(self, handler):
        body = (ASSETS_DIR.parent / "admin.html").read_text("utf-8")
        assert handler not in body

    def test_has_no_window_handler_bindings(self):
        app = (ASSETS_DIR / "app.js").read_text("utf-8")
        assert "Object.assign(window" not in app
        assert "onclick" not in app


class TestAdminAssets:
    @pytest.mark.parametrize("name", sorted(ASSET_TYPES))
    def test_served_with_type_and_cache_header(self, admin_url, name):
        status, headers, body = _fetch(f"{admin_url}/assets/{name}")
        assert status == 200
        assert body.strip()
        assert headers["Content-Type"] == ASSET_TYPES[name]
        assert headers["Cache-Control"] == "no-cache"

    @pytest.mark.parametrize(
        "name",
        ["missing.js", "notes.txt", "../server.py", "views/../api.js", "admin.html"],
    )
    def test_unknown_or_traversal_is_404(self, admin_url, name):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _fetch(f"{admin_url}/assets/{name}")
        assert exc.value.code == 404


class TestAssetWhitelist:
    @pytest.mark.parametrize(
        "name",
        ["", ".", "..", "../server.py", "views/../api.js", "admin.html", "notes.txt"],
    )
    def test_rejects_before_reading_resources(self, name):
        assert get_asset(name) is None

    def test_every_shipped_asset_is_whitelisted(self):
        on_disk = {
            path.relative_to(ASSETS_DIR).as_posix()
            for path in ASSETS_DIR.rglob("*")
            if path.is_file()
        }
        assert on_disk == set(ASSET_TYPES)

    def test_import_reads_no_package_resource(self, monkeypatch):
        import importlib
        import importlib.resources

        import mansio_web.admin.static as static

        def fail(*args, **kwargs):
            raise AssertionError("static read a package resource at import")

        monkeypatch.setattr(importlib.resources, "files", fail)
        try:
            importlib.reload(static)
        finally:
            monkeypatch.undo()
            importlib.reload(static)

    def test_asset_is_read_once_and_cached(self, monkeypatch):
        import mansio_web.admin.static as static

        static._CACHE.clear()
        calls: list[str] = []
        original = static._read

        def counting(name: str) -> str:
            calls.append(name)
            return original(name)

        monkeypatch.setattr(static, "_read", counting)
        first = static.get_asset("app.js")
        second = static.get_asset("app.js")
        assert first == second
        assert calls == ["assets/app.js"]

    def test_admin_html_is_read_on_demand(self, monkeypatch):
        import mansio_web.admin.static as static

        static._CACHE.clear()
        calls: list[str] = []
        original = static._read

        def counting(name: str) -> str:
            calls.append(name)
            return original(name)

        monkeypatch.setattr(static, "_read", counting)
        assert static.get_admin_html().strip()
        assert calls == ["admin.html"]

    @pytest.mark.parametrize("name", ["missing.js", "notes.txt", "../server.py", "views/../api.js"])
    def test_unknown_name_is_rejected_before_any_read(self, name, monkeypatch):
        import mansio_web.admin.static as static

        calls: list[str] = []

        def counting(path: str) -> str:
            calls.append(path)
            return ""

        monkeypatch.setattr(static, "_read", counting)
        assert static.get_asset(name) is None
        assert calls == []


class TestNoDynamicMarkup:
    _BANNED = (
        re.compile(r"\.innerHTML\s*="),
        re.compile(r"\.outerHTML\s*="),
        re.compile(r"insertAdjacentHTML\s*\("),
        re.compile(r"document\.write\s*\("),
    )

    def test_assets_build_dom_with_nodes_not_strings(self):
        offenders = []
        for path in sorted(ASSETS_DIR.rglob("*.js")):
            text = path.read_text("utf-8")
            for pattern in self._BANNED:
                if pattern.search(text):
                    offenders.append(f"{path.name}: {pattern.pattern}")
        assert not offenders


class TestModuleGraph:
    def test_relative_imports_resolve_to_shipped_assets(self):
        for name in ASSET_TYPES:
            if not name.endswith(".js"):
                continue
            source = (ASSETS_DIR / name).read_text("utf-8")
            for target in _IMPORT_RE.findall(source):
                resolved = ((ASSETS_DIR / name).parent / target).resolve()
                assert resolved.exists(), f"{name} imports missing {target}"
                relative = resolved.relative_to(ASSETS_DIR.resolve()).as_posix()
                assert relative in ASSET_TYPES, f"{name} imports unshipped {target}"
