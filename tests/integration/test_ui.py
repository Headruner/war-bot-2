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
    assert page.locator(".row:visible").count() == 3  # the 3 closest; the rest is under "Show all"
    page.click("#show-all")
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


def test_phone_map_above_inline_settings_and_ranked_characters(server, browser):
    page, errors = open_page(
        browser, f"{server}/?target=Tama", viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
    )
    page.wait_for_selector(".row.selected", state="attached")
    gear = page.get_by_role("button", name="Map controls")
    map_box = page.locator("#map").bounding_box()
    assert map_box["y"] >= page.locator(".topbar").bounding_box()["height"] and map_box["height"] < 844
    row = page.locator("#rows .row").first
    assert map_box["height"] <= row.bounding_box()["y"] < 844
    assert page.locator("#target").is_hidden() and page.locator("#route-bar").is_visible()
    gear.click()
    panel = page.locator("#mobile-map-controls")
    box = panel.bounding_box()
    assert box["y"] >= map_box["height"]
    assert row.bounding_box()["y"] >= box["y"] + box["height"]
    assert page.locator("#target").input_value() == "Tama"
    page.get_by_role("button", name="Zoom in").click()
    assert page.locator("#map-opts, #map-opts-btn, #refresh-btn").count() == 0
    assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
    gear.click()
    assert panel.is_hidden() and row.bounding_box()["y"] < 844
    page.locator("#rows .row").nth(1).click()
    assert "selected" in page.locator("#rows .row").nth(1).get_attribute("class")
    gear.click()
    page.set_viewport_size({"width": 1280, "height": 800})
    page.locator("body > #target-form #target").wait_for(state="visible")
    page.locator(".map-pane #zoom-in").wait_for(state="visible")
    assert page.locator("#mobile-map-controls").is_hidden()
    assert errors == []


@pytest.mark.parametrize("width", [390, 1280])
def test_navigation_position_across_pages(server, browser, width):
    page, _ = open_page(browser, f"{server}/?target=Tama", viewport={"width": width, "height": 844})
    nav = page.get_by_role("navigation", name="Main navigation")
    initial = nav.bounding_box()
    page.wait_for_selector("#rows .row")
    if width == 390:
        page.get_by_role("button", name="Map controls").click()
        assert nav.bounding_box() == initial
    page.click("#zoom-in")
    view = page.evaluate("({...map.view})")
    page.evaluate("rank(state.target.name, {refresh: 1, keepSelection: true})")
    assert page.evaluate("({...map.view})") == view
    for path in ("characters", "structures", "notifications", "alerts", "wars", "roster"):
        page.goto(f"{server}/{path}")
        assert nav.bounding_box() == initial
        page.evaluate("window.scrollBy(0, 300)")
        assert nav.bounding_box() == initial
    page.close()


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
    page.wait_for_function("document.querySelectorAll('#loot-rows tr').length === 14")  # any security, all space
    first = page.locator("#loot-rows tr").first.inner_text()
    assert first.split()[0] == "0"  # nearest first: a character is in that system
    page.select_option("#f-space", "high")
    page.wait_for_function("document.querySelectorAll('#loot-rows tr').length === 10")
    page.select_option("#f-side", "aggressor")
    page.wait_for_function("document.querySelectorAll('#loot-rows tr').length === 3")
    page.locator("#loot-rows tr .target-chip").first.click()  # rank the structure's system
    page.wait_for_selector(".row.selected")
    assert "target=Jita" in page.url
    page.goto(f"{server}/wars")
    page.wait_for_function("document.querySelectorAll('#loot-rows tr').length === 3")
    assert page.locator(".tab.active").inner_text() == "Structures at war"
    assert page.input_value("#f-side") == "aggressor" and page.input_value("#f-space") == "high"
    cookie = next(c for c in ctx.cookies() if c["name"] == "evedm_prefs")
    assert cookie["sameSite"] == "Lax" and cookie["path"] == "/" and not cookie["httpOnly"]
    assert cookie["expires"] - time.time() > 300 * 86400  # about a year
    assert errors == []
    ctx.close()


def test_auto_refresh_is_remembered_and_map_has_no_option_boxes(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/?target=Tama")
    page.wait_for_selector(".row.selected")
    assert page.input_value("#auto-refresh") == "30"  # server default until changed
    # no map check-boxes, legend or refresh button
    assert page.locator("#map-opts, #map-opts-btn, .legend, #refresh-btn").count() == 0
    page.select_option("#auto-refresh", "0")
    page.reload()
    page.wait_for_selector(".row.selected")
    assert page.input_value("#auto-refresh") == "0"
    assert page.evaluate("[map.opts.gates, map.opts.labels, map.opts.chars]") == [True, True, True]
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
    assert page.input_value("#s-source") == "assets"  # the default source: structures with assets
    page.select_option("#s-source", "dock")  # remembered like the other filters; demo has none that are dock-only
    page.wait_for_function("document.querySelectorAll('#s-rows tr').length === 0")
    page.reload()
    page.wait_for_function("document.querySelector('#s-info').textContent.includes('can dock')")
    assert page.input_value("#s-source") == "dock"
    page.select_option("#s-source", "assets")
    page.wait_for_function("document.querySelectorAll('#s-rows tr').length === 1")
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


def test_each_online_row_sets_the_destination_and_offline_rows_have_no_button(server, browser):
    """The destination button sits on each online row, never on the route bar."""
    page, errors = open_page(browser, f"{server}/?target=Tama", viewport={"width": 1440, "height": 860})
    page.wait_for_selector(".row.selected")
    assert page.locator("#route-bar .set-route").count() == 0
    assert "Set destination" not in page.inner_text("#route-bar")
    page.click("#show-all")  # every character, not only the 3 closest
    assert page.locator("#rows .row", has_text="Kael Varn").locator(".set-route").count() == 0  # offline
    online = page.locator("#rows .row:has(.badge.online)").count()
    assert online >= 1
    assert page.locator("#rows .row .set-route").count() == online
    juno = page.locator("#rows .row", has_text="Juno Krell")
    btn = juno.locator(".set-route button")
    assert btn.inner_text() == "Set destination" and not btn.is_disabled()
    assert btn.evaluate("b => getComputedStyle(b).backgroundColor") == "rgb(155, 231, 166)"  # light green
    jumps = juno.locator(".jumps")
    assert jumps.locator(".set-route button").count() == 1  # the button sits under the number of jumps
    box_j, box_b = jumps.locator("b").first.bounding_box(), btn.bounding_box()
    assert box_b["y"] > box_j["y"] + box_j["height"] - 1
    row_box = juno.bounding_box()
    assert row_box["x"] + row_box["width"] - (box_b["x"] + box_b["width"]) < 24  # aligned right
    selected = page.locator(".row.selected").inner_text()
    with page.expect_response(lambda r: r.url.endswith("/destination") and r.request.method == "POST"):
        btn.click()
    assert page.locator(".row.selected").inner_text() == selected  # the click did not select the row
    page.wait_for_function("![...document.querySelectorAll('.set-route button')].some((b) => b.disabled)")
    assert juno.locator(".set-route .small").inner_text() == ""  # no confirmation when the destination is set
    assert errors == []


def test_ranking_row_layout_and_trimmed_route_names(server, browser):
    """Ship next to the name, no ship name or docked state, ghost system chip, 3 closest + Show all, 7-symbol route."""
    page, errors = open_page(browser, f"{server}/?target=Tama", viewport={"width": 1440, "height": 860})
    page.wait_for_selector(".row.selected")
    rows = page.locator("#rows .row")
    total = rows.count()
    assert total > 3
    assert page.locator("#rows .row:visible").count() == 3  # only the 3 closest
    assert page.locator("#show-all").inner_text() == f"Show all ({total})"
    first = rows.first
    assert first.locator(".name .ship-type").count() == 1  # the hull is next to the character name
    text = first.inner_text()
    assert "Docked" not in text and "In space" not in text and "Ship:" not in text
    chip = first.locator(".where .sec")
    assert chip.evaluate("c => getComputedStyle(c).backgroundColor") == "rgba(0, 0, 0, 0)"  # transparent
    page.click("#show-all")
    assert page.locator("#rows .row:visible").count() == total
    page.click("#show-all")
    assert page.locator("#rows .row:visible").count() == 3
    # route bar: names only, at most 7 symbols, no security chips
    assert page.locator("#route-bar .path .sec").count() == 0
    hops = page.locator("#route-bar .path .hop:not(.wh):not(.ax)").all_inner_texts()
    assert hops and all(len(h) <= 7 for h in hops)
    assert page.locator("#thera-info").is_hidden()
    assert errors == []


# --------------------------------------------------------------------------- static roster (UC-12)
def test_roster_tab_ranks_static_characters_and_draws_a_route(server, browser):
    page, errors = open_page(browser, f"{server}/?target=Jita")
    page.wait_for_selector(".row")
    page.click('.tab[data-tab="roster"]')
    page.wait_for_selector("#roster-rows .row")
    rows = page.locator("#roster-rows .row")
    assert rows.count() >= 6
    first = rows.first.inner_text().lower()  # badges are upper-cased by the stylesheet
    assert "static" in first and "closest" in first
    assert "jump" in first  # ranked by gates, like the live list
    assert "Set destination" not in page.inner_text("#route-bar")  # a static entry has no token (D10)
    assert canvas_has_pixels(page)  # the route of the selected static character is on the map

    last = rows.last.inner_text().lower()  # unresolved, ranked last, with suggestions
    assert "unknown system" in last and "did you mean jita" in last
    assert page.locator("#roster-count").inner_text().strip() != ""
    assert errors == []


def test_roster_page_fixes_an_unresolved_entry(server, browser):
    page, errors = open_page(browser, f"{server}/roster")
    page.wait_for_selector("#roster-rows tr")
    assert "unresolved" in page.inner_text("#roster-stats")
    page.select_option("#filter-state", "unresolved")
    page.wait_for_function("document.querySelectorAll('#roster-rows tr').length === 1")
    assert "Pax Ilun" in page.inner_text("#roster-rows")

    page.reload()  # the filter is kept in this browser (prefs cookie, ROS-21)
    page.wait_for_selector("#roster-rows tr")
    assert page.input_value("#filter-state") == "unresolved"

    page.click("#roster-rows .suggest-fix button")  # "did you mean Jita?"
    page.wait_for_function("document.querySelectorAll('#roster-rows tr').length === 0")
    page.select_option("#filter-state", "")
    page.wait_for_selector("#roster-rows tr")
    assert "0 unresolved" in page.inner_text("#roster-stats")
    assert errors == []


# --------------------------------------------------------------------------- notifications (UC-13)
def test_notifications_page_shows_region_and_closest_characters(server, browser):
    page, errors = open_page(browser, f"{server}/notifications")
    page.wait_for_selector(".card.ntf")
    cards = page.locator(".card.ntf")
    assert cards.count() == 6  # the seventh demo notification is of a type filtered out by default

    first = cards.first.inner_text()
    assert "Asset Safety" in first and "Hek" in first and "Metropolis" in first  # title, system, region
    assert "EVE Time" in first
    assert "Your closest characters:" in first  # from the Characters set
    assert "Roster:" in page.inner_text("#ntf-list")  # and from the Roster set
    assert errors == []


def test_notification_filters_open_other_types_and_survive_a_reload(server, browser):
    page, errors = open_page(browser, f"{server}/notifications")
    page.wait_for_selector(".card.ntf")
    page.click("#ntf-filters-btn")
    assert "InsurancePayoutMsg" in page.inner_text("#ntf-groups")  # seen, but not in a default group

    page.click("#ntf-all")
    page.wait_for_function("document.querySelectorAll('.card.ntf').length === 7")
    page.reload()
    page.wait_for_selector(".card.ntf")
    assert page.locator(".card.ntf").count() == 7  # the choice is kept in this browser (prefs cookie)

    page.click("#ntf-filters-btn")
    page.click("#ntf-defaults")
    page.wait_for_function("document.querySelectorAll('.card.ntf').length === 6")
    assert errors == []


def test_notification_sound_only_chimes_for_new_matching_items(server, browser):
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page.add_init_script("""window.chimes = 0;
        const start = OscillatorNode.prototype.start;
        OscillatorNode.prototype.start = function (...args) { window.chimes++; return start.apply(this, args); };
    """)
    page.goto(f"{server}/notifications")
    page.wait_for_selector(".card.ntf")
    assert not page.locator("#ntf-sound").is_checked()
    page.locator("#ntf-sound").check()
    page.wait_for_function("audio.state === 'running'")
    payload = page.request.get(f"{server}/api/notifications").json()
    page.route("**/api/notifications?*", lambda route: route.fulfill(json=payload))

    def refresh():
        with page.expect_response(lambda resp: "/api/notifications?" in resp.url):
            page.click("#ntf-refresh")
        page.wait_for_function("!state.busy")

    refresh()
    assert page.evaluate("window.chimes") == 0
    item = payload["items"][0]
    payload["items"].insert(0, {**item, "notification_id": item["notification_id"] + 1000000})
    refresh()
    refresh()
    assert page.evaluate("window.chimes") == 1
    payload["items"].append({**item, "notification_id": item["notification_id"] + 2000000})
    page.select_option("#ntf-hours", "720")
    page.wait_for_function("!state.busy")
    assert page.evaluate("window.chimes") == 1
    page.locator("#ntf-sound").uncheck()
    payload["items"].insert(0, {**item, "notification_id": item["notification_id"] + 3000000})
    refresh()
    assert page.evaluate("window.chimes") == 1
    page.close()


def test_notifications_fit_a_phone(server, browser):
    page, _ = open_page(browser, f"{server}/notifications", viewport={"width": 360, "height": 800},
                        is_mobile=True, has_touch=True)
    page.wait_for_selector(".card.ntf")
    assert page.evaluate("document.documentElement.scrollWidth") <= 360


# --------------------------------------------------------------------------- Thera / Turnur filter (UC-15)
def test_thera_filter_draws_a_two_leg_route_and_is_remembered(server, browser):
    page, errors = open_page(browser, f"{server}/?target=Amarr", viewport={"width": 1440, "height": 860})
    page.wait_for_selector(".row.selected")
    assert "via Thera" not in page.locator("#rows").inner_text()
    assert page.locator("#thera-info").is_hidden()
    assert page.locator('#hub-toggle input[value=""]').is_checked()
    page.locator('#hub-toggle input[value="thera"]').check()
    page.wait_for_selector(".badge.wh")
    assert page.locator("#thera-info").is_hidden()  # the "EVE-Scout: n listed systems" line is gone
    page.locator(".row", has=page.locator(".badge.wh")).first.click()
    bar = page.locator("#route-bar").inner_text()
    assert "via Thera" in bar and "wormhole in and out" in bar
    assert page.locator("#route-bar .hop.wh").count() == 1
    assert page.locator(".legend").count() == 0
    assert canvas_has_pixels(page)
    page.reload()
    page.wait_for_selector(".row.selected")
    assert page.locator('#hub-toggle input[value="thera"]').is_checked()  # the choice lives in the evedm_prefs cookie
    page.wait_for_selector(".badge.wh")
    page.locator('#hub-toggle input[value="turnur"]').check()  # switching hubs re-ranks through the other mid-system
    page.wait_for_function("state.hub === 'turnur' && !state.busy")
    assert page.locator("#thera-info").is_hidden()
    page.locator('#hub-toggle input[value=""]').check()
    page.wait_for_function("!document.querySelector('.badge.wh')")
    assert errors == []


def test_phone_navigation_fits_two_rows(server, browser):
    page, errors = open_page(browser, f"{server}/?target=Tama", viewport={"width": 360, "height": 800}, is_mobile=True)
    page.wait_for_selector(".row.selected", state="attached")
    nav = page.get_by_role("navigation", name="Main navigation")
    links = nav.locator("a, button")
    tops = {round(links.nth(i).bounding_box()["y"]) for i in range(links.count())}
    assert len(tops) <= 2  # two rows at most
    assert nav.evaluate("n => n.scrollWidth <= n.clientWidth")  # nothing to scroll sideways
    assert page.evaluate("document.documentElement.scrollWidth") <= 360
    assert errors == []
