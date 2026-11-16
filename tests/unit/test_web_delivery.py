"""Delivery of pages and assets: compressed JSON/HTML, long-lived cache for versioned static files, revalidation
for everything else (no-cache keeps the pages eligible for the browser's back/forward cache)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_accounts_web import as_user
from test_settings_admin import MIO, settings

from evedm.wars.config import WarsSettings
from evedm.web.app import ASSET_V, HERE, create_app

web_static = HERE / "static"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=False))) as c:
        c.app.state.svc.db.upsert_user(MIO, "Shadowed Mio")
        yield as_user(c, MIO, "Shadowed Mio")


def test_versioned_static_files_are_cached_for_a_year(client):
    r = client.get(f"/static/app.css?v={ASSET_V}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_unversioned_static_files_are_revalidated(client):
    r = client.get("/static/app.css")
    assert r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
    assert client.get("/static/app.css", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_pages_and_api_answers_are_revalidated_not_stored_off(client):
    for url in ("/", "/api/health"):
        cc = client.get(url).headers["cache-control"]
        assert cc == "no-cache" and "no-store" not in cc
    assert 'defer src="/static/' in client.get("/").text  # scripts never block the parser


def test_large_answers_are_compressed(client):
    r = client.get("/api/settings", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip" and r.headers["vary"].lower().find("accept-encoding") >= 0
    assert r.json()["groups"]  # the client decompressed it transparently
    small = client.get("/api/targets/recent", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers  # under the 1 KB minimum: not worth compressing


def test_the_universe_map_keeps_its_own_cache_and_encoding(client):
    r = client.get("/api/universe/map", headers={"Accept-Encoding": "gzip"})
    assert r.headers["cache-control"] == "public, max-age=86400" and r.headers["content-encoding"] == "gzip"
    assert client.get("/api/universe/map", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_every_page_loads_the_shared_helpers_before_its_own_script(client):
    html = client.get("/").text
    assert 0 < html.index("/static/common.js") < html.index("/static/rank.js")  # deferred scripts run in order
    for name in ("alerts", "characters", "notifications", "rank", "roster", "settings", "structures", "wars"):
        text = (web_static / f"{name}.js").read_text(encoding="utf-8")
        assert "async function api(" not in text and "const $ =" not in text, name  # one copy only, in common.js
