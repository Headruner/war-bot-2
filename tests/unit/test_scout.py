"""Structure scout: player-owned structures found through every registered character's assets, shared by all mains."""

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
from evedm.wars.config import WarsSettings
from evedm.wars.structures import Structure, StructureIndex
from evedm.web.app import create_app
from fake_eve import ALL_SCOPES

pytestmark = pytest.mark.anyio
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
    row = services["db"].query("SELECT structures, error FROM scout_characters WHERE character_id=?", (A,))[0]
    assert row["error"] is None and row["structures"] == 2 and stats["truncated"] == 1  # pages 1-2: S_HIGH and S_WH
    settings.structures.max_asset_pages = 20
    services["db"].execute("UPDATE scout_characters SET checked_at=0")
    stats = await scout.run()
    row = services["db"].query("SELECT structures, error FROM scout_characters WHERE character_id=?", (A,))[0]
    assert row["error"] is None and row["structures"] == 3 and stats["truncated"] == 0


async def test_item_typed_locations_are_never_structures(settings, services, world):
    container = 1_050_000_000_999  # a can in a station hangar; its row is on a page that is not read
    world.asset_page_size = 2
    world.assets[A] = [
        asset(21, container),  # a ship or can's content: "item" typed, parent unknown while truncated
        asset(22, S_HIGH, "other"),
        asset(23, 60003760, "station"),
        asset(container, 60003760, "station"),
    ]
    await link(services, A, A)
    settings.structures.max_asset_pages = 1
    scout = scout_for(settings, services)
    await scout.run()
    db = services["db"]
    row = db.query("SELECT structures, error FROM scout_characters WHERE character_id=?", (A,))[0]
    assert row["error"] is None and row["structures"] == 1
    assert [r[0] for r in db.query("SELECT structure_id FROM known_structures")] == [S_HIGH]
    settings.structures.max_asset_pages = 20  # the whole list: still not a structure
    db.execute("UPDATE scout_characters SET checked_at=0")
    await scout.run()
    assert [r[0] for r in db.query("SELECT structure_id FROM known_structures")] == [S_HIGH]


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


async def test_each_page_is_saved_and_a_failure_keeps_the_earlier_pages(settings, services, world, monkeypatch):
    world.asset_page_size = 2
    await link(services, A, A)
    real = services["esi"].iter_pages

    async def flaky(path, token, max_pages=20):
        async for chunk, pages in real(path, token, max_pages):
            yield chunk, pages
            raise EsiError(503, "boom")  # after the first page

    monkeypatch.setattr(services["esi"], "iter_pages", flaky)
    scout = scout_for(settings, services)
    stats = await scout.run()
    db = services["db"]
    assert stats["errors"] == 1 and stats["characters_read"] == 0
    assert [r[0] for r in db.query("SELECT structure_id FROM structure_sightings")] == [S_HIGH]  # page 1 is in the file
    row = db.query("SELECT checked_at, error FROM scout_characters WHERE character_id=?", (A,))[0]
    assert row["checked_at"] == 0 and "503" in row["error"]  # a passing trouble: read again on the next run
    monkeypatch.undo()
    stats = await scout.run()
    assert stats["characters_read"] == 1
    assert {r[0] for r in db.query("SELECT structure_id FROM structure_sightings")} == {S_HIGH, S_WH, S_GONE}
    assert db.query("SELECT error FROM scout_characters WHERE character_id=?", (A,))[0][0] is None


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
    # nobody sights it and nobody may read it: the row is kept, not dropped
    orphan = 1_060_000_000_001
    now = time.time()
    db.execute(
        "INSERT INTO known_structures(structure_id, status, first_seen, last_seen) VALUES(?,?,?,?)",
        (orphan, "new", now, now),
    )
    # A loses docking access: the name, owner and system stay, flagged "no docking access"
    world.structures[S_HIGH]["acl"] = [B]
    db.execute("UPDATE known_structures SET checked_at=0")
    services["esi"]._cache.clear()
    await scout.run()
    row = {r["structure_id"]: r for r in scout.structures()}[S_HIGH]
    assert row["access"] is False and row["name"] == "Jita - Trade Hub"
    assert db.query("SELECT COUNT(*) FROM known_structures WHERE structure_id=?", (orphan,))[0][0] == 1
    # the structure is unanchored or destroyed: ESI answers 404, it is kept as a hidden 'gone' row, not asked again
    del world.structures[S_HIGH]
    db.execute("UPDATE known_structures SET checked_at=0")
    services["esi"]._cache.clear()
    await scout.run()
    assert db.query("SELECT status FROM known_structures WHERE structure_id=?", (S_HIGH,))[0][0] == "gone"
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


async def test_main_resolves_unresolved_structures_one_by_one(settings, services, world, monkeypatch):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])  # the main: may read structures, no assets
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "no_access", S_PRIVATE: "no_access", S_WH: "new", S_GONE: "new"})
    scout = scout_for(settings, services)
    asked: list[str] = []
    real = scout.esi.get

    async def spy(path, token=None):
        asked.append(path)
        return await real(path, token)

    monkeypatch.setattr(scout.esi, "get", spy)
    result = await scout.resolve_with_main(C)
    assert result["targets"] == 4 and result["public_first"] == 1 and result["stopped"] is None
    assert (result["resolved"], result["no_access"], result["gone"], result["remaining"]) == (2, 1, 1, 0)
    # the public list is read once without a token, and the public structure is asked before the others
    assert asked[0] == "/universe/structures/" and asked[1] == f"/universe/structures/{S_WH}"
    status = {r[0]: r[1] for r in db.query("SELECT structure_id, status FROM known_structures")}
    assert status == {S_HIGH: "no_access", S_PRIVATE: "ok", S_WH: "ok", S_GONE: "gone"}
    rows = {r["structure_id"]: r for r in scout.structures()}
    assert rows[S_PRIVATE]["name"] == "Secret" and rows[S_WH]["name"] == "J-Home" and rows[S_WH]["owner_id"] == CORP
    assert {r["structure_id"] for r in scout.unresolved()} == {S_HIGH}  # still refused: stays listed by ID only


async def test_a_limited_run_resumes_with_the_rows_not_yet_tried(settings, services, world):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    db = services["db"]
    seed_unresolved(db, {S_HIGH: "new", S_NULL: "new", S_PRIVATE: "new"})
    scout = scout_for(settings, services)
    first = await scout.resolve_with_main(C, limit=1)
    assert first["targets"] == 3 and first["remaining"] == 2
    second = await scout.resolve_with_main(C, limit=10)
    assert second["targets"] == 3  # the one still refused is asked again, after the two never tried
    assert (second["resolved"], second["no_access"]) == (1, 2)
    done = {r[0]: r[1] for r in db.query("SELECT structure_id, status FROM known_structures")}
    assert done[S_PRIVATE] == "ok" and done[S_HIGH] == done[S_NULL] == "no_access"


async def test_resolver_needs_the_main_with_the_structure_scope(settings, services, world):
    scout = scout_for(settings, services)
    with pytest.raises(ResolveError) as exc:
        await scout.resolve_with_main(C)
    assert exc.value.code == "no_character"
    await link(services, C, C, scopes=ALL_SCOPES)  # no read_structures scope
    with pytest.raises(ResolveError) as exc:
        await scout.resolve_with_main(C)
    assert exc.value.code == "no_scope"
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    services["db"].set_status(C, REAUTH, C)
    with pytest.raises(ResolveError) as exc:
        await scout.resolve_with_main(C)
    assert exc.value.code == "reauth"


async def test_resolver_stops_when_esi_is_error_limited(settings, services, world, monkeypatch):
    await link(services, C, C, scopes=[STRUCTURE_SCOPES[0]])
    seed_unresolved(services["db"], {S_HIGH: "new", S_PRIVATE: "new"})
    scout = scout_for(settings, services)
    monkeypatch.setattr(scout.esi, "blocked_for", lambda reserve=5: 42.0)
    result = await scout.resolve_with_main(C)
    assert result["stopped"] == "error_limit" and result["retry_after_s"] == 42
    assert result["resolved"] == 0 and result["remaining"] == 2
    assert {r[0] for r in services["db"].query("SELECT status FROM known_structures")} == {"new"}  # nothing marked


def test_resolve_endpoint_in_demo_is_refused(tmp_path):
    with demo(tmp_path) as c:
        assert c.post("/api/structures/resolve").status_code == 403  # the CSRF header is required
        reply = c.post("/api/structures/resolve", headers={"X-EVEDM": "1"})
        assert reply.status_code in (409, 422) and reply.json()["error"] in ("demo", "no_character")


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
    assert found == {"EVE Ref": 1, "Adam4EVE": 2}  # S_NULL has no details in the scrape; id 999 is not wanted
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
    by_id = {r["structure_id"]: r for r in scout.listing({}, [])["rows"]}
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
    first = await scout.resolve_with_main(B)
    assert first["no_access"] == 1 and first["history"] == {"EVE Ref": 1, "Adam4EVE": 1}
    again = await scout.resolve_with_main(B)  # forced downloads are limited to one per 10 minutes
    assert again["history"] == {"EVE Ref": 0, "Adam4EVE": 0}
