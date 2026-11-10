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
    by_id = {r["structure_id"]: r for r in scout.listing({}, [])["rows"]}
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
