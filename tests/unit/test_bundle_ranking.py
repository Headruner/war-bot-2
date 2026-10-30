import json

import pytest

from common import JITA, TAMA, add_character, mint
from evedm.bundle import BundleRejected, export_bundle, import_bundle, parse, report, validate
from evedm.characters import CharacterService
from evedm.crypto import TokenCipher
from evedm.db import Database
from evedm.demo import seed_demo
from evedm.ranking import DemoProvider, RankingService

pytestmark = pytest.mark.anyio


def bundle(client_id, entries):
    return json.dumps({"schema_version": 1, "client_id": client_id, "characters": entries}).encode()


# --------------------------------------------------------------------------- schema / file level
def test_schema_errors_report_json_paths():
    errors = validate({"schema_version": 2, "client_id": "", "characters": [{"access_token": "x"}]})
    text = "\n".join(errors)
    assert "$.schema_version" in text and "$.client_id" in text
    assert "$.characters[0]" in text and "refresh_token" in text


def test_example_bundle_from_the_spec_is_valid():
    doc = {
        "schema_version": 1,
        "client_id": "0123",
        "exported_at": "2026-10-29T12:00:00Z",
        "characters": [
            {
                "character_id": 2112345678,
                "refresh_token": "cmVmcmVzaC10b2tlbi1B",
                "access_token": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
                "scopes": ["a"],
                "tags": ["main"],
                "enabled": True,
            },
            {"refresh_token": "cmVmcmVzaC1C"},
        ],
    }
    assert validate(doc) == []


def test_file_level_rejections():
    with pytest.raises(BundleRejected, match="1 MB"):
        parse(b" " * 1_000_001)
    with pytest.raises(BundleRejected, match="JSON"):
        parse(b"{not json")
    with pytest.raises(BundleRejected) as err:
        parse(b'{"schema_version": 1}')
    assert err.value.errors


async def test_bundle_from_another_application_is_rejected(services):
    with pytest.raises(BundleRejected, match="another EVE application"):
        await import_bundle(bundle("other-app", [{"refresh_token": "abcdefgh"}]), services["chars"])
    assert services["db"].list_characters() == []


# --------------------------------------------------------------------------- import
async def test_import_adds_updates_limits_and_rejects(services, http, settings):
    t1 = await mint(http, 2100000001)
    t2 = await mint(http, 2100000002, scopes=["esi-location.read_location.v1"])
    t3 = await mint(http, 2100000003)
    await http.post("/_admin/revoke/2100000003")
    t4 = await mint(http, 2100000005, expired=True)
    entries = [
        {"refresh_token": t1["refresh_token"], "access_token": t1["access_token"], "tags": ["main"]},
        {"refresh_token": t2["refresh_token"]},
        {"refresh_token": t3["refresh_token"]},
        {"refresh_token": t4["refresh_token"], "access_token": t4["access_token"], "character_id": 2100000005},
        {"refresh_token": t1["refresh_token"], "character_id": 2100000004},  # wrong character
    ]
    results = await import_bundle(bundle(settings.eve.client_id, entries), services["chars"])
    got = [(r.result, r.reason) for r in results]
    assert got == [
        ("Added", ""),
        ("Limited", ""),
        ("Rejected", "invalid_grant"),
        ("Added", ""),
        ("Rejected", "character_mismatch"),
    ]
    assert results[1].missing_scopes == ["esi-location.read_ship_type.v1", "esi-location.read_online.v1"]
    assert services["db"].get_character(2100000001).tags == ["main"]
    rep = report(results)
    assert rep["summary"] == {"Added": 2, "Updated": 0, "Limited": 1, "Rejected": 2}

    services["db"].set_tags(2100000001, ["local"])
    again = await import_bundle(
        bundle(settings.eve.client_id, [{"refresh_token": t1["refresh_token"], "tags": ["file"]}]), services["chars"]
    )
    assert again[0].result == "Updated"
    assert services["db"].get_character(2100000001).tags == ["local"]  # IMP-07


async def test_import_stores_rotated_refresh_token(services, http, settings, fake_app):
    fake_app.state.fake.rotate_refresh = True
    t1 = await mint(http, 2100000001)
    await import_bundle(bundle(settings.eve.client_id, [{"refresh_token": t1["refresh_token"]}]), services["chars"])
    row = services["db"].get_tokens(2100000001)
    stored = services["chars"].cipher.decrypt(row["refresh_token"])
    assert stored != t1["refresh_token"]  # IMP-06: the newest refresh token is kept


async def test_export_then_import_round_trip(services, http, settings, tmp_path):
    await add_character(services, 2100000001)
    await add_character(services, 2100000002)
    services["db"].set_tags(2100000002, ["pvp"])
    doc = export_bundle(services["chars"])
    assert validate(doc) == []
    assert {c["character_id"] for c in doc["characters"]} == {2100000001, 2100000002}
    assert all("access_token" in c for c in doc["characters"])

    other_db = Database(tmp_path / "other.sqlite3")
    other = CharacterService(other_db, TokenCipher("other-key"), services["auth"], settings)
    results = await import_bundle(json.dumps(doc).encode(), other)
    assert [r.result for r in results] == ["Added", "Added"]
    assert other_db.get_character(2100000002).tags == ["pvp"]
    other_db.close()


async def test_export_skips_demo_characters(services, graph):
    seed_demo(services["db"], services["chars"].cipher, graph)
    with pytest.raises(BundleRejected):
        export_bundle(services["chars"])


# --------------------------------------------------------------------------- tokens
async def test_expiring_token_is_refreshed_and_revocation_marks_reauth(services, http):
    await add_character(services, 2100000001)
    services["db"].execute("UPDATE tokens SET access_expires_at=0")
    token = await services["chars"].access_token(2100000001)
    assert token.startswith("eyJ")
    assert services["db"].get_tokens(2100000001)["access_expires_at"] > 0
    await http.post("/_admin/revoke/2100000001")
    services["db"].execute("UPDATE tokens SET access_expires_at=0")
    with pytest.raises(Exception):
        await services["chars"].access_token(2100000001)
    assert services["db"].get_character(2100000001).status == "REAUTH"


async def test_remove_revokes_and_deletes(services, http, fake_app):
    await add_character(services, 2100000001)
    await services["chars"].remove(2100000001)
    assert services["db"].get_character(2100000001) is None
    assert fake_app.state.fake.revoked


# --------------------------------------------------------------------------- ranking (live provider)
async def test_ranking_order_route_and_flags(services, http, graph):
    for cid in (2100000001, 2100000002, 2100000003, 2100000004, 2100000005):
        await add_character(services, cid)
    result = await services["ranking"].rank(graph.index[TAMA])
    rows = result["rows"]
    assert [r["name"] for r in rows] == ["Echo Five", "Bravo Two", "Alpha One", "Charlie Three", "Delta Four"]
    assert [r["jumps"] for r in rows] == [0, 4, 4, 13, None]
    assert rows[0]["recommended"] and not any(r["recommended"] for r in rows[1:])
    assert rows[1]["online"] is True  # tie at 4 jumps: online first
    assert rows[2]["route"][0] == JITA and rows[2]["route"][-1] == TAMA
    assert rows[2]["route_summary"] == {"high": 4, "low": 1, "null": 0}
    assert rows[2]["docked"] is True and rows[1]["docked"] is False
    assert rows[3]["in_pod"] is True and rows[3]["ship_type"] == "Capsule"
    assert rows[0]["ship_type"] == "Prototype Hull"  # resolved through ESI /universe/names, then cached
    assert rows[4]["system"]["name"] == "Thera" and rows[4]["reachable"] is False
    assert result["stats"]["reachable"] == 4
    assert services["db"].get_type_names([99999]) == {99999: "Prototype Hull"}


async def test_ranking_excludes_disabled_and_reauth(services, http, graph):
    await add_character(services, 2100000001)
    await add_character(services, 2100000002)
    services["db"].set_enabled(2100000002, False)
    await http.post("/_admin/revoke/2100000001")
    services["db"].execute("UPDATE tokens SET access_expires_at=0")
    result = await services["ranking"].rank(graph.index[JITA])
    assert result["rows"] == []
    reasons = {e["name"]: e["reason"] for e in result["excluded"]}
    assert reasons == {"Bravo Two": "disabled", "Alpha One": "re-auth required"}


async def test_ranking_keeps_last_known_data_on_esi_error(services, http, graph, fake_app):
    await add_character(services, 2100000001)
    await services["ranking"].rank(graph.index[JITA])
    del fake_app.state.fake.characters[2100000001]  # the fake now fails for this character
    services["esi"]._cache.clear()
    result = await services["ranking"].rank(graph.index[JITA])
    row = result["rows"][0]
    assert row["error"] and row["error"].startswith("esi:")
    assert row["system"]["name"] == "Jita" and row["jumps"] == 0


# --------------------------------------------------------------------------- demo
async def test_demo_ranking_for_tama(db, graph):
    cipher = TokenCipher("k")
    assert seed_demo(db, cipher, graph) == 10
    assert seed_demo(db, cipher, graph) == 0  # idempotent
    ranking = RankingService(graph, db, DemoProvider(db), avoid_names=["Zarzakh"])
    result = await ranking.rank(graph.find("Tama"))
    rows = result["rows"]
    assert rows[0]["name"] == "Kael Varn" and rows[0]["jumps"] == 4 and rows[0]["recommended"]
    assert rows[-1]["name"] == "Rho Senn" and rows[-1]["reachable"] is False
    assert [e["name"] for e in result["excluded"]] == ["Old Alt"]
    lena = next(r for r in rows if r["name"] == "Lena Orrin")
    assert lena["status"] == "LIMITED" and lena["ship_type"] == ""
    assert db.recent_targets()[0] == graph.ids[graph.find("Tama")]
