"""Per-main ownership: every main sees only its own linked characters (USR-*)."""

import sqlite3
from pathlib import Path

import pytest

from common import JITA, add_character
from evedm.bundle import export_bundle
from evedm.db import SCHEMA_VERSION, Database

pytestmark = pytest.mark.anyio
A, B = 2100000001, 2100000002  # two mains
SHARED = 2100000003  # linked by both


async def test_links_are_scoped_per_main(services):
    db, chars = services["db"], services["chars"]
    for owner in (A, B):
        db.upsert_user(owner, f"main {owner}")
        await add_character(services, owner, owner_id=owner)
        await add_character(services, SHARED, owner_id=owner)
    await add_character(services, 2100000004, owner_id=A)

    assert [c.character_id for c in db.list_characters(A)] == [A, SHARED, 2100000004]
    assert [c.character_id for c in db.list_characters(B)] == [B, SHARED]
    assert db.list_characters(0) == []
    view = {c["character_id"]: c for c in chars.list_view(A)}
    assert view[A]["main"] is True and view[SHARED]["main"] is False

    # each link has its own token and local settings
    assert db.get_tokens(SHARED, A)["refresh_token"] != db.get_tokens(SHARED, B)["refresh_token"]
    db.set_tags(SHARED, ["cyno"], A)
    assert db.get_character(SHARED, A).tags == ["cyno"] and db.get_character(SHARED, B).tags == []

    # ranking only uses the caller's characters
    rows_a = (await services["ranking"].rank(services["ranking"].g.index[JITA], refresh=2, owner_id=A))["rows"]
    rows_b = (await services["ranking"].rank(services["ranking"].g.index[JITA], refresh=2, owner_id=B))["rows"]
    assert {r["character_id"] for r in rows_a} == {A, SHARED, 2100000004}
    assert {r["character_id"] for r in rows_b} == {B, SHARED}
    assert services["db"].recent_targets(owner_id=A) == [JITA] and services["db"].recent_targets(owner_id=0) == []

    # removing a shared character for one main keeps the other link and the shared snapshot
    await chars.remove(SHARED, A)
    assert db.get_character(SHARED, A) is None and db.get_character(SHARED, B) is not None
    assert SHARED in db.get_snapshots()
    await chars.remove(SHARED, B)
    assert SHARED not in db.get_snapshots()

    # exports only contain the caller's links
    assert {c["character_id"] for c in export_bundle(chars, owner_id=B)["characters"]} == {B}

    # deleting an account removes every link of that main only
    assert await chars.remove_user(A) == 2
    assert db.get_user(A) is None and db.list_characters(A) == []
    assert [c.character_id for c in db.list_characters(B)] == [B]


async def test_expired_token_refresh_and_reauth_are_per_link(services, http):
    db, chars = services["db"], services["chars"]
    await add_character(services, SHARED, owner_id=A)
    await add_character(services, SHARED, owner_id=B)
    db.execute("UPDATE tokens SET access_expires_at=0")
    assert await chars.access_token(SHARED, A)
    assert db.get_tokens(SHARED, A)["access_expires_at"] > db.get_tokens(SHARED, B)["access_expires_at"]


def test_reassign_owner_moves_legacy_characters(db):
    for cid in (1, 2):
        db.upsert_character(cid, f"c{cid}", "OK", [])
        db.save_tokens(cid, "x", None, 0, b"r", "tail")
    db.upsert_character(2, "c2", "OK", [], owner_id=A)
    db.save_tokens(2, "x", None, 0, b"mine", "mine", owner_id=A)
    assert db.reassign_owner(0, A) == 1
    assert [c.character_id for c in db.list_characters(A)] == [1, 2]
    assert db.get_tokens(2, A)["refresh_tail"] == "mine"  # an existing link wins
    assert db.list_characters(0) == [] and db.get_tokens(1, A) is not None


def test_v1_database_is_migrated_to_owner_0(tmp_path):
    path = tmp_path / "v1.sqlite3"
    con = sqlite3.connect(path)
    con.executescript((Path(__file__).parent / "fixtures" / "schema-v1.sql").read_text(encoding="utf-8"))
    con.execute("PRAGMA user_version=1")
    con.execute("INSERT INTO characters(character_id,name,added_at,tags) VALUES(7,'Old','1','[\"x\"]')")
    con.execute("INSERT INTO tokens(character_id,client_id,refresh_token,updated_at) VALUES(7,'c',x'01',1)")
    con.execute("INSERT INTO snapshots(character_id,solar_system_id) VALUES(7,30000142)")
    con.execute("INSERT INTO target_history VALUES(30000142, 1)")
    con.commit()
    con.close()

    db = Database(path)
    try:
        assert db.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
        c = db.get_character(7)
        assert c is not None and c.owner_id == 0 and c.tags == ["x"]
        assert db.get_tokens(7)["refresh_token"] == b"\x01"
        assert db.get_snapshots()[7]["solar_system_id"] == 30000142
        assert db.recent_targets() == [30000142]
        assert db.query("PRAGMA foreign_key_check") == []
        assert db.query("SELECT name FROM sqlite_master WHERE name LIKE '%_v1'") == []
    finally:
        db.close()


async def test_one_mains_broken_link_does_not_show_on_the_other_mains_ranking(services, graph):
    """B's ranking must report B's own refresh result, not the error A's revoked link left in the shared snapshot."""
    from evedm.ranking import LiveProvider, RankingService

    db, chars = services["db"], services["chars"]
    ranking = RankingService(graph, db, LiveProvider(chars, services["esi"]), refresh_interval_s=60, max_wait_age_s=60)
    await add_character(services, SHARED, owner_id=A)
    await add_character(services, SHARED, owner_id=B)
    jita = graph.index[JITA]
    assert (await ranking.rank(jita, refresh=1, owner_id=B))["rows"][0]["error"] is None

    tok = db.get_tokens(SHARED, A)  # A's link breaks: refresh token no longer valid
    db.save_tokens(SHARED, tok["client_id"], None, 0, chars.cipher.encrypt("revoked-token"), "x", owner_id=A)
    result_a = await ranking.rank(jita, refresh=2, owner_id=A)
    assert result_a["rows"] == [] and result_a["excluded"][0]["reason"] == "re-auth required"
    assert db.get_snapshots()[SHARED]["error"]  # the failure is recorded on the shared snapshot

    row_b = (await ranking.rank(jita, refresh=1, owner_id=B))["rows"][0]  # B answered from its own fresh round
    assert row_b["error"] is None and row_b["status"] == "OK"
    assert db.get_character(SHARED, B).status == "OK"


async def test_link_without_ship_scope_keeps_the_ship_another_link_fetched(services, graph):
    db = services["db"]
    no_ship = ["esi-location.read_location.v1", "esi-location.read_online.v1"]
    await add_character(services, 2100000002, owner_id=B)  # full scopes
    await add_character(services, 2100000002, scopes=no_ship, owner_id=A)
    jita = graph.index[JITA]
    await services["ranking"].rank(jita, refresh=2, owner_id=B)
    assert db.get_snapshots()[2100000002]["ship_type_id"] == 29990
    row_a = (await services["ranking"].rank(jita, refresh=2, owner_id=A))["rows"][0]
    assert db.get_snapshots()[2100000002]["ship_type_id"] == 29990  # not blanked by A's narrower link
    assert row_a["ship_type_id"] == 29990 and row_a["system"]["name"] == "Perimeter"


def test_adopting_skips_demo_characters_and_keeps_target_history(db):
    db.upsert_character(1, "real", "OK", [])
    db.save_tokens(1, "x", None, 0, b"r", "t")
    db.upsert_character(3900000001, "demo", "OK", [], demo=True)
    db.save_tokens(3900000001, "demo", None, 0, b"r", "t")
    db.remember_target(30000142)
    assert db.reassign_owner(0, A) == 1
    assert [c.character_id for c in db.list_characters(A)] == [1]
    assert [c.character_id for c in db.list_characters(0)] == [3900000001] and db.get_tokens(3900000001)
    assert db.recent_targets(owner_id=A) == [30000142]
    assert db.query("PRAGMA foreign_key_check") == []


def test_users_listing_counts_links(db):
    db.upsert_user(A, "Alice")
    assert db.upsert_user(A, "Alice") is False
    db.upsert_character(5, "x", "OK", [], owner_id=A)
    assert db.list_users()[0]["characters"] == 1
    db.delete_user(A)
    assert db.list_users() == [] and db.list_characters(A) == []


def test_cli_users_list_and_adopt(tmp_path, monkeypatch, capsys):
    import json as _json

    from evedm.cli import main

    monkeypatch.setenv("EVEDM_DATA_DIR", str(tmp_path))
    d = Database(tmp_path / "evedm.sqlite3")
    d.upsert_character(11, "legacy", "OK", [])
    d.upsert_user(A, "Alice")
    d.close()
    assert main(["users", "list"]) == 0
    rows = [_json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert rows[0]["characters"] == 1 and rows[1]["user_id"] == A
    assert main(["users", "adopt", "--to", str(B)]) == 2  # B never signed in
    assert main(["users", "adopt"]) == 2
    capsys.readouterr()
    assert main(["users", "adopt", "--to", str(A)]) == 0
    assert "moved 1 characters" in capsys.readouterr().out
    assert main(["rank", "Jita", "--owner", str(A), "--no-refresh", "--json"]) == 2  # needs the secret key
    monkeypatch.setenv("EVEDM_SECRET_KEY", "k")
    assert main(["rank", "Jita", "--owner", str(A), "--no-refresh", "--json"]) == 0
    assert [r["character_id"] for r in _json.loads(capsys.readouterr().out)["rows"]] == [11]
