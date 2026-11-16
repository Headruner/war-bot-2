"""Terms of Service and Privacy Policy for the Discord Developer Portal: public, static, linked from nowhere."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_accounts_web import as_user
from test_settings_admin import MIO, settings

from evedm.wars.config import WarsSettings
from evedm.web.app import create_app


@pytest.fixture
def anonymous(tmp_path):
    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=False))) as c:
        yield c


def test_both_pages_are_open_without_signing_in(anonymous):
    terms, privacy = anonymous.get("/terms"), anonymous.get("/privacy")
    assert terms.status_code == 200 and "Terms of Service" in terms.text and "as is" in terms.text
    assert privacy.status_code == 200 and "Privacy Policy" in privacy.text and "refresh tokens" in privacy.text
    assert "text/html" in terms.headers["content-type"]


@pytest.fixture
def member(tmp_path):
    with TestClient(create_app(settings(tmp_path / "m"), WarsSettings(enabled=False))) as c:
        c.app.state.svc.db.upsert_user(MIO, "Shadowed Mio")
        yield as_user(c, MIO, "Shadowed Mio")


def test_no_page_of_the_site_links_to_them(anonymous, member):
    signed_out, signed_in = ("/login", "/terms", "/privacy"), ("/", "/characters", "/roster", "/settings")
    for client, paths in ((anonymous, signed_out), (member, signed_in)):
        for path in paths:
            html = client.get(path).text
            assert 'href="/terms"' not in html and 'href="/privacy"' not in html, path
