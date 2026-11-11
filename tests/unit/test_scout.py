"""Structure scout: player-owned structures found through every registered character's assets, shared by all mains."""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from common import mint
from evedm.characters import REAUTH
from evedm.config import ANSIBLEX_SCOPE, STRUCTURE_SCOPES, WAYPOINT_SCOPE, Settings, load_settings
from evedm.esi import EsiError
from evedm.scout import ResolveError, StructureScout
from evedm.sso import SsoError
from evedm.wars.config import WarsSettings
from evedm.wars.structures import Structure, StructureIndex
from evedm.web.app import create_app
from fake_eve import ALL_SCOPES

pytestmark = pytest.mark.anyio
H = {"X-EVEDM": "1"}
A, B, C = 2100000001, 2100000002, 2100000003  # characters of the fake (also used as mains here)
S_HIGH, S_NULL, S_WH, S_PRIVATE, S_GONE = (
    1_035_000_000_001,
    1_035_000_000_002,
    1_035_000_000_003,
    1_035_000_000_004,
    1_035_000_000_005,
)
CORP = 98123456
JITA, DQ1, THERA = 30000142, 30004759, 31000005
ALL = ALL_SCOPES + STRUCTURE_SCOPES


async def link(services, cid: int, owner: int, scopes=ALL) -> None:
    tokens = await mint(services["http"], cid, scopes=scopes)
    ts = await services["auth"].refresh(tokens["refresh_token"])
    services["chars"].store(ts, owner_id=owner)


def asset(item_id: int, location_id: int, location_type: str = "item", flag: str = "Hangar") -> dict:
    return {
        "item_id": item_id,
        "location_id": location_id,
        "location_type": location_type,
        "location_flag": flag,
        "type_id": 587,
        "quantity": 1,
        "is_singleton": True,
    }


@pytest.fixture
def world(fake_app):
    st = fake_app.state.fake
    st.corp_names[CORP] = "Citadel Owners Inc"
    st.structures.update(
        {
            S_HIGH: {
                "name": "Jita - Trade Hub",
                "owner_id": CORP,
                "solar_system_id": JITA,
                "type_id": 35832,
                "position": {"x": 1.5e11, "y": -2.0e9, "z": 7.25e10},
                "acl": [A],
            },
            S_NULL: {"name": "1DQ1-A - Keep", "owner_id": CORP, "solar_system_id": DQ1, "type_id": 35834, "acl": [B]},
            S_WH: {"name": "J-Home", "owner_id": CORP, "solar_system_id": THERA, "type_id": 35832, "acl": None},
            S_PRIVATE: {"name": "Secret", "owner_id": CORP, "solar_system_id": JITA, "type_id": 35825, "acl": [C]},
        }
    )
    st.assets[A] = [
        asset(1, S_HIGH, "other"),  # ESI types an item inside a structure "other"
        asset(2, 1, flag="Cargo"),  # inside A's own ship (item 1): not a structure
        asset(3, 60003760, "station"),  # NPC station: never listed
        asset(4, S_WH, "other"),
        asset(5, S_GONE, "other"),  # an ID ESI does not know as a structure
    ]
    # B is not on S_HIGH's ACL
    st.assets[B] = [asset(11, S_NULL, "other"), asset(12, S_HIGH, "other"), asset(13, S_PRIVATE, "other")]
    return st


def scout_for(settings, services) -> StructureScout:
    return StructureScout(settings, services["db"], services["ranking"].g, services["chars"], services["esi"])


async def test_assets_reveal_structures_for_everyone(settings, services, world, graph):
    await link(services, A, A)
    await link(services, B, B)
    await link(services, C, C, scopes=ALL_SCOPES)  # no structure scopes: does not take part
    scout = scout_for(settings, services)
    stats = await scout.run()
    assert stats["characters_read"] == 2 and stats["resolved"] == 3
    db = services["db"]
    status = {r[0]: r[1] for r in db.query("SELECT structure_id, status FROM known_structures")}
    assert status == {S_HIGH: "ok", S_NULL: "ok", S_WH: "ok", S_PRIVATE: "no_access", S_GONE: "gone"}
    rows = {r["structure_id"]: r for r in scout.structures()}
    assert set(rows) == {S_HIGH, S_NULL, S_WH}  # never NPC stations, items, unknown or unreadable IDs
    assert rows[S_HIGH]["seen_by"] == 2 and rows[S_HIGH]["owner"] == "Citadel Owners Inc"
    assert rows[S_HIGH]["system"]["space"] == "high" and rows[S_NULL]["system"]["space"] == "null"
    assert rows[S_WH]["system"]["space"] == "wormhole" and rows[S_HIGH]["type"] == "Type 35832"
    # shared: B's characters see A's structures with jumps from B's own location
    listing = scout.listing({graph.index[DQ1]: ["Bravo"]}, [])
    by_id = {r["structure_id"]: r for r in listing["rows"]}
    assert by_id[S_NULL]["jumps"] == 0 and by_id[S_HIGH]["jumps"] > 0 and by_id[S_WH]["jumps"] is None
    assert listing["scout"]["contributing_characters"] == 2
    # unreachable last, then the structures nobody may read: listed with their ID and status only
    assert [r["structure_id"] for r in listing["rows"]][-2:] == [S_WH, S_PRIVATE]
    unread = by_id[S_PRIVATE]
    assert unread["status"] == "no_access" and unread["system"]["space"] == "unknown" and unread["jumps"] is None
    assert unread["name"] == f"Unknown structure {S_PRIVATE}" and unread["owner"] == "Unknown"
    assert by_id[S_HIGH]["status"] == "ok" and S_GONE not in by_id
    # positions are kept for "combat on grid" alerts, but not shown in the listing
    assert rows[S_HIGH]["position"] == (1.5e11, -2.0e9, 7.25e10) and rows[S_NULL]["position"] is None
    assert {s.structure_id: s.position for s in scout.index()[CORP]}[S_HIGH] == (1.5e11, -2.0e9, 7.25e10)
    assert "position" not in by_id[S_HIGH]


async def test_unreadable_by_one_character_is_read_with_another(settings, services, world):
    await link(services, B, B)
    scout = scout_for(settings, services)
    await scout.run()
    db = services["db"]
    assert db.query("SELECT status FROM known_structures WHERE structure_id=?", (S_HIGH,))[0][0] == "no_access"
    await link(services, A, A)
    db.execute("UPDATE known_structures SET checked_at=0")
    await scout.run()
    assert db.query("SELECT status, name FROM known_structures WHERE structure_id=?", (S_HIGH,))[0][:] == (
        "ok",
        "Jita - Trade Hub",
    )


async def test_unknown_structure_resolved_by_reader_without_assets(settings, services, world):
    await link(services, B, B)
    scout = scout_for(settings, services)
    await scout.run()
    assert S_PRIVATE in {row["structure_id"] for row in scout.unresolved()}

    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    services["db"].execute("UPDATE known_structures SET checked_at=0 WHERE structure_id=?", (S_PRIVATE,))
    await scout.run()
    row = next(row for row in scout.structures() if row["structure_id"] == S_PRIVATE)
    assert row["name"] == "Secret" and row["owner"] == "Citadel Owners Inc"
    assert row["owner_zkill"] == f"https://zkillboard.com/corporation/{CORP}/"
    assert row["seen_by"] == 1
    assert S_PRIVATE not in {row["structure_id"] for row in scout.unresolved()}


async def test_refresh_intervals_and_unlinked_characters(settings, services, world, fake_app):
    await link(services, A, A)
    scout = scout_for(settings, services)
    await scout.run()
    calls = dict(fake_app.state.fake.calls)
    await scout.run()  # nothing due: no asset or structure reads
    assert fake_app.state.fake.calls.get("assets") == calls.get("assets")
    assert fake_app.state.fake.calls.get("structure") == calls.get("structure")
    services["db"].execute("UPDATE scout_characters SET checked_at=0")
    await scout.run()
    assert fake_app.state.fake.calls["assets"] == calls["assets"] + 1
    await services["chars"].remove(A, A)  # nobody links A any more: its sightings go, the intel stays
    await scout.run()
    db = services["db"]
    assert db.query("SELECT COUNT(*) FROM structure_sightings")[0][0] == 0
    assert {r["structure_id"] for r in scout.structures()} == {S_HIGH, S_WH}


async def test_reauth_and_demo_characters_never_contribute(settings, services, world):
    await link(services, A, A)
    services["db"].set_status(A, "REAUTH", A)
    assert scout_for(settings, services).contributors() == []


async def test_paged_assets_and_truncation(settings, services, world):
    world.asset_page_size = 2
    await link(services, A, A)
    settings.structures.max_asset_pages = 2
    scout = scout_for(settings, services)
    stats = await scout.run()
    db = services["db"]
    row = db.query("SELECT structures, error, filter_v, checked_at FROM scout_characters WHERE character_id=?", (A,))[0]
    # three pages, two read: the own-item set is incomplete, so nothing is saved and the filter stays old
    assert stats["truncated"] == 1 and stats["characters_read"] == 0 and row["error"].startswith("truncated")
    assert row["structures"] == 0 and row["filter_v"] == 1 and row["checked_at"] > 0  # not read again every run
    assert db.query("SELECT COUNT(*) FROM structure_sightings")[0][0] == 0
    settings.structures.max_asset_pages = 20
    db.execute("UPDATE scout_characters SET checked_at=0")
    stats = await scout.run()
    row = db.query("SELECT structures, error, filter_v FROM scout_characters WHERE character_id=?", (A,))[0]
    assert row["error"] is None and row["structures"] == 3 and stats["truncated"] == 0 and row["filter_v"] == 2


async def test_item_typed_locations_are_never_structures(settings, services, world):
    container = 1_050_000_000_999  # a can in a station hangar; its row is on a page that is not read
    world.asset_page_size = 2
    world.assets[A] = [
        asset(21, container),  # a ship or can's content: "item" typed, flag Hangar, parent on another page
        asset(22, S_HIGH, "other"),
        asset(23, 60003760, "station"),
        asset(container, 60003760, "station"),
    ]
    await link(services, A, A)
    settings.structures.max_asset_pages = 1
    scout = scout_for(settings, services)
    await scout.run()
    db = services["db"]
    # cut short: the can may be on a page not read, so nothing at all is saved
    assert db.query("SELECT COUNT(*) FROM known_structures")[0][0] == 0
    settings.structures.max_asset_pages = 20  # the whole list: the can is the character's own item, not a structure
    db.execute("UPDATE scout_characters SET checked_at=0")
    await scout.run()
    assert [r[0] for r in db.query("SELECT structure_id FROM known_structures")] == [S_HIGH]
    row = db.query("SELECT structures FROM scout_characters WHERE character_id=?", (A,))[0]
    assert row["structures"] == 1


async def test_refused_structures_are_asked_again_only_after_the_long_interval(settings, services, world):
    await link(services, B, B)  # B may not dock at S_HIGH or S_PRIVATE: both answer 403
    settings.structures.structure_refresh_hours = 72  # refresh known structures too, to tell the two intervals apart
    scout = scout_for(settings, services)
    await scout.run()
    db = services["db"]
    assert {r[0]: r[1] for r in db.query("SELECT structure_id, status FROM known_structures")} == {
        S_NULL: "ok",
        S_HIGH: "no_access",
        S_PRIVATE: "no_access",
    }
    old = time.time() - 100 * 3600  # past structure_refresh_hours (72), inside no_access_refresh_hours (336)
    db.execute("UPDATE known_structures SET checked_at=?", (old,))
    await scout.run()
    seen = {r[0]: r[1] for r in db.query("SELECT structure_id, checked_at FROM known_structures")}
    assert seen[S_NULL] > old and seen[S_HIGH] == old and seen[S_PRIVATE] == old
    older = time.time() - 400 * 3600
    db.execute("UPDATE known_structures SET checked_at=? WHERE status='no_access'", (older,))
    await scout.run()
    seen = {r[0]: r[1] for r in db.query("SELECT structure_id, checked_at FROM known_structures")}
    assert seen[S_HIGH] > older and seen[S_PRIVATE] > older


async def test_structures_already_read_are_skipped_on_the_next_run(settings, services, world, fake_app):
    await link(services, A, A)
    scout = scout_for(settings, services)
    await scout.run()
    db = services["db"]
    calls = fake_app.state.fake.calls["structure"]
    stamps = {r[0]: r[1] for r in db.query("SELECT structure_id, checked_at FROM known_structures WHERE status='ok'")}
    assert set(stamps) == {S_HIGH, S_WH}
    db.execute("UPDATE known_structures SET checked_at=checked_at - 1000000")  # long ago
    db.execute("UPDATE scout_characters SET checked_at=0")  # the assets are read again
    await scout.run()
    assert fake_app.state.fake.calls["structure"] == calls  # known ones are not asked again within 720 h
    after = {r[0]: r[1] for r in db.query("SELECT structure_id, checked_at FROM known_structures WHERE status='ok'")}
    assert after == {k: v - 1000000 for k, v in stamps.items()}
    old = time.time() - 800 * 3600  # past the refresh interval: read once more
    db.execute("UPDATE known_structures SET checked_at=?", (old,))
    services["esi"]._cache.clear()
    await scout.run()
    assert all(r[0] > old for r in db.query("SELECT checked_at FROM known_structures WHERE status='ok'"))
    settings.structures.structure_refresh_hours = 0  # 0 = never again
    db.execute("UPDATE known_structures SET checked_at=?", (old,))
    await scout.run()
    assert all(r[0] == old for r in db.query("SELECT checked_at FROM known_structures WHERE status='ok'"))


async def test_a_failed_read_saves_nothing_and_keeps_the_old_sightings(settings, services, world, monkeypatch):
    world.asset_page_size = 2
    await link(services, A, A)
    db = services["db"]
    sight(db, A, S_NULL)  # found by an earlier complete read
    real = services["esi"].iter_pages

    async def flaky(path, token, max_pages=20):
        async for chunk, pages in real(path, token, max_pages):
            yield chunk, pages
            raise EsiError(503, "boom")  # after the first page

    monkeypatch.setattr(services["esi"], "iter_pages", flaky)
    scout = scout_for(settings, services)
    stats = await scout.run()
    assert stats["errors"] == 1 and stats["characters_read"] == 0
    # page 1 held S_HIGH, but without the whole list its own items are unknown: nothing is saved, S_NULL stays
    assert [r[0] for r in db.query("SELECT structure_id FROM structure_sightings")] == [S_NULL]
    row = db.query("SELECT checked_at, error, filter_v FROM scout_characters WHERE character_id=?", (A,))[0]
    assert row["checked_at"] == 0 and "503" in row["error"]  # a passing trouble: read again on the next run
    assert row["filter_v"] == 1
    monkeypatch.undo()
    stats = await scout.run()
    assert stats["characters_read"] == 1
    assert {r[0] for r in db.query("SELECT structure_id FROM structure_sightings")} == {S_HIGH, S_WH, S_GONE}
    assert db.query("SELECT error, filter_v FROM scout_characters WHERE character_id=?", (A,))[0][:] == (None, 2)


async def test_a_complete_read_drops_structures_the_character_left(settings, services, world):
    await link(services, A, A)
    scout = scout_for(settings, services)
    await scout.run()
    db = services["db"]
    assert {r[0] for r in db.query("SELECT structure_id FROM structure_sightings")} == {S_HIGH, S_WH, S_GONE}
    world.assets[A] = [a for a in world.assets[A] if a["location_id"] != S_WH]  # the items left that structure
    db.execute("UPDATE scout_characters SET checked_at=0")
    await scout.run()
    assert {r[0] for r in db.query("SELECT structure_id FROM structure_sightings")} == {S_HIGH, S_GONE}


async def test_a_structure_stays_in_the_file_until_it_is_gone(settings, services, world):
    await link(services, A, A)
    scout = scout_for(settings, services)
    db = services["db"]
    await scout.run()
    assert scout.structures() and {r["structure_id"] for r in scout.structures()} == {S_HIGH, S_WH}
    # nobody sights it and nobody may read it: the row is kept while a public dataset knows it (a row without such a
    # match is an orphan and is purged once every character was read with the current filter)
    orphan, nobody = 1_060_000_000_001, 1_060_000_000_002
    now = time.time()
    for sid in (orphan, nobody):
        db.execute(
            "INSERT INTO known_structures(structure_id, status, first_seen, last_seen) VALUES(?,?,?,?)",
            (sid, "new", now, now),
        )
    db.execute(
        "INSERT INTO public_structure_history(structure_id, name, source) VALUES(?,?,?)", (orphan, "x", "EVE Ref")
    )
    # A loses docking access: the name, owner and system stay, flagged "no docking access"
    world.structures[S_HIGH]["acl"] = [B]
    db.execute("UPDATE known_structures SET checked_at=0")
    services["esi"]._cache.clear()
    await scout.run()
    row = {r["structure_id"]: r for r in scout.structures()}[S_HIGH]
    assert row["access"] is False and row["name"] == "Jita - Trade Hub"
    assert db.query("SELECT COUNT(*) FROM known_structures WHERE structure_id=?", (orphan,))[0][0] == 1
    assert db.query("SELECT COUNT(*) FROM known_structures WHERE structure_id=?", (nobody,))[0][0] == 0  # purged
    # the structure is unanchored or destroyed: ESI answers 404, it is kept as a hidden 'gone' row, not asked again
    del world.structures[S_HIGH]
    db.execute("UPDATE known_structures SET checked_at=0")
    db.execute("UPDATE structure_refusals SET refused_at=0")  # the refusal of A is old enough to ask A again
    services["esi"]._cache.clear()
    await scout.run()
    assert db.query("SELECT status FROM known_structures WHERE structure_id=?", (S_HIGH,))[0][0] == "gone"
    assert db.query("SELECT COUNT(*) FROM structure_refusals WHERE structure_id=?", (S_HIGH,))[0][0] == 0
    assert S_HIGH not in {r["structure_id"] for r in scout.structures()}


async def test_esi_helpers(services, fake_app):
    tokens = await mint(services["http"], A, scopes=ALL)
    with pytest.raises(EsiError) as exc:
        await services["esi"].get_paged(f"/characters/{B}/assets", tokens["access_token"])
    assert exc.value.status == 403  # token of another character
    fake_app.state.fake.corp_names[CORP] = "Citadel Owners Inc"
    names = await services["esi"].names_categories([CORP, 587])
    assert names == {CORP: ("Citadel Owners Inc", "corporation"), 587: ("Rifter", "inventory_type")}


def test_discovered_structures_join_the_war_lists(graph):
    idx = StructureIndex(graph, "http://unused", "ua")
    assert not idx.loaded
    idx.set_discovered({CORP: [Structure(9, "Jita - Private", 35832, JITA, CORP)]})
    assert idx.loaded and [s.structure_id for s in idx.index[CORP]] == [9]
    idx.set_public(
        {CORP: [Structure(9, "Jita - Private", 35832, JITA, CORP), Structure(8, "Jita - Pub", 1, JITA, CORP)]}
    )
    assert sorted(s.structure_id for s in idx.index[CORP]) == [8, 9]  # no duplicate of 9
    rev = idx.revision
    idx.set_discovered({})
    assert idx.revision == rev + 1 and sorted(s.structure_id for s in idx.index[CORP]) == [8, 9]


def test_optional_scopes_setting(monkeypatch, tmp_path):
    s = load_settings(tmp_path / "none.yaml")
    assert s.eve.optional_scopes == [*STRUCTURE_SCOPES, WAYPOINT_SCOPE, ANSIBLEX_SCOPE]
    required = len(s.eve.required_scopes)
    assert s.eve.requested_scopes[:required] == s.eve.required_scopes
    assert s.eve.requested_scopes[required:] == s.eve.optional_scopes
    monkeypatch.setenv("EVE_OPTIONAL_SCOPES", "none")
    assert load_settings(tmp_path / "none.yaml").eve.optional_scopes == []
    monkeypatch.setenv("EVE_OPTIONAL_SCOPES", "esi-assets.read_assets.v1, esi-universe.read_structures.v1")
    assert load_settings(tmp_path / "none.yaml").eve.optional_scopes == list(reversed(STRUCTURE_SCOPES))
    monkeypatch.setenv("EVEDM_STRUCTURES_ENABLED", "0")
    assert load_settings(tmp_path / "none.yaml").structures.enabled is False


# --------------------------------------------------------------------------- web
def demo(tmp_path, **kw) -> TestClient:
    s = Settings()
    s.data_dir, s.secret_key, s.demo, s.web.auth = tmp_path / "d", "k", True, "none"
    for k, v in kw.items():
        setattr(s.structures, k, v)
    return TestClient(create_app(s, WarsSettings(enabled=True)))


def test_structures_page_and_api_in_demo(tmp_path):
    with demo(tmp_path) as c:
        assert 'href="/structures"' in c.get("/").text
        assert "structures.js" in c.get("/structures").text
        body = c.get("/api/structures").json()
        assert body["matched"] == 7 and body["characters"] == 9
        spaces = {r["system"]["space"] for r in body["rows"]}
        assert spaces == {"high", "low", "null", "wormhole"}
        assert body["rows"][-1]["system"]["space"] == "wormhole" and body["rows"][-1]["jumps"] is None
        jumps = [r["jumps"] for r in body["rows"] if r["jumps"] is not None]
        assert jumps == sorted(jumps)
        get = lambda **p: c.get("/api/structures", params=p).json()  # noqa: E731
        assert {r["system"]["name"] for r in get(space="null")["rows"]} == {"1DQ1-A"}
        assert [r["name"] for r in get(q="forge")["rows"]] == ["Old Man Star - Star Forge"]
        assert {r["owner"] for r in get(at_war=1)["rows"]} == {
            "Crimson Ledger Syndicate",
            "Grey Tollkeepers",
            "Ashen Veil",
        }
        assert all(r["wars"] for r in get(at_war=1)["rows"])
        assert get(type="Keepstar")["matched"] == 1 and get(region="Heimatar")["matched"] == 2
        assert [r["system"]["name"] for r in get(sort="system")["rows"]][0] == "1DQ1-A"
        assert get(limit=2, offset=6)["rows"][0]["system"]["space"] == "wormhole"
        # discovered high-sec structures of corporations at war also show on the Wars page list
        names = {r["name"] for r in c.get("/api/wars/structures").json()["rows"]}
        assert {"Ledger Vault", "Tollgate Market"} <= names


def test_structures_off(tmp_path):
    with demo(tmp_path, enabled=False) as c:
        assert c.get("/structures").status_code == 404 and c.get("/api/structures").status_code == 404
        assert 'href="/structures"' not in c.get("/").text


def test_sign_in_asks_for_structure_scopes_and_reports_sso_errors(tmp_path):
    from test_accounts_web import as_user, settings

    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=False))) as c:
        r = c.get("/sso/login", follow_redirects=False)
        assert "esi-assets.read_assets.v1" in r.headers["location"]
        assert "esi-universe.read_structures.v1" in r.headers["location"]
        r = c.get("/sso/callback", params={"error": "invalid_scope", "state": "x"}, follow_redirects=False)
        assert r.headers["location"] == "/login?error=invalid_scope"
        assert "developers.eveonline.com" in c.get("/login?error=invalid_scope").text
        svc = c.app.state.svc
        svc.db.upsert_user(A, "Alice")
        svc.db.create_state("st-add", "add")
        r = as_user(c, A).get(
            "/sso/callback", params={"error": "access_denied", "state": "st-add"}, follow_redirects=False
        )
        assert r.headers["location"] == "/characters?error=access_denied"


def test_health_reports_the_scout(tmp_path):
    with demo(tmp_path) as c:
        assert c.get("/api/health").json()["structure_scout"] == {"running": False, "cycles": 0}


def test_bad_values_in_the_main_config_are_reported(monkeypatch, tmp_path, capsys):
    from evedm.cli import main
    from evedm.config import ConfigError

    monkeypatch.setenv("EVEDM_STRUCTURES_INTERVAL_MIN", "0.5")
    assert load_settings(tmp_path / "none.yaml").structures.interval_min == 0.5
    monkeypatch.setenv("EVEDM_PORT", "eighty")
    with pytest.raises(ConfigError, match="invalid setting"):
        load_settings(tmp_path / "none.yaml")
    monkeypatch.setenv("EVEDM_DATA_DIR", str(tmp_path))
    assert main(["--demo", "serve"]) == 2
    assert "invalid setting" in capsys.readouterr().err


# --------------------------------------------------------------------- one by one with the signed-in main
def seed_unresolved(db, ids: dict[int, str]) -> None:
    now = time.time()
    with db.tx() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO known_structures(structure_id, status, first_seen, last_seen, checked_at) "
            "VALUES(?,?,?,?,0)",
            [(sid, status, now, now) for sid, status in ids.items()],
        )


def sight(db, character_id: int, *structure_ids: int) -> None:
    now = time.time()
    with db.tx() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO structure_sightings(structure_id, character_id, seen_at) VALUES(?,?,?)",
            [(sid, character_id, now) for sid in structure_ids],
        )


def refusals(db) -> set[tuple[int, int]]:
    return {(r[0], r[1]) for r in db.query("SELECT structure_id, character_id FROM structure_refusals")}


def statuses(db) -> dict[int, str]:
    return {r[0]: r[1] for r in db.query("SELECT structure_id, status FROM known_structures")}


def watch(scout: StructureScout, monkeypatch) -> list[tuple[int, int]]:
    """(structure_id, character_id) of every structure read sent towards ESI, in order."""
    sent: list[tuple[int, int]] = []
    current: list[int] = []
    real_token, real_get = scout.chars.access_token, scout.esi.get

    async def token(character_id, owner_id=0):
        current.append(character_id)
        return await real_token(character_id, owner_id)

    async def get(path, tok=None):
        if path.startswith("/universe/structures/") and path != "/universe/structures/":
            sent.append((int(path.rsplit("/", 1)[1]), current[-1]))
        return await real_get(path, tok)

    monkeypatch.setattr(scout.chars, "access_token", token)
    monkeypatch.setattr(scout.esi, "get", get)
    return sent


def esi_window(scout: StructureScout, monkeypatch, sent: list, after: int, seconds: float = 41.0, on_sleep=None):
    """ESI's error window runs low after ``after`` structure reads and has room again ``seconds`` later. Time is a
    counter: the job's sleep advances it, nothing really waits. Reading while the window is low fails the test."""
    clock = {"now": 0.0, "until": 0.0}
    slept: list[float] = []
    real_get = scout.esi.get

    def blocked_for(reserve=5):
        return max(0.0, clock["until"] - clock["now"])

    async def guarded(path, token=None):
        assert blocked_for() == 0, "sent to ESI while the error window was low"
        try:
            return await real_get(path, token)
        finally:
            if len(sent) == after and clock["until"] == 0.0:
                clock["until"] = clock["now"] + seconds

    async def fake_sleep(delay, cancel):
        slept.append(delay)
        state = scout.resolve_state()
        assert state["running"] and state["paused_until"] is not None  # the page can show the pause
        if on_sleep is not None:
            on_sleep()
        else:
            clock["now"] += delay

    monkeypatch.setattr(scout.esi, "blocked_for", blocked_for)
    monkeypatch.setattr(scout.esi, "get", guarded)
    scout._sleep = fake_sleep
    return slept


async def test_main_resolves_unresolved_structures_one_by_one(settings, services, world, monkeypatch):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])  # the main: may read structures, no assets
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "no_access", S_PRIVATE: "no_access", S_WH: "new", S_GONE: "new"})
    scout = scout_for(settings, services)
    sent = watch(scout, monkeypatch)
    result = await scout.run_resolve(C)
    assert result["targets"] == 4 and result["public_first"] == 1 and result["stopped"] == "done"
    assert (result["resolved"], result["no_access"], result["gone"], result["skipped"]) == (2, 1, 1, 0)
    assert (result["done"], result["remaining"], result["errors"], result["running"]) == (4, 0, 0, False)
    assert result["started_by"] == C and result["paused_until"] is None
    assert result["finished_at"] >= result["started_at"]
    # the public structure is asked before the others; nobody but the main can be asked here
    assert sent[0] == (S_WH, C) and {c for _, c in sent} == {C}
    assert statuses(db) == {S_HIGH: "no_access", S_PRIVATE: "ok", S_WH: "ok", S_GONE: "gone"}
    rows = {r["structure_id"]: r for r in scout.structures()}
    assert rows[S_PRIVATE]["name"] == "Secret" and rows[S_WH]["name"] == "J-Home" and rows[S_WH]["owner_id"] == CORP
    assert {r["structure_id"] for r in scout.unresolved()} == {S_HIGH}  # still refused: stays listed by ID only
    assert refusals(db) == {(S_HIGH, C)}  # the 403 is remembered; 200 and 404 leave nothing behind
    assert scout.resolve_state() == result


async def test_a_sighter_is_asked_before_the_main_and_the_main_comes_last(settings, services, world, monkeypatch):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])  # the main
    await link(services, A, C)  # A is an alt of the main; it may dock at S_HIGH
    await link(services, B, B)  # B belongs to another main; it may dock at S_NULL
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "new", S_NULL: "new", S_PRIVATE: "new"})
    sight(db, A, S_HIGH, S_NULL)
    sight(db, B, S_HIGH, S_NULL, S_PRIVATE)
    scout = scout_for(settings, services)
    sent = watch(scout, monkeypatch)
    result = await scout.run_resolve(C)
    # S_HIGH: A (the main's own) answers, B and the main are never asked. S_NULL: A is refused, then B (another main's)
    # answers. S_PRIVATE: B is refused, the main itself is the last resort and may dock there
    assert sent == [(S_HIGH, A), (S_NULL, A), (S_NULL, B), (S_PRIVATE, B), (S_PRIVATE, C)]
    assert (result["resolved"], result["no_access"], result["skipped"], result["stopped"]) == (3, 0, 0, "done")
    assert statuses(db) == {S_HIGH: "ok", S_NULL: "ok", S_PRIVATE: "ok"}
    assert refusals(db) == set()  # a structure that answered keeps no refusals


async def test_targets_go_public_first_then_never_asked_sighters_then_the_rest(settings, services, world, monkeypatch):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    await link(services, B, B)
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "new", S_NULL: "new", S_PRIVATE: "new", S_WH: "new"})
    for sid, checked in ((S_HIGH, 1), (S_PRIVATE, 2), (S_NULL, 3)):
        db.execute("UPDATE known_structures SET checked_at=? WHERE structure_id=?", (checked, sid))
    sight(db, B, S_NULL)
    scout = scout_for(settings, services)
    sent = watch(scout, monkeypatch)
    await scout.run_resolve(C)
    assert sent == [(S_WH, C), (S_NULL, B), (S_HIGH, C), (S_PRIVATE, C)]


async def test_a_refused_pair_is_not_asked_again_until_the_window_is_over(settings, services, world, fake_app):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "new"})  # the main may not dock there
    scout = scout_for(settings, services)
    first = await scout.run_resolve(C)
    assert first["no_access"] == 1 and fake_app.state.fake.calls["structure"] == 1
    assert refusals(db) == {(S_HIGH, C)} and statuses(db) == {S_HIGH: "no_access"}
    again = await scout.run_resolve(C)  # nobody left to ask: no request, status as it was
    assert (again["skipped"], again["no_access"], again["done"]) == (1, 0, 1)
    assert fake_app.state.fake.calls["structure"] == 1 and statuses(db) == {S_HIGH: "no_access"}
    inside = time.time() - 335 * 3600  # no_access_refresh_hours is 336
    db.execute("UPDATE structure_refusals SET refused_at=?", (inside,))
    assert (await scout.run_resolve(C))["skipped"] == 1 and fake_app.state.fake.calls["structure"] == 1
    db.execute("UPDATE structure_refusals SET refused_at=?", (time.time() - 337 * 3600,))
    third = await scout.run_resolve(C)  # the refusal is old: the pair is asked again, and the date is renewed
    assert (third["no_access"], third["skipped"]) == (1, 0) and fake_app.state.fake.calls["structure"] == 2
    assert db.query("SELECT refused_at FROM structure_refusals")[0][0] > inside


async def test_a_structure_nobody_can_be_asked_about_is_not_sent_to_esi(settings, services, world, fake_app):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "new", S_PRIVATE: "new"})
    now = time.time()
    for sid in (S_HIGH, S_PRIVATE):
        db.execute("INSERT INTO structure_refusals VALUES(?,?,?)", (sid, C, now))
    scout = scout_for(settings, services)
    result = await scout.run_resolve(C)
    assert fake_app.state.fake.calls.get("structure", 0) == 0  # not one read request
    assert (result["skipped"], result["resolved"], result["no_access"], result["gone"]) == (2, 0, 0, 0)
    assert (result["done"], result["remaining"], result["stopped"]) == (2, 0, "done")
    assert statuses(db) == {S_HIGH: "new", S_PRIVATE: "new"}  # statuses are left as they were
    assert scout.resolve_state()["skipped"] == 2


async def test_resolver_needs_the_main_with_the_structure_scope(settings, services, world):
    scout = scout_for(settings, services)
    with pytest.raises(ResolveError) as exc:
        await scout.run_resolve(C)
    assert exc.value.code == "no_character"
    await link(services, C, C, scopes=ALL_SCOPES)  # no read_structures scope
    with pytest.raises(ResolveError) as exc:
        scout.start_resolve(C)
    assert exc.value.code == "no_scope"
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    services["db"].set_status(C, REAUTH, C)
    with pytest.raises(ResolveError) as exc:
        await scout.run_resolve(C)
    assert exc.value.code == "reauth"
    assert scout.resolve_state()["running"] is False and scout.resolve_state()["started_at"] is None  # no job started


async def test_the_job_waits_for_the_error_window_and_goes_on(settings, services, world, monkeypatch):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    seed_unresolved(services["db"], {S_HIGH: "new", S_NULL: "new", S_PRIVATE: "new"})
    scout = scout_for(settings, services)
    sent = watch(scout, monkeypatch)
    slept = esi_window(scout, monkeypatch, sent, after=1)  # low after the first refusal, 41 s to the next window
    result = await scout.run_resolve(C)
    assert slept == [42.0]  # the 41 s of the window plus one second, once
    assert len(sent) == 3  # nothing was sent while the window was low (the guard fails the test otherwise)
    assert (result["resolved"], result["no_access"], result["done"], result["stopped"]) == (1, 2, 3, "done")
    assert result["paused_until"] is None


async def test_a_window_that_closes_between_two_characters_of_one_structure(settings, services, world, monkeypatch):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    await link(services, A, C)
    await link(services, B, B)
    db = services["db"]
    seed_unresolved(db, {S_NULL: "new"})
    sight(db, A, S_NULL)
    sight(db, B, S_NULL)
    scout = scout_for(settings, services)
    sent = watch(scout, monkeypatch)
    slept = esi_window(scout, monkeypatch, sent, after=1)
    result = await scout.run_resolve(C)
    assert sent == [(S_NULL, A), (S_NULL, B)] and slept == [42.0]  # A refused, wait, then B answers
    assert (result["resolved"], result["no_access"]) == (1, 0)


async def test_cancel_ends_the_job_and_nothing_more_is_sent(settings, services, world, monkeypatch):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "new", S_NULL: "new", S_PRIVATE: "new"})
    scout = scout_for(settings, services)
    sent = watch(scout, monkeypatch)
    esi_window(scout, monkeypatch, sent, after=1, on_sleep=lambda: scout.stop_resolve())  # cancelled during the pause
    result = await scout.run_resolve(C)
    assert result["stopped"] == "cancelled" and result["running"] is False and result["paused_until"] is None
    assert len(sent) == 1 and (result["done"], result["remaining"]) == (1, 2)
    assert statuses(db) == {S_HIGH: "no_access", S_NULL: "new", S_PRIVATE: "new"}


async def test_start_runs_in_the_background_refuses_a_second_job_and_can_be_stopped(
    settings, services, world, monkeypatch
):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    seed_unresolved(services["db"], {S_HIGH: "new", S_NULL: "new"})
    scout = scout_for(settings, services)
    monkeypatch.setattr(scout.esi, "blocked_for", lambda reserve=5: 30.0)  # waiting for ESI: the job stays alive
    state = scout.start_resolve(C)
    assert state["running"] is True and state["started_by"] == C
    with pytest.raises(ResolveError) as exc:
        scout.start_resolve(C)
    assert exc.value.code == "busy"
    for _ in range(100):  # the job reaches its pause
        await asyncio.sleep(0.02)
        if scout.resolve_state()["paused_until"]:
            break
    assert scout.resolve_state()["running"] and scout.resolve_state()["paused_until"] is not None
    assert scout.stop_resolve()["running"] is True  # the stop is a request: the job ends at its next step
    await scout._job_task
    final = scout.resolve_state()
    assert final["running"] is False and final["stopped"] == "cancelled" and final["remaining"] == 2
    assert scout.start_resolve(C)["running"] is True  # a new job may start now
    await scout.stop()  # shutdown cancels it and waits for it
    assert scout.resolve_state()["running"] is False and scout.resolve_state()["stopped"] == "cancelled"


async def test_three_failures_in_a_row_stop_the_job_and_mark_nothing(settings, services, world):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    db = services["db"]
    ids = [S_HIGH, S_NULL, S_WH, S_PRIVATE, S_GONE]
    seed_unresolved(db, {sid: "new" for sid in ids})
    scout = scout_for(settings, services)
    real_get = scout.esi.get
    failing = {"left": 3}

    async def flaky(path, token=None):
        if path != "/universe/structures/" and failing["left"] > 0:
            failing["left"] -= 1
            raise EsiError(503, "unavailable")
        return await real_get(path, token)

    scout.esi.get = flaky
    result = await scout.run_resolve(C)
    assert result["stopped"] == "esi_error" and result["errors"] == 3 and result["done"] == 0
    assert result["remaining"] == 5 and result["resolved"] == 0
    assert statuses(db) == {sid: "new" for sid in ids}
    assert [r[0] for r in db.query("SELECT checked_at FROM known_structures")] == [0.0] * 5  # nothing marked checked
    assert refusals(db) == set()
    # two failures are not enough: the job goes on, and the two rows stay for the next run
    failing["left"] = 2
    result = await scout.run_resolve(C)
    assert result["stopped"] == "done" and result["errors"] == 2 and result["remaining"] == 2
    assert result["done"] == 3


async def test_a_token_that_is_not_accepted_is_skipped_for_the_rest_of_the_job(settings, services, world, monkeypatch):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    await link(services, A, C)
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "new", S_PRIVATE: "new"})
    sight(db, A, S_HIGH, S_PRIVATE)
    scout = scout_for(settings, services)
    real = scout.chars.access_token
    tried: list[int] = []

    async def token(character_id, owner_id=0):
        tried.append(character_id)
        if character_id == A:
            raise SsoError("invalid_grant")
        return await real(character_id, owner_id)

    monkeypatch.setattr(scout.chars, "access_token", token)
    result = await scout.run_resolve(C)
    assert tried == [A, C, C]  # A is given up at the first failure; the main takes over without stopping the job
    assert result["stopped"] == "done" and result["no_access"] == 1 and result["resolved"] == 1  # S_HIGH: refused to C
    assert statuses(db) == {S_HIGH: "no_access", S_PRIVATE: "ok"}


def test_resolve_endpoint_in_demo_is_refused(tmp_path):
    with demo(tmp_path) as c:
        assert c.post("/api/structures/resolve").status_code == 403  # the CSRF header is required
        reply = c.post("/api/structures/resolve", headers={"X-EVEDM": "1"})
        assert reply.status_code in (409, 422) and reply.json()["error"] in ("demo", "no_character")
        idle = c.get("/api/structures/resolve").json()
        assert idle["running"] is False and idle["started_at"] is None and idle["stopped"] is None
        assert c.post("/api/structures/resolve/stop").status_code == 403  # CSRF here as well
        assert c.post("/api/structures/resolve/stop", headers={"X-EVEDM": "1"}).json()["running"] is False
        assert c.get("/api/structures/stats").json()["unknown"] == 0


MEMBER, OUTSIDER, NO_CHARACTER = 2113000001, 2113000002, 2113000003


def job_app(tmp_path, monkeypatch):
    """A real (not demo) app with three mains: a member who may read structures, a member without any character and a
    main outside the alliance. The hourly loop and the dataset downloads are switched off."""
    from test_accounts_web import link as link_character
    from test_accounts_web import settings as web_settings

    monkeypatch.setattr(StructureScout, "start", lambda self: None)
    s = web_settings(tmp_path)
    app = create_app(s, WarsSettings(enabled=False))
    return app, link_character


def test_resolve_job_endpoints(tmp_path, monkeypatch):
    from test_accounts_web import as_user

    app, link_character = job_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        svc = c.app.state.svc
        mains = ((MEMBER, "Member", 99010320), (NO_CHARACTER, "Bare", 99010320), (OUTSIDER, "Out", 5))
        for uid, name, alliance in mains:
            svc.db.upsert_user(uid, name)
            svc.db.set_affiliation(uid, 1, alliance)  # fresh: the gate reads no ESI
        link_character(svc, MEMBER, MEMBER, "Member", 30000142)
        svc.db.upsert_character(MEMBER, "Member", "OK", [STRUCTURE_SCOPES[0]], owner_id=MEMBER)
        seed_unresolved(svc.db, {S_HIGH: "new", S_NULL: "new"})
        scout = svc.scout
        zero = {"EVE Ref": 0, "Local index": 0, "Adam4EVE": 0}

        async def no_datasets(force=False):
            return zero

        async def public_list(path, token=None):
            return []

        scout._refresh_directory = no_datasets
        scout.esi.get = public_list
        scout.esi.blocked_for = lambda reserve=5: 30.0  # ESI's window is low: the job waits and stays running
        post = lambda path, who=MEMBER: as_user(c, who).post(path, headers=H)  # noqa: E731
        state = lambda who=MEMBER: as_user(c, who).get("/api/structures/resolve")  # noqa: E731

        # outside the alliance every structures endpoint is 404; signed out it is 401; the CSRF header is needed
        assert state(OUTSIDER).status_code == 404 and post("/api/structures/resolve", OUTSIDER).status_code == 404
        assert post("/api/structures/resolve/stop", OUTSIDER).status_code == 404
        assert as_user(c, OUTSIDER).get("/api/structures/stats").status_code == 404
        assert as_user(c, None).get("/api/structures/resolve").status_code == 401
        assert as_user(c, MEMBER).post("/api/structures/resolve").status_code == 403
        assert as_user(c, MEMBER).post("/api/structures/resolve/stop").status_code == 403
        assert post("/api/structures/resolve", NO_CHARACTER).json()["error"] == "no_character"
        assert post("/api/structures/resolve", NO_CHARACTER).status_code == 422
        idle = state().json()
        assert idle["running"] is False and idle["started_at"] is None and idle["history"] == {}

        started = post("/api/structures/resolve")
        assert started.status_code == 202 and started.json()["running"] is True
        assert started.json()["started_by"] == MEMBER and started.json()["stopped"] is None
        busy = post("/api/structures/resolve")
        assert busy.status_code == 409 and busy.json()["error"] == "busy" and busy.json()["running"] is True
        for _ in range(100):  # the job reaches its pause
            body = state().json()
            if body["paused_until"]:
                break
            time.sleep(0.02)
        assert body["running"] is True and body["targets"] == 2 and body["paused_until"] > time.time()
        assert body["remaining"] == 2 and set(body) >= {"done", "resolved", "no_access", "gone", "skipped", "errors"}
        assert as_user(c, OUTSIDER).get("/api/structures/resolve").status_code == 404  # members only, also to read
        stats = as_user(c, MEMBER).get("/api/structures/stats").json()
        assert stats["unknown"] == 2 and stats["readers"] == 1
        assert post("/api/structures/resolve/stop").status_code == 200
        for _ in range(100):
            body = state().json()
            if not body["running"]:
                break
            time.sleep(0.02)
        assert body["running"] is False and body["stopped"] == "cancelled" and body["remaining"] == 2
        assert body["finished_at"] is not None and body["paused_until"] is None
        assert post("/api/structures/resolve/stop").json()["stopped"] == "cancelled"  # nothing running: just the state
        again = post("/api/structures/resolve")  # a new job may start; it is still running when the app shuts down
        assert again.status_code == 202
    # leaving the app cancelled that job and waited for it
    assert scout._job.running is False and scout._job.stopped == "cancelled"


async def test_the_hourly_step_leaves_the_reads_to_a_running_job(settings, services, world, fake_app):
    await link(services, A, A)
    scout = scout_for(settings, services)
    await scout.run()  # reads A's structures; then something new is sighted
    seed_unresolved(services["db"], {S_NULL: "new"})
    sight(services["db"], A, S_NULL)
    calls = fake_app.state.fake.calls["structure"]
    stats = {"resolved": 0, "no_access": 0, "gone": 0, "errors": 0}
    async with scout._main_lock:  # a job holds the reads
        await scout._resolve(stats)
    assert fake_app.state.fake.calls["structure"] == calls and statuses(services["db"])[S_NULL] == "new"
    await scout._resolve(stats)  # no job: the structure is asked about (A is refused at S_NULL)
    assert fake_app.state.fake.calls["structure"] == calls + 1 and refusals(services["db"]) >= {(S_NULL, A)}


async def test_the_hourly_step_records_refusals_and_does_not_repeat_them(settings, services, world, fake_app):
    await link(services, B, B)  # B may not dock at S_HIGH or S_PRIVATE
    scout = scout_for(settings, services)
    await scout.run()
    db = services["db"]
    assert refusals(db) == {(S_HIGH, B), (S_PRIVATE, B)}
    calls = fake_app.state.fake.calls["structure"]
    db.execute("UPDATE known_structures SET checked_at=0 WHERE status='no_access'")  # due by the long interval
    await scout.run()
    assert fake_app.state.fake.calls["structure"] == calls  # but B was refused there, and not long ago
    db.execute("UPDATE structure_refusals SET refused_at=?", (time.time() - 400 * 3600,))
    db.execute("UPDATE known_structures SET checked_at=0 WHERE status='no_access'")
    await scout.run()
    assert fake_app.state.fake.calls["structure"] == calls + 2  # the refusals are old: both pairs asked again
    # a character that may dock reads S_HIGH: the 200 clears what refused it
    await link(services, A, A)
    db.execute("UPDATE known_structures SET checked_at=0 WHERE status='no_access'")
    await scout.run()
    assert statuses(db)[S_HIGH] == "ok" and S_HIGH not in {sid for sid, _ in refusals(db)}


async def test_refusals_of_unlinked_characters_are_pruned_but_a_main_without_assets_keeps_them(
    settings, services, world
):
    await link(services, B, B)
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])  # no assets scope: not one of the scout's contributors
    db = services["db"]
    scout = scout_for(settings, services)
    await scout.run()  # S_HIGH is refused to B and then, as an unidentified one, to the other reader C
    assert (S_HIGH, B) in refusals(db) and (S_HIGH, C) in refusals(db)
    await scout.run()
    assert (S_HIGH, C) in refusals(db) and (S_HIGH, B) in refusals(db)  # still linked: kept
    await services["chars"].remove(B, B)
    await scout.run()
    assert {c for _, c in refusals(db)} == {C}  # B is gone, and so are its refusals


async def test_stats_count_what_the_resolver_has_to_work_with(settings, services, world):
    for cid in (A, B):
        await link(services, cid, cid)
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "new", S_NULL: "no_access", S_PRIVATE: "no_access", S_WH: "ok", S_GONE: "gone"})
    sight(db, A, S_HIGH)
    sight(db, B, S_HIGH, S_NULL)
    sight(db, C, S_WH)
    sight(db, 2100000099, S_WH)  # not a registered character: holds no scope
    now = time.time()
    db.execute(  # a structure that is fully known: not wanted by the resolver
        "INSERT INTO known_structures(structure_id, name, owner_id, solar_system_id, status, first_seen, last_seen, "
        "checked_at) VALUES(?,?,?,?,?,?,?,?)",
        (S_NULL + 100, "Known", CORP, JITA, "ok", now, now, now),
    )
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO structure_refusals VALUES(?,?,?)",
            [
                (S_HIGH, A, now), (S_HIGH, B, now), (S_NULL, B, now),
                (S_PRIVATE, A, now), (S_PRIVATE, B, now), (S_PRIVATE, C, now),
                (S_WH, A, 0.0),  # old: no longer counts as recent
            ],
        )
        conn.executemany(
            "INSERT INTO public_structure_history(structure_id, name, source) VALUES(?,?,?)",
            [
                (S_PRIVATE, "p", "EVE Ref"),
                (S_HIGH, "h", "Adam4EVE"),
                (S_NULL, "n", None),
                (S_GONE, "g", "EVE Ref"),  # gone: not wanted
                (1, "z", "EVE Ref"),  # not a known structure
            ],
        )
    stats = scout_for(settings, services).stats()
    assert stats["by_status"] == {"new": 1, "no_access": 2, "ok": 2, "gone": 1}
    assert stats["readers"] == 3 and stats["unknown"] == 4  # the gone one is not wanted
    # S_HIGH: 2 sighters with the scope, S_NULL: 1, S_WH: 1 (the unregistered one does not count), S_PRIVATE: none
    assert stats["unknown_by_sighters"] == {"0": 1, "1": 2, "2+": 1}
    # askable: S_PRIVATE was refused to all three readers; the others still have a reader that was not refused
    assert (stats["askable"], stats["not_askable"]) == (3, 1)
    # S_HIGH and S_NULL had all their sighters refused; S_WH has C (never refused there)
    assert stats["askable_via_sighters"] == 1
    assert (stats["refusal_pairs"], stats["refusal_pairs_recent"]) == (7, 6)
    assert stats["public_history_matches"] == {"EVE Ref": 1, "Local index": 0, "Adam4EVE": 2}


def test_the_stats_command_reads_the_database_and_changes_nothing(tmp_path, capsys):
    from evedm.cli import main
    from evedm.db import Database

    data = tmp_path / "data"
    db = Database(data / "evedm.sqlite3")
    db.close()
    import sqlite3

    con = sqlite3.connect(data / "evedm.sqlite3")  # an old database: no refusals table yet
    con.execute("DROP TABLE structure_refusals")
    con.execute("PRAGMA user_version=13")
    con.commit()
    con.close()
    assert main(["--data-dir", str(data), "structures", "stats"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unknown"] == 0 and out["refusal_pairs"] == 0 and out["readers"] == 0
    con = sqlite3.connect(data / "evedm.sqlite3")
    assert con.execute("PRAGMA user_version").fetchone()[0] == 13  # not migrated by a report
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='structure_refusals'").fetchone()
    con.close()
    assert main(["--data-dir", str(tmp_path / "nowhere"), "structures", "stats"]) == 2


# ------------------------------------------------------------------------------------ public datasets
ADAM_CSV = "structureID;name;ownerID;ownerName;solarSystemID;typeID;typeName;lastSeen\n" + (
    f"{S_PRIVATE};Adam Name;{CORP};Adam Corp;{JITA};35825;Raitaru;2026-10-01 10:00:00\n"
    f"{S_HIGH};Adam High;{CORP};Adam Corp;{JITA};35832;Astrahus;2026-10-02 10:00:00\n"
)
EVEREF = {
    str(S_PRIVATE): {
        "structure_id": S_PRIVATE, "name": "Ref Name", "owner_id": CORP, "solar_system_id": DQ1, "type_id": 35834,
        "is_gettable_structure": True, "last_structure_get": "2026-11-09T04:00:00Z",
        "first_seen": "2025-01-01T00:00:00Z",
    },
    str(S_NULL): {"structure_id": S_NULL, "is_gettable_structure": False, "first_seen": "1970-01-01T00:00:00Z"},
    "999": {"name": "Not wanted", "owner_id": 1, "solar_system_id": JITA, "type_id": 35832},
}


def dataset_client(calls: list[str], everef=EVEREF, status: int = 200, original=None) -> httpx.AsyncClient:
    """Serves the two public datasets; every other host (the fake ESI) goes to the client that was there before."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data.everef.net":
            calls.append(request.url.host)
            return httpx.Response(status, content=json.dumps(everef).encode())
        if request.url.host == "static.adam4eve.eu":
            calls.append(request.url.host)
            return httpx.Response(200, content=ADAM_CSV.encode())
        passed = await original.request(request.method, request.url, headers=request.headers)
        return httpx.Response(passed.status_code, headers=passed.headers, content=passed.content)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def history(db) -> dict[int, dict]:
    return {r["structure_id"]: dict(r) for r in db.query("SELECT * FROM public_structure_history")}


async def test_eve_ref_outranks_adam4eve_and_skips_empty_records(settings, services, world):
    db = services["db"]
    seed_unresolved(db, {S_PRIVATE: "no_access", S_HIGH: "no_access", S_NULL: "no_access"})
    scout = scout_for(settings, services)
    calls: list[str] = []
    scout.esi.http = dataset_client(calls)
    found = await scout._refresh_directory()
    # S_NULL has no details in the scrape; id 999 is not wanted
    assert found == {"EVE Ref": 1, "Local index": 0, "Adam4EVE": 2}
    rows = history(db)
    assert set(rows) == {S_PRIVATE, S_HIGH}
    assert rows[S_PRIVATE]["source"] == "EVE Ref" and rows[S_PRIVATE]["name"] == "Ref Name"
    assert rows[S_PRIVATE]["system_id"] == DQ1 and rows[S_PRIVATE]["observed"] == "2026-11-09T04:00:00Z"
    assert rows[S_HIGH]["source"] == "Adam4EVE" and rows[S_HIGH]["name"] == "Adam High"
    # a later Adam4EVE run never overwrites the EVE Ref entry
    scout._directory_due = 0.0
    await scout._refresh_directory()
    assert history(db)[S_PRIVATE]["name"] == "Ref Name"
    # the listing says where the details come from
    by_id = {r["structure_id"]: r for r in scout.listing({}, [], source="all")["rows"]}  # seeded without sightings
    assert by_id[S_PRIVATE]["historical_source"] == "EVE Ref" and by_id[S_PRIVATE]["name"] == "Ref Name"
    assert by_id[S_HIGH]["historical_source"] == "Adam4EVE"
    # both datasets are asked once a day, not on every run
    calls.clear()
    await scout._refresh_directory()
    assert calls == []


async def test_eve_ref_failures_are_retried_later_and_can_be_switched_off(settings, services, world):
    db = services["db"]
    seed_unresolved(db, {S_PRIVATE: "no_access"})
    scout = scout_for(settings, services)
    calls: list[str] = []
    scout.esi.http = dataset_client(calls, status=503)
    assert (await scout._refresh_directory())["EVE Ref"] == 0
    assert scout._everef_due > time.monotonic()  # retried in an hour
    assert history(db)[S_PRIVATE]["source"] == "Adam4EVE"  # the other source still filled the gap
    settings.structures.everef_enabled = False
    scout._everef_due = 0.0
    calls.clear()
    await scout._refresh_directory(force=True)
    assert "data.everef.net" not in calls


async def test_eve_ref_download_size_is_capped(settings, services, world):
    seed_unresolved(services["db"], {S_PRIVATE: "no_access"})
    settings.structures.everef_max_mb = 1
    big = {str(i): {"name": "x" * 100} for i in range(20000)}  # more than 1 MB of JSON
    scout = scout_for(settings, services)
    scout.esi.http = dataset_client([], everef=big)
    found = await scout._refresh_directory()
    assert found["EVE Ref"] == 0 and scout._everef_due > time.monotonic()


async def test_resolver_also_matches_the_public_datasets(settings, services, world):
    await link(services, B, B, scopes=[STRUCTURE_SCOPES[0]])  # B may not dock at S_PRIVATE: 403
    seed_unresolved(services["db"], {S_PRIVATE: "no_access"})
    scout = scout_for(settings, services)
    scout.esi.http = dataset_client([], original=scout.esi.http)
    first = await scout.run_resolve(B)
    assert first["no_access"] == 1 and first["history"] == {"EVE Ref": 1, "Local index": 0, "Adam4EVE": 1}
    again = await scout.run_resolve(B)  # forced downloads are limited to one per 10 minutes
    assert again["history"] == {"EVE Ref": 0, "Local index": 0, "Adam4EVE": 0}
    assert again["skipped"] == 1  # B was refused a moment ago: nobody left to ask


async def test_the_local_index_fills_unresolved_structures_and_ranks_between_the_datasets(settings, services, world):
    db = services["db"]
    seed_unresolved(db, {S_PRIVATE: "no_access", S_HIGH: "no_access"})
    index = settings.data_dir / "structure-index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"structure_id": S_HIGH, "name": "Jita - Local High", "owner_id": CORP, "owner_name": "Local Corp",
         "solar_system_id": JITA, "type_id": 35832, "type_name": "Astrahus", "observed": "2026-08-01T00:00:00Z"},
        {"structure_id": S_PRIVATE, "name": "Jita - Local Private", "owner_id": CORP, "owner_name": "Local Corp",
         "solar_system_id": JITA, "type_id": 35825, "type_name": "Raitaru", "observed": "2026-08-02T00:00:00Z"},
        {"structure_id": S_NULL, "name": "Not wanted", "owner_id": CORP, "solar_system_id": DQ1},
    ]
    index.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")
    scout = scout_for(settings, services)
    scout.esi.http = dataset_client([], original=scout.esi.http)
    found = await scout._refresh_directory()
    assert found["Local index"] == 2 and found["EVE Ref"] == 1  # EVE Ref knows S_PRIVATE as well
    rows_in_db = history(db)
    assert set(rows_in_db) == {S_HIGH, S_PRIVATE}
    assert rows_in_db[S_PRIVATE]["source"] == "EVE Ref" and rows_in_db[S_PRIVATE]["name"] == "Ref Name"  # outranks it
    assert rows_in_db[S_HIGH]["source"] == "Local index" and rows_in_db[S_HIGH]["name"] == "Jita - Local High"
    assert rows_in_db[S_HIGH]["type_name"] == "Astrahus" and rows_in_db[S_HIGH]["observed"] == "2026-08-01T00:00:00Z"
    # the local list outranks Adam4EVE: a later Adam4EVE run does not replace it
    scout._directory_due = 0.0
    await scout._refresh_directory()
    assert history(db)[S_HIGH]["name"] == "Jita - Local High"
    by_id = {r["structure_id"]: r for r in scout.listing({}, [], source="all")["rows"]}  # seeded without sightings
    assert by_id[S_HIGH]["historical_source"] == "Local index" and by_id[S_HIGH]["name"] == "Jita - Local High"


async def test_a_missing_or_broken_local_index_changes_nothing(settings, services, world):
    seed_unresolved(services["db"], {S_PRIVATE: "no_access"})
    settings.structures.everef_enabled = False
    scout = scout_for(settings, services)
    scout.esi.http = dataset_client([], original=scout.esi.http)
    assert (await scout._refresh_directory(force=True))["Local index"] == 0  # no file: nothing, no error
    (settings.data_dir / "structure-index.jsonl").write_bytes(b"\xff\xfe not utf-8 \xff")
    scout._directory_due = 0.0
    assert (await scout._refresh_directory(force=True))["Local index"] == 0  # unreadable: logged, run goes on
    settings.structures.index_file = str(settings.data_dir / "elsewhere.jsonl")
    assert scout._fetch_local({S_PRIVATE}) == []


# --------------------------------------------------------------------------------------- probe (diagnostics)
P1, P2, P3, P4, P5 = (1_036_000_000_000 + n for n in range(1, 6))  # structure IDs for the probe tests
SHIP, CARGO = 1_000_000_000_101, 1_000_000_000_102  # a character's own items (ship, container) with big IDs


def probe_row(item: int, location: int, ltype: str = "other", flag: str = "Hangar", type_id: int = 587) -> dict:
    return {"item_id": item, "location_id": location, "location_type": ltype, "location_flag": flag, "type_id": type_id}


def probe_db(db) -> None:
    now = time.time()
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO known_structures(structure_id, status, first_seen, last_seen, checked_at) VALUES(?,?,?,?,0)",
            [(P1, "ok", now, now), (P2, "no_access", now, now), (P3, "new", now, now), (P4, "gone", now, now)],
        )  # P5 is not stored
    db.save_type_names({28352: "Rorqual", 3294: "Container"})


def probe_assets_of_two(db):
    from evedm.probe import probe_assets

    probe_db(db)
    a = [
        probe_row(1_000_000_000_001, 60003760, "station"),  # an NPC station: below 1e12, never counted
        probe_row(SHIP, P1, type_id=28352),  # the character's ship, in structure P1
        probe_row(1_000_000_000_103, P1),  # a second row at P1
        probe_row(1_000_000_000_104, P2),
        probe_row(1_000_000_000_105, P3, flag="AutoFit"),
        probe_row(1_000_000_000_106, P4, flag="CorpDeliveries"),
        probe_row(1_000_000_000_107, P5, flag="OfficeFolder"),
        probe_row(CARGO, SHIP, "item", "Cargo", 3294),  # inside the ship: a location that is the character's own item
        probe_row(1_000_000_000_108, SHIP, "item", "DroneBay"),
        probe_row(1_000_000_000_109, CARGO, "item", "Unlocked"),
    ]
    b = [probe_row(2_000_000_000_001, P1), probe_row(2_000_000_000_002, P2), probe_row(2_000_000_000_003, P2)]
    return probe_assets({A: a, B: b}, db, samples=1)


def bucket_of(buckets: list[dict], ltype: str, flag: str) -> dict:
    return next(b for b in buckets if b["location_type"] == ltype and b["location_flag"] == flag)


def test_probe_groups_locations_by_type_and_flag_with_database_status(db):
    report = probe_assets_of_two(db)
    mine = report["characters"][str(A)]
    assert mine["assets"] == 10 and mine["assets_at_1e12_or_more"] == 9  # the station row is left out
    hangar = bucket_of(mine["buckets"], "other", "Hangar")
    assert (hangar["rows"], hangar["locations"], hangar["own_item"]) == (2 + 1, 2, 0)
    assert hangar["db"] == {"ok": 1, "no_access": 1, "new": 0, "gone": 0, "not_in_db": 0}
    assert {k: len(v) for k, v in hangar["samples"].items()} == {"ok": 1, "no_access": 1}  # samples=1
    assert bucket_of(mine["buckets"], "other", "AutoFit")["db"]["new"] == 1
    assert bucket_of(mine["buckets"], "other", "CorpDeliveries")["db"]["gone"] == 1
    assert bucket_of(mine["buckets"], "other", "OfficeFolder")["db"]["not_in_db"] == 1
    assert report["characters"][str(B)]["assets_at_1e12_or_more"] == 3


def test_probe_counts_locations_that_are_the_characters_own_items(db):
    mine = probe_assets_of_two(db)["characters"][str(A)]["buckets"]
    cargo, drone = bucket_of(mine, "item", "Cargo"), bucket_of(mine, "item", "DroneBay")
    assert (cargo["locations"], cargo["own_item"]) == (1, 1) and cargo["db"]["not_in_db"] == 1
    assert cargo["own_item_parent_types"] == [{"type_id": 28352, "name": "Rorqual", "count": 1}]
    assert drone["own_item_parent_types"][0]["name"] == "Rorqual"
    # a container inside the ship holds an item: its parent type is the container, named from type_names
    assert bucket_of(mine, "item", "Unlocked")["own_item_parent_types"] == [
        {"type_id": 3294, "name": "Container", "count": 1}
    ]
    assert bucket_of(mine, "other", "Hangar")["own_item_parent_types"] == []


def test_probe_total_counts_each_location_once_and_the_verdict_reads_the_other_flags(db):
    report = probe_assets_of_two(db)
    total = report["total"]
    assert total["characters"] == 2 and total["assets"] == 13 and total["assets_at_1e12_or_more"] == 12
    hangar = bucket_of(total["buckets"], "other", "Hangar")
    assert (hangar["rows"], hangar["locations"]) == (3 + 3, 2)  # P1 and P2 once, although both characters hold assets
    assert hangar["db"]["ok"] == 1 and hangar["db"]["no_access"] == 1
    flags = report["verdict"]["location_type_other_by_flag"]
    assert set(flags) == {"Hangar", "AutoFit", "CorpDeliveries", "OfficeFolder"}  # only location_type "other"
    assert flags["Hangar"] == {
        "distinct_ids": 2,
        "ok": 1,
        "no_access": 1,
        "ok_share": 0.5,
        "no_access_share": 0.5,
        "new_gone_or_not_in_db": 0,
        "own_item": 0,
        "reading": "mostly_resolved",
    }
    assert flags["AutoFit"]["reading"] == "mixed" and flags["AutoFit"]["new_gone_or_not_in_db"] == 1


def test_probe_picks_the_characters_with_the_most_sightings_or_the_ones_asked_for(db):
    from evedm.probe import pick_characters

    sight(db, A, P1)
    sight(db, B, P1, P2)
    readers = [(A, A, []), (B, B, []), (C, C, [])]
    assert pick_characters(db, readers, 2) == ([(B, B), (A, A)], [])  # most sightings first
    assert pick_characters(db, readers, 1)[0] == [(B, B)]
    picked, skipped = pick_characters(db, readers, 1, [C, 5, C])
    assert picked == [(C, C)] and skipped == [{"character_id": 5, "reason": "not registered with the assets scope"}]


async def test_probe_stops_cleanly_while_esi_is_error_limited(settings, services, world, fake_app, monkeypatch):
    from evedm.probe import probe_characters

    await link(services, A, A)
    await link(services, B, B)
    esi = services["esi"]
    monkeypatch.setattr(esi, "blocked_for", lambda reserve=5: 17.0)
    report = await probe_characters(services["db"], services["chars"], esi, [(A, A), (B, B)])
    assert report["run"]["stopped"] == {"reason": "blocked", "retry_in_s": 17, "before_character": A}
    assert fake_app.state.fake.calls.get("assets", 0) == 0  # nothing was sent
    assert report["total"]["characters"] == 0 and report["verdict"] == {
        "location_type_other_by_flag": {},
        "candidates": {"distinct_ids": 0, "db": {"ok": 0, "no_access": 0, "new": 0, "gone": 0, "not_in_db": 0}},
    }


def test_probe_command_reads_asset_pages_and_writes_nothing(tmp_path, settings, fake_app, monkeypatch, capsys):
    from common import FAKE
    from evedm import cli
    from evedm.characters import CharacterService
    from evedm.crypto import TokenCipher
    from evedm.db import Database
    from evedm.sso import AuthService

    fake = fake_app.state.fake
    fake.asset_page_size = 2  # three pages for A: the probe walks all of them
    fake.assets[A] = [
        asset(1, S_HIGH, "other"),
        asset(2, S_HIGH, "other", "AutoFit"),
        asset(3, S_WH, "other"),
        asset(4, 1, flag="Cargo"),
        asset(5, 60003760, "station"),
    ]
    fake.assets[B] = [asset(11, S_NULL, "other")]

    async def seed() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_app), base_url=FAKE) as http:
            db = Database(settings.db_path, settings.sqlite_journal)
            auth = AuthService(settings.eve, http)
            chars = CharacterService(db, TokenCipher(settings.secret_key), auth, settings)
            for cid in (A, B):
                tokens = await mint(http, cid, scopes=ALL)
                chars.store(await auth.refresh(tokens["refresh_token"]), owner_id=cid)
            now = time.time()
            with db.tx() as conn:
                conn.executemany(
                    "INSERT INTO known_structures(structure_id, status, first_seen, last_seen, checked_at) "
                    "VALUES(?,?,?,?,0)",
                    [(S_HIGH, "no_access", now, now), (S_WH, "ok", now, now), (S_NULL, "new", now, now)],
                )
            sight(db, A, S_HIGH, S_WH)
            sight(db, B, S_NULL)
            db.close()

    asyncio.run(seed())

    def tables() -> dict[str, list]:
        db = Database(settings.db_path, read_only=True)
        out = {
            t: [tuple(r) for r in db.query(f"SELECT * FROM {t} ORDER BY 1, 2")]  # noqa: S608
            for t in ("known_structures", "structure_sightings", "structure_refusals")
        }
        db.close()
        return out

    before = tables()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    fake_client = lambda **kw: real_client(transport=httpx.ASGITransport(app=fake_app), base_url=FAKE)  # noqa: E731
    monkeypatch.setattr("evedm.web.app.httpx.AsyncClient", fake_client)
    assert cli.main(["structures", "probe", "--characters", "1", "--samples", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"]["characters"] == [A] and out["run"]["stopped"] is None and out["run"]["skipped"] == []
    assert out["characters"][str(A)]["read"] == {"pages_read": 3, "pages_total": 3, "complete": True, "error": None}
    assert fake.calls["assets"] == 3 and fake.calls.get("structure", 0) == 0  # asset pages only
    flags = out["verdict"]["location_type_other_by_flag"]
    assert flags["Hangar"]["distinct_ids"] == 2 and flags["Hangar"]["ok"] == 1 and flags["Hangar"]["no_access"] == 1
    assert flags["AutoFit"]["no_access"] == 1 and flags["AutoFit"]["reading"] == "mostly_refused"
    assert tables() == before  # not one row of the structure tables changed
    # --character overrides the choice and says which requested ones cannot be read
    assert cli.main(["structures", "probe", "--character", str(B), "--character", "7", "--max-pages", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"]["characters"] == [B] and out["run"]["max_pages"] == 1
    assert out["run"]["skipped"] == [{"character_id": 7, "reason": "not registered with the assets scope"}]
    assert tables() == before
    text = json.dumps(out)
    assert "token" not in text and "Bravo" not in text  # no tokens, no character names


# ------------------------------------------------------------------------------------ asset filter and purge
T1, T2, T3, T4, T5 = (1_070_000_000_000 + n for n in range(1, 6))
SHIP1, SHIP2, SHIP3 = 1_080_000_000_001, 1_080_000_000_002, 1_080_000_000_003  # a character's own ships


async def test_only_hangar_deliveries_and_asset_safety_of_not_own_locations_are_structures(settings, services, world):
    world.assets[A] = [
        asset(31, T1, "item", "Hangar"),  # typed "item" or "other": the type is not used
        asset(32, T2, "other", "Deliveries"),
        asset(33, T3, "item", "AssetSafety"),
        asset(34, T4, "item", "MedSlot0"),  # fitted into a ship: the ID is the ship, never a structure
        asset(35, T5, "other", "Cargo"),
        asset(SHIP1, 60003760, "station"),
        asset(36, SHIP1, "item", "Hangar"),  # a flag Hangar, but the location is the character's own ship
        asset(37, SHIP1, "item", "Cargo"),
        asset(38, 60003760, "station"),
    ]
    await link(services, A, A)
    scout = scout_for(settings, services)
    stats = await scout.run()
    db = services["db"]
    assert {r[0] for r in db.query("SELECT structure_id FROM known_structures")} == {T1, T2, T3}
    assert {r[0] for r in db.query("SELECT structure_id FROM structure_sightings")} == {T1, T2, T3}
    assert stats["candidates"] == 3 and stats["characters_read"] == 1
    row = db.query("SELECT structures, filter_v FROM scout_characters WHERE character_id=?", (A,))[0]
    assert (row["structures"], row["filter_v"]) == (3, 2)  # the Characters page count is now the real number
    assert services["db"].structure_counts() == {A: 3}


async def test_the_scan_keeps_ids_not_rows():
    from evedm.scout import AssetScan, is_structure_candidate

    scan = AssetScan()
    scan.add([asset(SHIP1, 60003760, "station"), asset(1, SHIP1, "item", "Hangar")])
    scan.add([asset(2, T1, "item", "Hangar")])  # the own ship was on an earlier page; here it could be on a later one
    scan.add([asset(3, T2, "item", "Hangar"), asset(T2, 60003760, "station")])
    assert scan.items == {SHIP1, T2} and scan.candidates == {T1}
    assert is_structure_candidate(T1, "Deliveries", set()) and not is_structure_candidate(T1, "Cargo", set())
    assert not is_structure_candidate(T1, "Hangar", {T1}) and not is_structure_candidate(60003760, "Hangar", set())


async def test_own_items_stored_as_structures_are_purged_with_their_sightings_and_refusals(
    settings, services, world, caplog
):
    db = services["db"]
    await link(services, A, A)
    seed_unresolved(db, {SHIP1: "no_access", SHIP2: "gone", SHIP3: "ok", T1: "new"})
    db.execute("UPDATE known_structures SET checked_at=? WHERE structure_id=?", (time.time(), SHIP3))
    sight(db, A, SHIP1, SHIP3)
    sight(db, B, SHIP1)  # whoever sighted it: an own item is no structure for anybody
    db.execute("INSERT INTO structure_refusals VALUES(?,?,?)", (SHIP1, A, time.time()))
    world.assets[A] = [
        asset(SHIP1, 60003760, "station"),
        asset(SHIP2, 60003760, "station"),
        asset(SHIP3, 60003760, "station"),
        asset(41, T1, "item", "Hangar"),
    ]
    scout = scout_for(settings, services)
    with caplog.at_level("WARNING", logger="evedm.scout"):
        stats = await scout.run()
    assert stats["purged_own_items"] == 2 and stats["purged_orphans"] == 0
    assert {r[0]: r[1] for r in db.query("SELECT structure_id, status FROM known_structures")} == {
        SHIP3: "ok",  # read from ESI once: never purged this way, but warned about
        T1: "gone",  # the real candidate stays (ESI does not know this ID in the fake, so it is read as gone)
    }
    assert [r[0] for r in db.query("SELECT structure_id FROM structure_sightings")] == [T1]
    assert db.query("SELECT COUNT(*) FROM structure_refusals WHERE structure_id=?", (SHIP1,))[0][0] == 0
    assert any(str(SHIP3) in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)
    numbers = scout.stats()
    assert (numbers["purged_own_items"], numbers["purged_orphans"], numbers["filter_pending"]) == (2, 0, 0)


async def test_a_character_read_with_an_older_filter_is_read_again_and_orphans_wait_for_everybody(
    settings, services, world, fake_app, monkeypatch
):
    await link(services, A, A)
    await link(services, B, B)
    scout = scout_for(settings, services)
    await scout.run()
    db = services["db"]
    calls = fake_app.state.fake.calls["assets"]
    await scout.run()
    assert fake_app.state.fake.calls["assets"] == calls  # nothing due: current filter, fresh read
    now = time.time()
    with db.tx() as conn:
        # refused a moment ago, so the hourly step does not ask ESI about them meanwhile (a 404 would make them 'gone')
        for sid, status in ((T1, "no_access"), (T2, "no_access"), (T3, "ok"), (T4, "gone")):
            conn.execute(
                "INSERT INTO known_structures(structure_id, status, first_seen, last_seen, checked_at) "
                "VALUES(?,?,?,?,?)",
                (sid, status, now, now, now),
            )
        conn.execute("INSERT INTO structure_refusals VALUES(?,?,?)", (T2, A, now))
        conn.execute("UPDATE scout_characters SET filter_v=1")  # as after an upgrade from the old rule
    assert scout.stats()["filter_pending"] == 2
    real = services["esi"].iter_pages

    async def flaky(path, token, max_pages=20):
        if f"/characters/{B}/" in path:
            raise EsiError(503, "boom")
        async for chunk in real(path, token, max_pages):
            yield chunk

    monkeypatch.setattr(services["esi"], "iter_pages", flaky)
    stats = await scout.run()
    assert fake_app.state.fake.calls["assets"] == calls + 1  # A is read at once, B fails
    assert stats["purged_orphans"] == 0
    assert {T1, T2} <= {r[0] for r in db.query("SELECT structure_id FROM known_structures")}
    assert scout.stats()["filter_pending"] == 1  # B is not current: the orphans stay
    monkeypatch.undo()
    stats = await scout.run()
    assert fake_app.state.fake.calls["assets"] == calls + 2
    assert stats["purged_orphans"] == 2 and scout.stats()["filter_pending"] == 0
    left = {r[0] for r in db.query("SELECT structure_id FROM known_structures")}
    assert T1 not in left and T2 not in left and {T3, T4} <= left  # status ok and gone are never orphans
    assert db.query("SELECT COUNT(*) FROM structure_refusals WHERE structure_id=?", (T2,))[0][0] == 0
    assert scout.stats()["purged_orphans"] == 2


async def test_no_orphans_are_purged_without_a_contributor(settings, services, world):
    db = services["db"]
    seed_unresolved(db, {T1: "new"})
    scout = scout_for(settings, services)
    stats = await scout.run()  # nobody is registered: nothing proves the finds are orphans
    assert stats["purged_orphans"] == 0 and statuses(db) == {T1: "new"}


async def test_the_purge_waits_while_a_resolve_job_holds_the_lock(settings, services, world):
    db = services["db"]
    seed_unresolved(db, {SHIP1: "no_access"})
    scout = scout_for(settings, services)
    scout._own_hits = {SHIP1}
    stats = {"purged_own_items": 0, "purged_orphans": 0}
    async with scout._main_lock:  # what a running job holds
        scout._purge(stats, [])
    assert statuses(db) == {SHIP1: "no_access"} and scout._own_hits == {SHIP1}  # nothing deleted, nothing forgotten
    scout._purge(stats, [])
    assert statuses(db) == {} and stats["purged_own_items"] == 1 and scout._own_hits == set()


def test_probe_counts_the_candidates_the_scout_would_take(db):
    from evedm.probe import probe_assets

    probe_db(db)  # P1 ok, P2 no_access, P3 new, P4 gone; P5 not stored
    rows = [
        probe_row(1_000_000_000_201, P1, "item", "Hangar"),
        probe_row(1_000_000_000_202, P1, "other", "Hangar"),
        probe_row(1_000_000_000_203, P2, "item", "Deliveries"),
        probe_row(1_000_000_000_204, P2, "item", "Hangar"),  # P2 again, in another flag
        probe_row(1_000_000_000_205, P5, "item", "AssetSafety"),
        probe_row(1_000_000_000_206, P3, "item", "MedSlot0"),  # fitting: not a candidate
        probe_row(SHIP, P4, "item", "Hangar", 28352),
        probe_row(1_000_000_000_207, SHIP, "item", "Hangar"),  # a flag Hangar, but inside the character's own ship
    ]
    report = probe_assets({A: rows}, db)
    mine = report["characters"][str(A)]["buckets"]
    assert bucket_of(mine, "item", "Hangar")["candidates"] == 3 and bucket_of(mine, "item", "Hangar")["locations"] == 4
    assert bucket_of(mine, "other", "Hangar")["candidates"] == 1
    assert bucket_of(mine, "item", "Deliveries")["candidates"] == 1
    assert bucket_of(mine, "item", "AssetSafety")["candidates"] == 1
    assert bucket_of(mine, "item", "MedSlot0")["candidates"] == 0
    cands = report["verdict"]["candidates"]
    # P1, P2, P4 (in Hangar) and P5; the own ship is no candidate. P2 counts once although it is in two flags
    assert cands == {"distinct_ids": 4, "db": {"ok": 1, "no_access": 1, "new": 0, "gone": 1, "not_in_db": 1}}


# ------------------------------------------------------------------------------------ dock search (character search)
DOCK = ALL + [ANSIBLEX_SCOPE]  # both the structures scopes and esi-search.search_structures.v1
D1, D2, D3 = (1_095_000_000_000 + n for n in range(1, 4))


@pytest.fixture
def dock_world(world):
    """A may dock at S_HIGH, D1 and D2; B at S_NULL and D2; C at D2 only (S_WH, S_PRIVATE: no " - " in the name)."""
    world.structures.update(
        {
            D1: {"name": "Perimeter - Dock Only", "owner_id": CORP, "solar_system_id": 30000144, "type_id": 35832,
                 "acl": [A]},
            D2: {"name": "Jita - Open House", "owner_id": CORP, "solar_system_id": JITA, "type_id": 35825, "acl": None},
        }
    )
    return world


def fresh_stats():
    from collections import defaultdict

    return defaultdict(int)


def docks(db) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    for r in db.query("SELECT structure_id, character_id FROM structure_access"):
        out.setdefault(r[1], set()).add(r[0])
    return out


def dock_state(db, character_id: int):
    rows = db.query("SELECT * FROM scout_dock_search WHERE character_id=?", (character_id,))
    return dict(rows[0]) if rows else None


def due_again(db) -> None:
    db.execute("UPDATE scout_dock_search SET checked_at=1 WHERE checked_at > 1")


def fail_search(scout, monkeypatch, exc: Exception) -> None:
    real = scout.esi.get

    async def get(path, token=None):
        if "/search/" in path:
            raise exc
        return await real(path, token)

    monkeypatch.setattr(scout.esi, "get", get)


async def test_the_dock_search_stores_access_rows_and_creates_known_rows(settings, services, dock_world):
    dock_world.structures[60_003_760] = {"name": "Jita - Station", "acl": None}  # below 1e12: never a structure here
    await link(services, A, A, DOCK)
    scout = scout_for(settings, services)
    stats = fresh_stats()
    await scout._dock_search(stats)
    db = services["db"]
    assert docks(db) == {A: {S_HIGH, D1, D2}}
    assert statuses(db) == {S_HIGH: "new", D1: "new", D2: "new"}  # new rows, exactly like sightings
    state = dock_state(db, A)
    assert (state["found"], state["capped"], state["cursor"], state["error"]) == (4, 0, 0, None)
    assert state["checked_at"] > 0  # the raw result had 4 hits, one of them no structure ID
    assert stats["dock_searched"] == 1 and stats["dock_found"] == 3 and stats["dock_capped"] == 0
    assert dock_world.search_log == [(A, " - ")]
    await scout._dock_search(stats)  # not due for 24 hours
    assert dock_world.search_log == [(A, " - ")]


async def test_the_dock_search_needs_both_scopes_and_can_be_switched_off(settings, services, dock_world):
    await link(services, A, A, DOCK)
    await link(services, B, B, ALL)  # structures scopes, no search scope
    await link(services, C, C, ALL_SCOPES + [ANSIBLEX_SCOPE])  # search scope, no structures scopes
    scout = scout_for(settings, services)
    assert [c for c, _, _ in scout.dock_searchers()] == [A]
    await scout._dock_search(fresh_stats())
    assert {c for c, _ in dock_world.search_log} == {A}
    settings.structures.dock_search_enabled = False
    dock_world.search_log.clear()
    due_again(services["db"])
    await scout._dock_search(fresh_stats())
    assert dock_world.search_log == []


async def test_a_complete_result_replaces_the_set_and_a_capped_one_only_adds(settings, services, dock_world):
    await link(services, A, A, DOCK)
    scout = scout_for(settings, services)
    db = services["db"]
    await scout._dock_search(fresh_stats())
    assert docks(db)[A] == {S_HIGH, D1, D2}
    dock_world.structures[D1]["acl"] = []  # A may no longer dock at D1
    due_again(db)
    await scout._dock_search(fresh_stats())
    assert docks(db)[A] == {S_HIGH, D2}  # complete (2 < 500): what is gone is dropped
    assert dock_state(db, A)["capped"] == 0
    # a result of cap or more may be cut off: it adds and refreshes, it never drops
    settings.structures.dock_search_cap = 2
    dock_world.structures[D1]["acl"] = [A]
    dock_world.structures[D2]["acl"] = []  # D2 is no longer returned ...
    due_again(db)
    stats = fresh_stats()
    await scout._dock_search(stats)
    assert docks(db)[A] == {S_HIGH, D1, D2}  # ... but D2 stays, D1 is added
    state = dock_state(db, A)
    assert state["capped"] == 1 and state["found"] == 2 and stats["dock_capped"] == 1


def forge_and_delve(g) -> tuple[list[str], list[str]]:
    def region_names(system_id: int) -> list[str]:
        r = g.region_of[g.index[system_id]]
        return [g.names[i] for i in range(len(g.names)) if g.region_of[i] == r]

    return region_names(JITA), region_names(DQ1)


async def test_the_fallback_terms_go_home_region_first_then_the_structures_and_their_regions(
    settings, services, dock_world
):
    await link(services, A, A, DOCK)
    scout = scout_for(settings, services)
    db = services["db"]
    forge, delve = forge_and_delve(scout.g)
    db.save_snapshot(A, solar_system_id=JITA)  # last known system
    assert scout.dock_terms(A) == [f"{n} - " for n in ["Jita", *[n for n in forge if n != "Jita"]]]
    now = time.time()
    with db.tx() as conn:  # a sighted structure in Delve, a dockable one in Jita (already in the first block)
        for sid, system in ((S_NULL, DQ1), (D2, JITA)):
            conn.execute(
                "INSERT INTO known_structures(structure_id, solar_system_id, status, first_seen, last_seen) "
                "VALUES(?,?,'ok',?,?)",
                (sid, system, now, now),
            )
        conn.execute("INSERT INTO structure_access VALUES(?,?,?)", (D2, A, now))
    sight(db, A, S_NULL)
    terms = scout.dock_terms(A)
    expected = ["Jita", *[n for n in forge if n != "Jita"], "1DQ1-A", *[n for n in delve if n != "1DQ1-A"]]
    assert terms == [f"{n} - " for n in expected]
    assert len(set(terms)) == len(terms)  # each system once


async def test_a_capped_result_searches_the_fallback_terms_bounded_and_resumes_at_the_cursor(
    settings, services, dock_world
):
    await link(services, A, A, DOCK)
    settings.structures.dock_search_cap = 1  # every non-empty result counts as cut off
    settings.structures.dock_search_max_terms = 3
    scout = scout_for(settings, services)
    db = services["db"]
    db.save_snapshot(A, solar_system_id=JITA)
    terms = scout.dock_terms(A)
    stats = fresh_stats()
    await scout._dock_search(stats)
    assert [t for _, t in dock_world.search_log] == [" - ", *terms[:3]]  # the term, then at most 3 systems
    assert dock_state(db, A)["cursor"] == 3 and stats["dock_capped"] == 1
    assert terms[0] == "Jita - " and docks(db)[A] >= {S_HIGH, D1, D2}  # "Jita - " finds the Jita structures
    dock_world.search_log.clear()
    due_again(db)
    await scout._dock_search(fresh_stats())
    assert [t for _, t in dock_world.search_log] == [" - ", *terms[3:6]]  # a later run goes on where this one stopped
    assert dock_state(db, A)["cursor"] == 6


async def test_the_fallback_cursor_cycles_through_all_terms(settings, services, dock_world, monkeypatch):
    await link(services, A, A, DOCK)
    settings.structures.dock_search_cap = 1
    settings.structures.dock_search_max_terms = 2
    scout = scout_for(settings, services)
    monkeypatch.setattr(scout, "dock_terms", lambda cid: ["Aaa - ", "Bbb - ", "Ccc - "])
    db = services["db"]
    seen: list[str] = []
    for expected_cursor in (2, 1, 0):
        dock_world.search_log.clear()
        await scout._dock_search(fresh_stats())
        seen += [t for _, t in dock_world.search_log if t != " - "]
        assert dock_state(db, A)["cursor"] == expected_cursor
        due_again(db)
    assert seen == ["Aaa - ", "Bbb - ", "Ccc - ", "Aaa - ", "Bbb - ", "Ccc - "]  # wraps around, eventually everything
    settings.structures.dock_search_max_terms = 0  # no fallback at all
    dock_world.search_log.clear()
    await scout._dock_search(fresh_stats())
    assert [t for _, t in dock_world.search_log] == [" - "]


async def test_a_passing_error_keeps_the_interval_and_a_refusal_waits_the_whole_of_it(
    settings, services, dock_world, monkeypatch
):
    await link(services, A, A, DOCK)
    scout = scout_for(settings, services)
    db = services["db"]
    await scout._dock_search(fresh_stats())
    due_again(db)
    passing_errors = (EsiError(503, "boom"), EsiError(429, "slow"), EsiError(420, "error limited"), EsiError(0, "net"))
    for passing in passing_errors:
        fail_search(scout, monkeypatch, passing)
        stats = fresh_stats()
        await scout._dock_search(stats)
        state = dock_state(db, A)
        assert state["checked_at"] == 1 and state["error"] and stats["dock_errors"] == 1  # still due next run
        assert docks(db)[A] == {S_HIGH, D1, D2}  # what was stored stays
        monkeypatch.undo()
    stats = fresh_stats()
    await scout._dock_search(stats)  # ESI is back: searched again at once
    state = dock_state(db, A)
    assert state["checked_at"] > 1 and state["error"] is None and stats["dock_searched"] == 1
    # 403: the error is recorded and the character waits the full interval
    due_again(db)
    fail_search(scout, monkeypatch, EsiError(403, "token not valid for scope"))
    await scout._dock_search(fresh_stats())
    state = dock_state(db, A)
    assert "scope" in state["error"] and state["checked_at"] > time.time() - 60
    monkeypatch.undo()
    dock_world.search_log.clear()
    await scout._dock_search(fresh_stats())
    assert dock_world.search_log == [] and docks(db)[A] == {S_HIGH, D1, D2}
    # a token SSO does not accept counts the same
    due_again(db)
    fail_search(scout, monkeypatch, SsoError("refresh token revoked"))
    await scout._dock_search(fresh_stats())
    assert dock_state(db, A)["checked_at"] > time.time() - 60 and "revoked" in dock_state(db, A)["error"]


async def test_a_first_search_that_fails_for_a_passing_reason_is_retried_next_run(
    settings, services, dock_world, monkeypatch
):
    await link(services, A, A, DOCK)
    scout = scout_for(settings, services)
    db = services["db"]
    fail_search(scout, monkeypatch, EsiError(502, "bad gateway"))
    await scout._dock_search(fresh_stats())
    assert dock_state(db, A)["checked_at"] == 0.0 and docks(db) == {}
    monkeypatch.undo()
    await scout._dock_search(fresh_stats())
    assert docks(db) == {A: {S_HIGH, D1, D2}}


async def test_the_dock_search_respects_the_error_window_and_pauses_the_fallback(
    settings, services, dock_world, monkeypatch
):
    await link(services, A, A, DOCK)
    scout = scout_for(settings, services)
    db = services["db"]
    reserves: list[int] = []
    monkeypatch.setattr(scout.esi, "blocked_for", lambda reserve=5: reserves.append(reserve) or 30.0)
    await scout._dock_search(fresh_stats())
    assert dock_world.search_log == [] and dock_state(db, A) is None  # nothing sent, nothing recorded
    from evedm.scout import SCOUT_ERROR_RESERVE

    assert reserves == [SCOUT_ERROR_RESERVE]
    # the window closes in the middle of the fallback: what was found is kept, the cursor says where it stopped
    settings.structures.dock_search_cap = 1
    settings.structures.dock_search_max_terms = 5
    monkeypatch.setattr(scout, "dock_terms", lambda cid: ["Jita - ", "Perimeter - ", "Amarr - ", "Rens - "])
    calls = {"n": 0}

    def closing(reserve=5):
        if reserve != SCOUT_ERROR_RESERVE:  # the client's own check
            return 0.0
        calls["n"] += 1
        return 0.0 if calls["n"] <= 3 else 30.0  # before the term, before "Jita - ", before "Perimeter - "

    monkeypatch.setattr(scout.esi, "blocked_for", closing)
    stats = fresh_stats()
    await scout._dock_search(stats)
    assert [t for _, t in dock_world.search_log] == [" - ", "Jita - ", "Perimeter - "]
    state = dock_state(db, A)
    assert state["cursor"] == 2 and "paused" in state["error"] and state["checked_at"] == 0.0  # retried next run
    assert docks(db)[A] == {S_HIGH, D1, D2} and stats["dock_errors"] == 1


async def test_characters_that_may_dock_are_asked_first_and_resolve_without_a_403(
    settings, services, dock_world, monkeypatch
):
    dock_world.structures[D3] = {"name": "Delve - Club", "owner_id": CORP, "solar_system_id": DQ1, "type_id": 35832,
                                 "acl": [A]}
    dock_world.assets[B] = [*dock_world.assets[B], asset(14, D3, "other")]  # B has assets in D3 but may not read it
    await link(services, A, A, DOCK)
    await link(services, B, B, ALL)
    scout = scout_for(settings, services)
    sight(services["db"], B, D3)
    await scout._dock_search(fresh_stats())
    pool = scout._load_pool()
    assert pool.access[D3] == [A] and pool.sightings[D3] == [B]
    assert scout._candidates(D3, pool) == [A, B]  # the one known to be allowed goes first, then the sighter
    assert scout._candidates(D3, pool, main_id=B) == [A, B]  # even before the main's own link
    sent = watch(scout, monkeypatch)
    await scout.run()
    assert [x for x in sent if x[0] == D3] == [(D3, A)]
    assert statuses(services["db"])[D3] == "ok" and not {r for r in refusals(services["db"]) if r[0] == D3}


async def test_the_orphan_purge_spares_structures_somebody_may_dock_at(settings, services, world):
    await link(services, A, A, ALL)
    scout = scout_for(settings, services)
    await scout.run()
    db = services["db"]
    now = time.time()
    with db.tx() as conn:
        for sid in (T1, T2):  # refused a moment ago: the hourly step leaves them alone
            conn.execute(
                "INSERT INTO known_structures(structure_id, status, first_seen, last_seen, checked_at) "
                "VALUES(?,?,?,?,?)",
                (sid, "no_access", now, now, now),
            )
        conn.execute("INSERT INTO structure_access VALUES(?,?,?)", (T1, A, now))
    stats = await scout.run()
    assert stats["purged_orphans"] == 1
    left = statuses(db)
    assert T1 in left and T2 not in left  # no sighting, but A may dock there


async def test_dock_search_rows_of_unregistered_characters_and_old_rows_are_pruned(settings, services, world):
    await link(services, A, A, ALL)
    scout = scout_for(settings, services)
    db = services["db"]
    now = time.time()
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO structure_access VALUES(?,?,?)",
            [(T1, A, now), (T2, A, now - 31 * 86400), (T1, 777, now), (T3, 777, now)],
        )
        conn.execute("INSERT INTO scout_dock_search(character_id, checked_at) VALUES(777, ?)", (now,))
        conn.execute("INSERT INTO scout_dock_search(character_id, checked_at) VALUES(?, ?)", (A, now))
    await scout.run()  # prunes as part of the step
    assert docks(db) == {A: {T1}}  # character 777 is not registered, T2 was not refreshed for 30 days
    assert [r[0] for r in db.query("SELECT character_id FROM scout_dock_search")] == [A]


async def test_the_listing_source_filter_defaults_to_assets_and_shows_counts_only(settings, services, dock_world):
    await link(services, A, A, DOCK)
    await link(services, B, B, ALL)
    scout = scout_for(settings, services)
    await scout._dock_search(fresh_stats())  # D1 and D2 are stored, not yet read
    scout.version += 1
    unread = {r["structure_id"]: r for r in scout.listing({}, [], source="dock")["rows"]}
    assert {D1, D2, S_HIGH} == set(unread) and unread[D1]["status"] == "unread"
    await scout.run()
    ids = lambda **kw: {r["structure_id"] for r in scout.listing({}, [], **kw)["rows"]}  # noqa: E731
    with_assets = ids()
    assert ids(source="assets") == with_assets and {S_HIGH, S_WH, S_NULL} <= with_assets
    assert D1 not in with_assets and D2 not in with_assets  # dockable, but nobody has assets there
    assert ids(source="dock") == {D1, D2}  # dockable and without assets (S_HIGH has both)
    assert ids(source="all") == with_assets | {D1, D2} and ids(source="nonsense") == with_assets
    listing = scout.listing({}, [], source="all", limit=500)
    by_id = {r["structure_id"]: r for r in listing["rows"]}
    assert (by_id[S_HIGH]["assets"], by_id[S_HIGH]["dockable"], by_id[S_HIGH]["dock_by"]) == (True, True, 1)
    d1 = by_id[D1]
    assert (d1["assets"], d1["dockable"], d1["dock_by"], d1["seen_by"]) == (False, True, 1, 0)
    assert (by_id[S_WH]["assets"], by_id[S_WH]["dockable"], by_id[S_WH]["dock_by"]) == (True, False, 0)
    assert listing["facets"]["sources"] == {"assets": len(with_assets), "dock": 2, "all": len(with_assets) + 2}
    assert listing["facets"]["total"] == len(with_assets) + 2 and listing["facets"]["source"] == "all"
    assert scout.listing({}, [])["facets"]["total"] == len(with_assets)  # the header count of the default view
    text = json.dumps(listing)
    assert str(A) not in text and str(B) not in text and "Alpha" not in text  # who may dock is never shown
    assert all(isinstance(r["dock_by"], int) for r in listing["rows"])


def test_the_structures_api_takes_the_source_filter(tmp_path):
    with demo(tmp_path) as c:
        get = lambda **p: c.get("/api/structures", params=p).json()  # noqa: E731
        base = get()
        assert base["matched"] == 7 and base["facets"]["source"] == "assets"
        assert base["facets"]["sources"] == {"assets": 7, "dock": 0, "all": 7}
        assert get(source="dock")["matched"] == 0 and get(source="all")["matched"] == 7
        svc = c.app.state.svc
        sid = 1_099_000_000_001
        now = time.time()
        with svc.db.tx() as conn:  # a structure somebody may dock at, nobody has assets there
            conn.execute(
                "INSERT INTO known_structures(structure_id,name,owner_id,solar_system_id,type_id,status,first_seen,"
                "last_seen,checked_at) VALUES(?,?,?,?,?,'ok',?,?,?)",
                (sid, "Jita - Open House", 1, 30000142, 35832, now, now, now),
            )
            conn.execute("INSERT INTO structure_access VALUES(?,?,?)", (sid, 5, now))
            conn.execute("INSERT INTO structure_access VALUES(?,?,?)", (sid, 6, now))
        svc.scout.version += 1
        assert get()["matched"] == 7  # the default view is unchanged
        dock = get(source="dock")
        assert dock["matched"] == 1 and dock["rows"][0]["dock_by"] == 2 and dock["rows"][0]["seen_by"] == 0
        assert get(source="all")["matched"] == 8 and get(source="all")["facets"]["sources"]["dock"] == 1
        assert "character" not in json.dumps(dock["rows"][0]).replace("characters", "")  # counts, not characters
        assert 'id="s-source"' in c.get("/structures").text and "s-source" in c.get("/static/structures.js").text


async def test_stats_have_a_dock_search_block(settings, services, dock_world):
    await link(services, A, A, DOCK)
    scout = scout_for(settings, services)
    empty = scout.stats()["dock_search"]
    assert empty["characters_searched"] == 0 and empty["access_rows"] == 0 and empty["eligible_characters"] == 1
    assert empty["term"] == " - " and empty["cap"] == 500 and empty["enabled"] is True
    await scout._dock_search(fresh_stats())
    block = scout.stats()["dock_search"]
    assert block["characters_searched"] == 1 and block["capped"] == 0 and block["with_errors"] == 0
    assert block["access_rows"] == 3 and block["dockable"] == 3 and block["dockable_unresolved"] == 3
    await scout.run()  # reads them; S_HIGH has assets of A
    block = scout.stats()["dock_search"]
    assert block["dockable"] == 3 and block["dockable_without_assets"] == 2 and block["dockable_unresolved"] == 0
    services["db"].execute("DROP TABLE structure_access")  # a database that was not migrated yet (read-only CLI)
    services["db"].execute("DROP TABLE scout_dock_search")
    old = scout.stats()["dock_search"]
    assert old["access_rows"] == 0 and old["characters_searched"] == 0


async def test_search_structure_ids_is_the_one_request_for_the_character_search(services, dock_world):
    tokens = await mint(services["http"], A, scopes=DOCK)
    from evedm.esi import search_structure_ids

    esi = services["esi"]
    assert await search_structure_ids(esi, A, tokens["access_token"], " - ") == sorted([S_HIGH, D1, D2])
    assert await search_structure_ids(esi, A, tokens["access_token"], " » ") == []  # ESI answers {}: no match
    assert dock_world.search_log == [(A, " - "), (A, " » ")]

    class Stub:
        paths: list[str] = []

        async def get(self, path, token=None):
            self.paths.append(path)

    stub = Stub()
    assert await search_structure_ids(stub, 5, "t", " - ") == []
    assert stub.paths == ["/characters/5/search/?categories=structure&search=%20-%20&strict=false"]


def test_the_dock_search_settings_are_checked():
    s = Settings()
    s.secret_key, s.demo = "k", True
    assert (s.structures.dock_search_enabled, s.structures.dock_search_hours, s.structures.dock_search_term) == (
        True,
        24.0,
        " - ",
    )
    assert (s.structures.dock_search_cap, s.structures.dock_search_max_terms) == (500, 40)
    s.validate_for_serve()
    from evedm.config import ConfigError

    for field, value in (("dock_search_term", "ab"), ("dock_search_hours", 0), ("dock_search_cap", 0),
                         ("dock_search_max_terms", -1)):
        bad = Settings()
        bad.secret_key, bad.demo = "k", True
        setattr(bad.structures, field, value)
        with pytest.raises(ConfigError, match="dock_search"):
            bad.validate_for_serve()
        bad.structures.dock_search_enabled = False  # not checked while switched off
        bad.validate_for_serve()


def test_search_probe_command_runs_the_terms_and_writes_nothing(tmp_path, settings, fake_app, monkeypatch, capsys):
    from common import FAKE
    from evedm import cli
    from evedm.characters import CharacterService
    from evedm.crypto import TokenCipher
    from evedm.db import Database
    from evedm.sso import AuthService

    fake = fake_app.state.fake
    fake.structures.update(
        {
            S_HIGH: {"name": "Jita - Trade Hub", "solar_system_id": JITA, "acl": [A]},
            D1: {"name": "Perimeter - Dock Only", "solar_system_id": 30000144, "acl": [A]},
            D2: {"name": "Jita - Open House", "solar_system_id": JITA, "acl": None},
            D3: {"name": "Jita » Amarr - Gate", "solar_system_id": JITA, "acl": [A]},
        }
    )

    async def seed() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_app), base_url=FAKE) as http:
            db = Database(settings.db_path, settings.sqlite_journal)
            auth = AuthService(settings.eve, http)
            chars = CharacterService(db, TokenCipher(settings.secret_key), auth, settings)
            for cid, scopes in ((A, DOCK), (B, ALL)):
                tokens = await mint(http, cid, scopes=scopes)
                chars.store(await auth.refresh(tokens["refresh_token"]), owner_id=cid)
            db.save_snapshot(A, solar_system_id=JITA)
            db.close()

    asyncio.run(seed())

    def tables() -> dict[str, list]:
        db = Database(settings.db_path, read_only=True)
        out = {
            t: [tuple(r) for r in db.query(f"SELECT * FROM {t} ORDER BY 1, 2")]  # noqa: S608
            for t in ("known_structures", "structure_sightings", "structure_refusals", "structure_access",
                      "scout_dock_search")
        }
        db.close()
        return out

    before = tables()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    fake_client = lambda **kw: real_client(transport=httpx.ASGITransport(app=fake_app), base_url=FAKE)  # noqa: E731
    monkeypatch.setattr("evedm.web.app.httpx.AsyncClient", fake_client)
    settings.structures.dock_search_cap = 3
    assert cli.main(["structures", "search-probe", "--character", str(A)]) == 0
    out = json.loads(capsys.readouterr().out)
    by_term = {t["term"]: t for t in out["terms"]}
    assert list(by_term) == [" - ", " » ", "Jita - "]  # configured, the Ansiblex term, the last known system
    assert by_term[" - "]["count"] == 4 and by_term[" - "]["hit_cap"] is True  # the cap is 3 here
    assert by_term[" - "]["structure_ids"] == 4 and by_term[" - "]["sample"] == sorted(by_term[" - "]["sample"])
    assert len(by_term[" - "]["sample"]) == 4 and by_term[" » "]["count"] == 1 and by_term[" » "]["hit_cap"] is False
    assert by_term["Jita - "]["count"] == 2 and by_term["Jita - "]["sample"] == sorted([S_HIGH, D2])
    assert out["cap"] == 3 and out["stopped"] is None and out["character_id"] == A
    assert tables() == before  # nothing stored
    assert cli.main(["structures", "search-probe", "--character", str(A), "--term", "Perim", "--term", "zzz"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(t["term"], t["count"], t["sample"]) for t in out["terms"]] == [("Perim", 1, [D1]), ("zzz", 0, [])]
    assert tables() == before
    # a character without both scopes cannot be searched; no character at all is a usage error
    assert cli.main(["structures", "search-probe", "--character", str(B)]) == 2
    assert "not registered with both search scopes" in capsys.readouterr().err
    assert cli.main(["structures", "search-probe"]) == 2
    text = json.dumps(out)
    assert "token" not in text and "Alpha" not in text
