"""Browser tests (Playwright + Chromium) against a real demo server: no JS errors, list + map + paste work."""

from __future__ import annotations

import time

import pytest
from helpers import ROOT, evedm_env, evedm_server, free_port

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
        EVEDM_WARS_ENABLED="1",  # demo mode: sample wars, no network
        EVEDM_WARS_CONFIG=str(ROOT / "config" / "wars.yaml"),
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


def test_wars_page_lists_wars_and_ranks_a_structure_system(server, browser):
    page, errors = open_page(browser, f"{server}/wars", viewport={"width": 1280, "height": 900})
    page.wait_for_selector(".war")
    assert page.locator(".war").count() == 3
    first = page.locator("#war-990001")
    assert "Crimson Ledger Syndicate → Quiet Harbor Logistics" in first.inner_text()
    assert [c.strip().split()[1] for c in first.locator(".targets .target-chip").all_inner_texts()] == [
        "Tama",
        "Kedama",
        "Nourvukaiken",
    ]
    page.select_option("#war-type", "mutual")
    page.wait_for_function("document.querySelectorAll('.war').length === 1")
    assert "Ashen Veil" in page.locator(".war").inner_text()
    page.select_option("#war-type", "")
    page.wait_for_function("document.querySelectorAll('.war').length === 3")
    page.locator("#war-990001 .targets .target-chip").first.click()
    page.wait_for_selector(".row.selected")
    assert "target=Tama" in page.url and page.locator(".row").count() == 9
    assert errors == []


def test_wars_page_fits_a_phone(server, browser):
    page, errors = open_page(
        browser, f"{server}/wars", viewport={"width": 360, "height": 800}, is_mobile=True, has_touch=True
    )
    page.wait_for_selector(".war")
    assert page.evaluate("document.documentElement.scrollWidth") <= 360
    assert errors == []


def test_wars_tabs_and_filters_survive_a_reload(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/wars")
    page.wait_for_selector(".war")
    page.click("#tab-btn-stats")
    page.wait_for_selector(".kpi")
    assert page.locator(".kpi").first.inner_text().startswith("8")
    assert "Crimson Ledger Syndicate" in page.locator("#top-aggressors").inner_text()
    page.click("#tab-btn-loot")
    page.wait_for_function("document.querySelectorAll('#loot-rows tr').length === 10")
    first = page.locator("#loot-rows tr").first.inner_text()
    assert first.split()[0] == "0"  # nearest first: a character is in that system
    page.select_option("#f-side", "aggressor")
    page.wait_for_function("document.querySelectorAll('#loot-rows tr').length === 3")
    page.locator("#loot-rows tr .target-chip").first.click()  # rank the structure's system
    page.wait_for_selector(".row.selected")
    assert "target=Jita" in page.url
    page.goto(f"{server}/wars")
    page.wait_for_function("document.querySelectorAll('#loot-rows tr').length === 3")
    assert page.locator(".tab.active").inner_text() == "High-sec structures"
    assert page.input_value("#f-side") == "aggressor"
    cookie = next(c for c in ctx.cookies() if c["name"] == "evedm_prefs")
    assert cookie["sameSite"] == "Lax" and cookie["path"] == "/" and not cookie["httpOnly"]
    assert cookie["expires"] - time.time() > 300 * 86400  # about a year
    assert errors == []
    ctx.close()


def test_map_options_and_auto_refresh_are_remembered(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/?target=Tama")
    page.wait_for_selector(".row.selected")
    assert page.input_value("#auto-refresh") == "30"  # server default until changed
    page.click("#map-opts-btn")
    assert page.is_visible("#map-opts")
    page.uncheck("[data-opt=gates]")
    page.uncheck("[data-opt=labels]")
    page.select_option("#auto-refresh", "0")
    page.reload()
    page.wait_for_selector(".row.selected")
    assert page.input_value("#auto-refresh") == "0"
    assert page.evaluate("[map.opts.gates, map.opts.labels, map.opts.chars]") == [False, False, True]
    page.click("#map-opts-btn")
    assert not page.is_checked("[data-opt=gates]") and page.is_checked("[data-opt=chars]")
    assert canvas_has_pixels(page)  # the map still draws (systems, route)
    other = browser.new_page()  # another browser profile: defaults
    other.goto(f"{server}/?target=Tama")
    other.wait_for_selector(".row.selected")
    assert other.input_value("#auto-refresh") == "30"
    assert errors == []
    ctx.close()


def test_broken_prefs_cookie_falls_back_to_defaults(server, browser):
    ctx = browser.new_context()
    ctx.add_cookies([{"name": "evedm_prefs", "value": "%7Bnot-json", "url": server}])
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/wars")
    page.wait_for_selector(".war")
    assert page.locator(".tab.active").inner_text() == "New wars"
    page.goto(f"{server}/?target=Tama")
    page.wait_for_selector(".row.selected")
    assert page.input_value("#auto-refresh") == "30"
    assert errors == []
    ctx.close()


def test_structures_page_lists_every_space_and_remembers_filters(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/structures")
    page.wait_for_function("document.querySelectorAll('#s-rows tr').length === 7")
    assert "no gates" in page.locator("#s-rows tr").last.inner_text()  # the wormhole structure
    page.select_option("#s-space", "low")
    page.wait_for_function("document.querySelectorAll('#s-rows tr').length === 2")
    page.fill("#s-q", "ember")
    page.wait_for_function("document.querySelectorAll('#s-rows tr').length === 1")
    assert (
        "Ashen Veil" in page.locator("#s-rows").inner_text()
        and "AT WAR" in page.locator("#s-rows").inner_text().upper()
    )
    page.reload()
    page.wait_for_function("document.querySelectorAll('#s-rows tr').length === 1")
    assert page.input_value("#s-space") == "low" and page.input_value("#s-q") == "ember"
    page.locator("#s-rows .target-chip").first.click()
    page.wait_for_selector(".row.selected")
    assert "target=Amamake" in page.url
    assert errors == []
    ctx.close()


def test_alerts_page_filters_deep_links_and_offline_characters(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/alerts")
    page.wait_for_function("document.querySelectorAll('#a-groups > li').length === 3")
    cards = page.locator("#a-groups > li")
    assert "Perimeter - Tollgate Market" in cards.nth(0).inner_text() and "2 combat" in cards.nth(0).inner_text()
    hek = cards.nth(1).inner_text()
    assert "CORE DROP" in hek.upper() and "Hek" in hek and "Your closest characters" in hek
    # offline characters cannot get a destination; the reason is on the button
    first = cards.nth(0).locator(".closest button").first
    assert first.is_disabled() and first.get_attribute("title") == "Only for an online character"
    # filters apply at once and survive a reload (prefs cookie)
    page.uncheck(".a-kind[value=combat]")
    page.wait_for_function("document.querySelectorAll('#a-groups > li').length === 2")
    page.check("#a-core")
    page.wait_for_function("document.querySelectorAll('#a-groups > li').length === 1")
    page.reload()
    page.wait_for_function("document.querySelectorAll('#a-groups > li').length === 1")
    assert page.is_checked("#a-core") and not page.is_checked(".a-kind[value=combat]")
    page.uncheck(".a-kind[value=destroyed]")
    page.uncheck(".a-kind[value=firing]")
    page.wait_for_selector("#a-empty:not([hidden])")  # nothing chosen: nothing listed
    # a Discord link to an older alert of a structure opens that structure's card
    page.check(".a-kind[value=combat]")
    page.uncheck("#a-core")
    older = page.evaluate("fetch('/api/alerts').then(r => r.json()).then(d => d.groups[0].alerts[1].alert_id)")
    page.goto(f"{server}/alerts#alert-{older}")
    page.wait_for_selector("#a-groups > li.flash")
    assert "Tollgate Market" in page.locator("#a-groups > li.flash").inner_text()
    assert errors == []
    ctx.close()


def test_route_bar_sets_the_destination_of_an_online_character(server, browser):
    page, errors = open_page(browser, f"{server}/?target=Tama", viewport={"width": 1440, "height": 860})
    page.wait_for_selector(".row.selected")
    btn = page.locator("#route-bar .set-route button")
    assert btn.inner_text() == "Set destination: Tama" and btn.is_disabled()  # Kael Varn is offline
    assert "only for an online character" in page.locator("#route-bar .set-route").inner_text()
    page.locator(".row", has_text="Juno Krell").click()
    btn = page.locator("#route-bar .set-route button")
    assert not btn.is_disabled()
    btn.click()
    page.wait_for_function("document.querySelector('#route-bar .set-route').innerText.includes('demo')")
    assert "demo: destination not sent" in page.locator("#route-bar .set-route").inner_text()
    assert errors == []
