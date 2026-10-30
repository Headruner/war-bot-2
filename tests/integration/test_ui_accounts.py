"""Browser test of the account UI: main badge, linked alts, remove, delete account (real SSO against the fake)."""

from __future__ import annotations

import httpx
import pytest
from helpers import evedm_env, evedm_server, fake_eve, free_port, sso_flow

pytestmark = pytest.mark.integration
playwright = pytest.importorskip("playwright.sync_api")
FIRST = 2_100_000_000


def test_account_ui(tmp_path):
    port = free_port()
    with fake_eve(characters=5) as eve:
        env = evedm_env(tmp_path / "data", eve, port)
        with evedm_server(env, log_path=tmp_path / "server.out") as (base, _):
            c = httpx.Client(base_url=base)
            sso_flow(c, "/sso/login", FIRST)
            sso_flow(c, "/sso/add", FIRST + 1)
            cookie = c.cookies.get("evedm_session")
            with playwright.sync_playwright() as p:
                try:
                    b = p.chromium.launch()
                except Exception as exc:  # browser binaries not installed
                    pytest.skip(f"chromium not available: {exc}")
                ctx = b.new_context(viewport={"width": 1280, "height": 900})
                ctx.add_cookies([{"name": "evedm_session", "value": cookie, "url": base}])
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("dialog", lambda d: d.accept())
                page.goto(f"{base}/characters")
                page.wait_for_selector("#char-rows tr")
                rows = page.locator("#char-rows tr")
                assert rows.count() == 2
                main_row = rows.filter(has=page.locator(".badge.main"))
                assert main_row.count() == 1 and main_row.locator("button.danger").is_disabled()
                assert "Signed in as" in page.locator(".card.account").inner_text()
                assert "Sign out" in page.locator(".nav").inner_text()

                alt_row = rows.filter(has_not=page.locator(".badge.main"))
                alt_row.locator("button.danger").click()  # confirm() accepted
                page.wait_for_function("document.querySelectorAll('#char-rows tr').length === 1")

                page.click("#delete-account")
                page.wait_for_url(f"{base}/login")
                assert "Sign in with your" in page.content()
                page.goto(f"{base}/characters")
                assert page.url == f"{base}/login"
                assert errors == []
                b.close()
            assert c.get("/api/characters").status_code == 401
