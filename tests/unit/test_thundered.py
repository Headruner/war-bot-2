"""Import from a ThunderED database: one token per character (the best scopes), stored for the legacy EVE
application, refreshed with it once it is entered; a character the main already has is left alone."""

from __future__ import annotations

import json
import sqlite3

import pytest

from evedm.characters import LIMITED, OK, short_scope
from evedm.config import NOTIFICATION_SCOPE
from evedm.sso import LEGACY_CLIENT, SsoError
from evedm.thundered import TAG, best_tokens, import_legacy, read_legacy

pytestmark = pytest.mark.anyio

MAIN = 2100000005
FULL, NOTIF = 2100000001, 2100000002
LEGACY_ID, LEGACY_SECRET = "thundered-app", "thundered-secret"
STRUCTS = ["esi-universe.read_structures.v1", "esi-assets.read_assets.v1"]


def thundered_db(path, rows: list[tuple[int, int, str, str]]) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE tokens(id integer primary key autoincrement, token text not null, type int not null,"
        " character_id integer not null, roles integer, scopes text);"
        "CREATE TABLE auth_users(Id INTEGER PRIMARY KEY, characterID INTEGER NOT NULL, refreshToken TEXT,"
        " character_name text);"
    )
    con.executemany("INSERT INTO tokens(character_id,type,token,scopes) VALUES(?,?,?,?)", rows)
    con.execute("INSERT INTO tokens(character_id,type,token,scopes) VALUES(2100000003, 0, 'login-only', '')")
    con.execute("INSERT INTO auth_users(characterID, character_name) VALUES(?, 'Bravo From Thundered')", (NOTIF,))
    con.commit()
    con.close()


@pytest.fixture
def legacy(tmp_path, settings, fake_app, db):
    """The fake SSO only knows the legacy application; ThunderED holds two characters."""
    fake = fake_app.state.fake
    fake.client_id, fake.client_secret = LEGACY_ID, LEGACY_SECRET
    full = [*settings.eve.required_scopes, *STRUCTS]
    rows = [
        (FULL, 2, "notif-token-1", NOTIFICATION_SCOPE),
        (FULL, 0, "general-token-1", ",".join(full)),
        (NOTIF, 2, "notif-token-2", ",".join([*STRUCTS, NOTIFICATION_SCOPE])),
    ]
    for cid, _, tok, scopes in rows:
        fake.refresh[tok] = (cid, scopes.split(","))
    path = tmp_path / "edb.db"
    thundered_db(path, rows)
    db.upsert_user(MAIN, "Echo Five")
    return path


def test_the_token_with_the_best_scopes_wins(legacy, settings):
    tokens, names = read_legacy(legacy)
    best = best_tokens(tokens, settings.eve.required_scopes, settings.eve.optional_scopes)
    assert best[FULL].refresh_token == "general-token-1" and best[FULL].token_type == 0
    assert best[NOTIF].refresh_token == "notif-token-2"
    assert names == {NOTIF: "Bravo From Thundered"}


async def test_dry_run_writes_nothing(legacy, services, settings, db):
    entries = import_legacy(legacy, db, services["chars"].cipher, settings, MAIN, dry_run=True)
    results = [(e.character_id, e.result) for e in entries]
    assert results == [(FULL, "Added"), (NOTIF, "Limited"), (2100000003, "Skipped")]
    assert "no ESI scopes" in entries[2].reason
    assert db.list_characters(MAIN) == []


async def test_imported_tokens_wait_for_the_legacy_application_then_refresh_with_it(legacy, services, settings, db):
    chars = services["chars"]
    import_legacy(legacy, db, chars.cipher, settings, MAIN)
    linked = {c.character_id: c for c in db.list_characters(MAIN)}
    assert linked[FULL].status == OK and linked[NOTIF].status == LIMITED
    assert linked[NOTIF].name == "Bravo From Thundered" and linked[FULL].name == f"Character {FULL}"
    assert all(c.tags == [TAG] for c in linked.values())
    row = db.get_tokens(FULL, MAIN)
    assert row["client_id"] == LEGACY_CLIENT and row["access_token"] is None
    assert chars.cipher.decrypt(row["refresh_token"]) == "general-token-1"

    with pytest.raises(SsoError) as err:  # not entered yet: the character waits, it is not marked REAUTH
        await chars.access_token(FULL, MAIN)
    assert err.value.code == "legacy_client_missing"
    assert db.get_character(FULL, MAIN).status == OK

    settings.eve.legacy_client_id, settings.eve.legacy_client_secret = LEGACY_ID, LEGACY_SECRET
    assert await chars.access_token(FULL, MAIN)
    row = db.get_tokens(FULL, MAIN)
    assert row["client_id"] == LEGACY_ID and row["access_expires_at"] > 0
    assert db.get_character(FULL, MAIN).name == "Alpha One"  # the EVE name from the refreshed token
    assert await chars.access_token(FULL, MAIN)  # cached, then refreshed with the stored legacy client ID


async def test_a_token_of_our_own_application_does_not_use_the_legacy_one(legacy, services, settings, db):
    settings.eve.legacy_client_id, settings.eve.legacy_client_secret = LEGACY_ID, LEGACY_SECRET
    auth = services["auth"]
    assert not auth.is_legacy(settings.eve.client_id) and not auth.is_legacy(None)
    assert auth.is_legacy(LEGACY_CLIENT) and auth.is_legacy(LEGACY_ID) and not auth.is_legacy("someone-else")
    assert auth.stored_client_id(LEGACY_CLIENT) == LEGACY_ID
    assert auth.stored_client_id(None) == settings.eve.client_id


async def test_a_character_the_main_has_is_skipped(legacy, services, settings, db):
    db.upsert_character(FULL, "Alpha One", OK, settings.eve.required_scopes, owner_id=MAIN)
    entries = import_legacy(legacy, db, services["chars"].cipher, settings, MAIN)
    assert {e.character_id: e.result for e in entries} == {FULL: "Skipped", NOTIF: "Limited", 2100000003: "Skipped"}
    assert db.get_tokens(FULL, MAIN) is None  # untouched
    assert json.loads(json.dumps([e.__dict__ for e in entries]))  # the CLI prints it as JSON


def test_not_a_thundered_database(tmp_path):
    path = tmp_path / "other.db"
    sqlite3.connect(path).close()
    with pytest.raises(ValueError, match="not a ThunderED database"):
        read_legacy(path)


@pytest.mark.parametrize(
    ("scope", "label"),
    [
        ("esi-location.read_location.v1", "location"),
        ("esi-characters.read_notifications.v1", "notifications"),
        ("esi-corporations.read_structures.v1", "corporations:structures"),
        ("esi-killmails.read_corporation_killmails.v1", "killmails:corporation_killmails"),
        ("esi-mail.send_mail.v1", "mail:send_mail"),
        ("esi-ui.open_window.v1", "ui:open_window"),
    ],
)
def test_short_scope(scope, label):
    assert short_scope(scope) == label
