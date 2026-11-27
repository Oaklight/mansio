"""Admin UI asset serving: whitelist, content types, module graph."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mansio_core import Bus, MemoryBackend
from mansio_web.admin.server import AdminServer
from mansio_web.admin.static import ADMIN_ASSETS, get_asset

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


class TestAdminAssets:
    @pytest.mark.parametrize("name", sorted(ADMIN_ASSETS))
    def test_served_with_type_and_cache_header(self, admin_url, name):
        status, headers, body = _fetch(f"{admin_url}/assets/{name}")
        assert status == 200
        assert body.strip()
        assert headers["Content-Type"] == ADMIN_ASSETS[name][1]
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
        assert on_disk == set(ADMIN_ASSETS)


class TestModuleGraph:
    def test_relative_imports_resolve_to_shipped_assets(self):
        for name in ADMIN_ASSETS:
            if not name.endswith(".js"):
                continue
            source = (ASSETS_DIR / name).read_text("utf-8")
            for target in _IMPORT_RE.findall(source):
                resolved = ((ASSETS_DIR / name).parent / target).resolve()
                assert resolved.exists(), f"{name} imports missing {target}"
                relative = resolved.relative_to(ASSETS_DIR.resolve()).as_posix()
                assert relative in ADMIN_ASSETS, f"{name} imports unshipped {target}"
