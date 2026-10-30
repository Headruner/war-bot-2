"""Browser tests (Playwright + Chromium) against a real demo server: no JS errors, list + map + paste work."""

from __future__ import annotations

import pytest
from helpers import evedm_env, evedm_server, free_port

pytestmark = pytest.mark.integration
playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = free_port()
    env = evedm_env(
        tmp_path_factory.mktemp("ui"),
        "http://127.0.0.1:9",
        port,
        EVEDM_DEMO="1",
        EVE_CLIENT_ID="",
        EVE_CLIENT_SECRET="",
        EVEDM_WEB_AUTH="none",
    )
    with evedm_server(env) as (base, _):
        yield base


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binaries not installed
            pytest.skip(f"chromium not available: {exc}")
        yield b
        b.close()


def open_page(browser, url, **kw):
    page = browser.new_page(**kw)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(url)
    return page, errors


def canvas_has_pixels(page) -> bool:
    return page.evaluate("""() => {
        const c = document.querySelector('#map');
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        let lit = 0;
        for (let i = 0; i < d.length; i += 4 * 97) if (d[i] + d[i + 1] + d[i + 2] > 120) lit++;
        return lit > 50;
    }""")


def test_ranking_list_and_map(server, browser):
    page, errors = open_page(browser, f"{server}/?target=Tama", viewport={"width": 1440, "height": 860})
    page.wait_for_selector(".row.selected")
    rows = page.locator(".row")
    assert rows.count() == 9
    assert "CLOSEST" in rows.nth(0).inner_text().upper() and "Kael Varn" in rows.nth(0).inner_text()
    assert "Kael Varn: Jita → Tama · 4 jumps" in page.locator("#route-bar").inner_text()
    assert canvas_has_pixels(page)
    rows.nth(4).click()  # Vex Aldane, 8 jumps
    assert "selected" in (rows.nth(4).get_attribute("class") or "")
    assert "Vex Aldane: Dodixie → Tama · 8 jumps" in page.locator("#route-bar").inner_text()
    rows.nth(8).click()  # wormhole character
    assert "no stargate route" in page.locator("#route-bar").inner_text()
    assert errors == []


def test_autocomplete_and_paste(server, browser):
    page, errors = open_page(browser, server, viewport={"width": 1280, "height": 800})
    page.fill("#target", "ama")
    page.wait_for_selector("#suggest li")
    assert "Amarr" in page.locator("#suggest").inner_text()
    page.click("#paste-btn")
    page.fill("#paste-text", "Fortizar under attack https://evemaps.dotlan.net/system/Amamake")
    page.click("#paste-find")
    page.wait_for_selector(".row.selected")
    assert page.locator("#target-info h1").inner_text().startswith("Amamake")
    assert "target=Amamake" in page.url
    assert errors == []


def test_characters_page_renders_every_row(server, browser):
    page, errors = open_page(browser, f"{server}/characters")
    page.wait_for_selector("#char-rows tr")
    assert page.locator("#char-rows tr").count() == 10
    assert "Re-auth required" in page.locator("#char-rows").inner_text()
    assert errors == []


def test_phone_layout_puts_map_above_list(server, browser):
    page, errors = open_page(
        browser, f"{server}/?target=Tama", viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
    )
    page.wait_for_selector(".row.selected")
    map_box = page.locator(".map-pane").bounding_box()
    list_box = page.locator(".list-pane").bounding_box()
    assert map_box["y"] < list_box["y"]
    assert page.evaluate("document.documentElement.scrollWidth") <= 390  # no horizontal scroll (WEB-03)
    assert errors == []
