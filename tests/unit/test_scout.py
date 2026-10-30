"""Structure scout: player-owned structures found through every registered character's assets, shared by all mains."""

import pytest
from fastapi.testclient import TestClient

from common import mint
from evedm.config import STRUCTURE_SCOPES, Settings, load_settings
from evedm.esi import EsiError
from evedm.scout import StructureScout
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
                "acl": [A],
            },
            S_NULL: {"name": "1DQ1-A - Keep", "owner_id": CORP, "solar_system_id": DQ1, "type_id": 35834, "acl": [B]},
            S_WH: {"name": "J-Home", "owner_id": CORP, "solar_system_id": THERA, "type_id": 35832, "acl": None},
            S_PRIVATE: {"name": "Secret", "owner_id": CORP, "solar_system_id": JITA, "type_id": 35825, "acl": [C]},
        }
    )
    st.assets[A] = [
        asset(1, S_HIGH),
        asset(2, 1, flag="Cargo"),  # inside A's own ship (item 1): not a structure
        asset(3, 60003760, "station"),  # NPC station: never listed
        asset(4, S_WH),
        asset(5, S_GONE),  # an ID ESI does not know as a structure
    ]
    st.assets[B] = [asset(11, S_NULL), asset(12, S_HIGH), asset(13, S_PRIVATE)]  # B is not on S_HIGH's ACL
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
    assert [r["structure_id"] for r in listing["rows"]][-1] == S_WH  # unreachable last


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
    await scout.run()
    row = services["db"].query("SELECT structures, error FROM scout_characters WHERE character_id=?", (A,))[0]
    assert row["error"] == "assets truncated" and row["structures"] == 2  # pages 1-2: S_HIGH and S_WH
    settings.structures.max_asset_pages = 20
    services["db"].execute("UPDATE scout_characters SET checked_at=0")
    await scout.run()
    row = services["db"].query("SELECT structures, error FROM scout_characters WHERE character_id=?", (A,))[0]
    assert row["error"] is None and row["structures"] == 3


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
    assert s.eve.optional_scopes == STRUCTURE_SCOPES
    assert s.eve.requested_scopes[:3] == s.eve.required_scopes and s.eve.requested_scopes[3:] == STRUCTURE_SCOPES
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
