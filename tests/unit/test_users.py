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
    con.executescript((Path(__file__).parent / "fixtures" / "schema-v1.sql").read_text())
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
