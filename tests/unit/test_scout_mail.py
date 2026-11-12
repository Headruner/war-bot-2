# ruff: noqa: F811  (the ``world`` fixture of test_scout.py is imported and then requested as an argument)
"""STR-14: structures named in in-game mails. The scout reads the mails of every character that granted the mail scope,
keeps structure ID, type, link text and owner corporation, and shows them in the shared list (source ``mail``)."""

import asyncio
import json
import logging
import time
from collections import Counter

import pytest
from test_scout import CORP, JITA, S_HIGH, A, B, link, scout_for, world  # noqa: F401

from evedm.config import MAIL_SCOPE, ConfigError, Settings
from evedm.esi import EsiError
from evedm.scout import SCOUT_ERROR_RESERVE
from evedm.sso import SsoError
from evedm.wars.structures import Structure, StructureIndex

pytestmark = pytest.mark.anyio
MAILER = ["esi-location.read_location.v1", MAIL_SCOPE]  # a character with nothing but the mail scope (and a location)
M1, M2, M3, M4 = (1_036_000_000_000 + n for n in range(1, 5))
OWNER = 98_765_432
PERIMETER = 30000144
SECRET_BODY, SECRET_SUBJECT, SENDER, RECIPIENT = (
    "TOP-SECRET-BODY-TEXT",
    "TOP-SECRET-SUBJECT",
    2_119_876_543,
    2_118_765_432,
)
NOW = time.time()


def stamp(age_days: float = 1.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - age_days * 86400))


def a(type_id: int, item_id: int, text: str) -> str:
    return f'<font size="14" color="#ffd98d00"><a href="showinfo:{type_id}//{item_id}">{text}</a></font>'


def pair(sid: int, name: str, corp: int = OWNER, type_id: int = 35832) -> str:
    return f"{a(type_id, sid, name)}<br>{a(2, corp, 'Some Corp')}<br>"


def mail(mail_id: int, body: str, age_days: float = 1.0, **extra) -> dict:
    return {
        "mail_id": mail_id,
        "timestamp": stamp(age_days),
        "from": SENDER,
        "subject": SECRET_SUBJECT,
        "body": f"{SECRET_BODY} {body}",
        "recipients": [{"recipient_id": RECIPIENT, "recipient_type": "character"}],
        "is_read": True,
        **extra,
    }


def counts() -> Counter:
    return Counter()


def finds(db) -> dict[int, dict]:
    return {r["structure_id"]: dict(r) for r in db.query("SELECT * FROM structure_mail_finds")}


def mail_row(db, cid: int) -> dict:
    return dict(db.query("SELECT * FROM scout_mail WHERE character_id=?", (cid,))[0])


def opened(fake) -> list[tuple[int, int]]:
    return [(cid, mid) for kind, cid, mid in fake.mail_log if kind == "open"]


def listed(fake) -> list[tuple[int, int | None]]:
    return [(cid, last) for kind, cid, last in fake.mail_log if kind == "list"]


def due_again(db) -> None:
    db.execute("UPDATE scout_mail SET checked_at=1 WHERE checked_at > 1")


def fail_mail(scout, monkeypatch, exc: Exception, only: str = "/mail/"):
    """Every ESI call whose path contains ``only`` fails with ``exc``; returns the function that undoes it."""
    real = scout.esi.get

    async def get(path, token=None, **kw):
        if only in path:
            raise exc
        return await real(path, token, **kw)

    monkeypatch.setattr(scout.esi, "get", get)
    return lambda: monkeypatch.setattr(scout.esi, "get", real)


@pytest.fixture
def mails(world):
    """Mail of A: one mail naming a structure with its owner and one naming a lone structure."""
    world.mails[A] = [mail(10, pair(M1, "Jita - Mailed Fort") + a(35833, M2, "Perimeter - Lone Anchor"))]
    return world


# ----------------------------------------------------------------------------------------------- the scan
async def test_the_scan_stores_the_finds_and_makes_known_structures_of_them(settings, services, mails):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    stats = counts()
    await scout._mail_scan(stats)
    assert (stats["mail_characters"], stats["mails_read"], stats["mail_found"], stats["mail_errors"]) == (1, 1, 2, 0)
    rows = finds(db)
    assert {k: rows[M1][k] for k in ("owner_id", "type_id", "name", "system_id", "mail_id")} == {
        "owner_id": OWNER,
        "type_id": 35832,
        "name": "Jita - Mailed Fort",
        "system_id": JITA,
        "mail_id": 10,
    }
    assert rows[M2]["owner_id"] is None and rows[M2]["system_id"] == PERIMETER and rows[M2]["type_id"] == 35833
    assert abs(rows[M1]["mail_ts"] - (NOW - 86400)) < 2
    known = {r["structure_id"]: r for r in db.query("SELECT * FROM known_structures")}
    assert {M1, M2} == set(known) and known[M1]["status"] == "new" and known[M1]["name"] is None
    assert known[M1]["first_seen"] > 0 and known[M1]["checked_at"] == 0
    assert tuple(db.query("SELECT mail_id, finds FROM mail_reads")[0]) == (10, 2)
    state = mail_row(db, A)
    assert (state["last_mail_id"], state["mails"], state["found"], state["error"]) == (10, 1, 2, None)
    assert state["checked_at"] > 0


async def test_a_run_scans_after_the_dock_search_and_reports_it_and_a_scan_that_finds_something_bumps_the_version(
    settings, services, mails
):
    await link(services, A, A, [*MAILER, "esi-assets.read_assets.v1", "esi-universe.read_structures.v1"])
    scout = scout_for(settings, services)
    order: list[str] = []
    for name in ("_dock_search", "_mail_scan", "_purge"):
        real = getattr(scout, name)

        def spy(*args, _real=real, _name=name, **kw):
            order.append(_name)
            return _real(*args, **kw)

        setattr(scout, name, spy)
    version = scout.version
    stats = await scout.run()
    assert order == ["_dock_search", "_mail_scan", "_purge"]
    assert (stats["mail_characters"], stats["mails_read"], stats["mail_found"], stats["mail_errors"]) == (1, 1, 2, 0)
    assert scout.version > version
    quiet = scout.version
    await scout._mail_scan(counts(), force=True)  # nothing new: nothing stored, the version is not bumped
    assert scout.version == quiet


async def test_only_characters_with_the_mail_scope_are_read_and_the_scan_can_be_switched_off(settings, services, mails):
    await link(services, A, A, MAILER)
    await link(services, B, B, ["esi-location.read_location.v1"])  # no mail scope
    scout = scout_for(settings, services)
    assert [c[0] for c in scout.mail_readers()] == [A]
    settings.structures.mail_enabled = False
    await scout._mail_scan(counts(), force=True)
    assert mails.mail_log == []
    settings.structures.mail_enabled = True
    await scout._mail_scan(counts())
    assert {cid for cid, _ in listed(mails)} == {A}


async def test_a_mail_sent_to_two_characters_is_opened_once(settings, services, world):
    await link(services, A, A, MAILER)
    await link(services, B, B, MAILER)
    shared = mail(10, pair(M1, "Jita - Mailed Fort"))
    world.mails[A], world.mails[B] = [shared], [dict(shared)]
    scout, db = scout_for(settings, services), services["db"]
    stats = counts()
    await scout._mail_scan(stats)  # both are read at the same time
    assert len(opened(world)) == 1 and stats["mails_read"] == 1 and stats["mail_found"] == 1
    assert db.query("SELECT COUNT(*) FROM mail_reads")[0][0] == 1
    assert mail_row(db, A)["last_mail_id"] == mail_row(db, B)["last_mail_id"] == 10  # both are past it
    due_again(db)
    await scout._mail_scan(counts())
    assert len(opened(world)) == 1  # and still once on the next run


async def test_the_older_mails_come_first_and_a_deleted_mail_counts_as_read(settings, services, world):
    await link(services, A, A, MAILER)
    world.mails[A] = [mail(12, pair(M2, "Jita - Newer")), mail(11, pair(M1, "Jita - Older"))]
    world.mail_body_fail[11] = 404  # deleted between the header list and the body
    scout, db = scout_for(settings, services), services["db"]
    stats = counts()
    await scout._mail_scan(stats)
    assert [mid for _, mid in opened(world)] == [11, 12]  # oldest first
    assert (stats["mails_read"], stats["mail_found"], stats["mail_errors"]) == (1, 1, 0)  # a 404 is not an error
    assert {r[0] for r in db.query("SELECT mail_id FROM mail_reads")} == {11, 12}
    assert set(finds(db)) == {M2} and mail_row(db, A)["last_mail_id"] == 12


async def test_paging_follows_the_smallest_id_of_a_page_up_to_the_page_limit(settings, services, world):
    await link(services, A, A, MAILER)
    world.mails[A] = [mail(n, "no links") for n in range(1, 121)]  # 120 mails: three pages of 50
    scout = scout_for(settings, services)
    settings.structures.mail_max_pages = 2
    stats = counts()
    await scout._mail_scan(stats)
    assert listed(world) == [(A, None), (A, 71)]  # the second page is asked for mails older than the first's smallest
    assert stats["mails_read"] == 100 and mail_row(services["db"], A)["last_mail_id"] == 120
    world.mail_log.clear()
    settings.structures.mail_max_pages = 4
    services["db"].execute("DELETE FROM scout_mail")
    services["db"].execute("DELETE FROM mail_reads")
    await scout._mail_scan(counts())
    assert listed(world) == [(A, None), (A, 71), (A, 21), (A, 1)]  # the 4th page is empty and ends it
    assert len(opened(world)) == 120


async def test_paging_stops_at_the_stored_mail_id(settings, services, world):
    await link(services, A, A, MAILER)
    world.mails[A] = [mail(n, "no links", age_days=2) for n in range(1, 61)]
    scout, db = scout_for(settings, services), services["db"]
    await scout._mail_scan(counts())
    assert mail_row(db, A)["last_mail_id"] == 60
    world.mail_log.clear()
    world.mails[A] += [mail(61, pair(M1, "Jita - New")), mail(62, "again nothing")]
    await scout._mail_scan(counts(), force=True)
    assert listed(world) == [(A, None)]  # the first page already holds mails we have seen: no second page
    assert [mid for _, mid in opened(world)] == [61, 62]
    assert mail_row(db, A)["last_mail_id"] == 62 and set(finds(db)) == {M1}


async def test_old_mails_are_never_opened_and_end_the_paging(settings, services, world):
    await link(services, A, A, MAILER)
    old = [mail(n, pair(M3, "Jita - Ancient"), age_days=40) for n in range(1, 41)]
    young = [mail(n, pair(M1, "Jita - Recent"), age_days=2) for n in range(41, 61)]
    world.mails[A] = old + young
    scout, db = scout_for(settings, services), services["db"]
    await scout._mail_scan(counts())
    assert listed(world) == [(A, None)]  # the first page reaches back past 30 days
    assert [mid for _, mid in opened(world)] == list(range(41, 61))
    assert set(finds(db)) == {M1}
    settings.structures.mail_max_age_days = 100
    db.execute("DELETE FROM scout_mail")
    await scout._mail_scan(counts())
    assert M3 in finds(db)  # the age limit is a setting


async def test_a_passing_error_keeps_the_character_due_and_the_progress(settings, services, world, monkeypatch):
    await link(services, A, A, MAILER)
    world.mails[A] = [mail(10, pair(M1, "Jita - One")), mail(11, pair(M2, "Jita - Two")), mail(12, "x")]
    scout, db = scout_for(settings, services), services["db"]
    undo = fail_mail(scout, monkeypatch, EsiError(503, "boom"), only="/mail/12/")
    stats = counts()
    await scout._mail_scan(stats)
    state = mail_row(db, A)
    assert state["checked_at"] == 0 and state["last_mail_id"] == 11 and "503" in state["error"]  # due at the next run
    assert stats["mail_errors"] == 1 and set(finds(db)) == {M1, M2}
    undo()
    stats = counts()
    await scout._mail_scan(stats)  # no force: a character with checked_at 0 is due
    assert stats["mails_read"] == 1 and mail_row(db, A)["last_mail_id"] == 12 and mail_row(db, A)["error"] is None
    assert [mid for _, mid in opened(world)] == [10, 11, 12]  # 10 and 11 were not opened again


@pytest.mark.parametrize("status", [0, 420, 429, 500, 503])
async def test_every_passing_status_keeps_the_character_due(settings, services, mails, monkeypatch, status):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    fail_mail(scout, monkeypatch, EsiError(status, "x"))
    await scout._mail_scan(counts())
    assert mail_row(db, A)["checked_at"] == 0 and mail_row(db, A)["last_mail_id"] == 0


async def test_a_refusal_or_a_token_that_is_not_accepted_waits_the_whole_interval(
    settings, services, mails, monkeypatch
):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    mails.mail_fail[A] = 403
    stats = counts()
    await scout._mail_scan(stats)
    state = mail_row(db, A)
    assert state["checked_at"] > 0 and "403" in state["error"] and stats["mail_errors"] == 1
    mails.mail_log.clear()
    await scout._mail_scan(counts())
    assert mails.mail_log == []  # waits for mail_refresh_minutes
    del mails.mail_fail[A]
    due_again(db)
    await scout._mail_scan(counts())
    assert mail_row(db, A)["error"] is None and set(finds(db)) == {M1, M2}
    due_again(db)

    async def refused(cid, owner=0):
        raise SsoError("refresh token revoked")

    monkeypatch.setattr(scout.chars, "access_token", refused)
    await scout._mail_scan(counts())
    assert mail_row(db, A)["checked_at"] > 1 and "revoked" in mail_row(db, A)["error"]


async def test_the_scan_stops_while_esi_s_error_window_is_low_and_when_cancelled(
    settings, services, mails, monkeypatch
):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    monkeypatch.setattr(scout.esi, "blocked_for", lambda reserve=5: 30.0)
    await scout._mail_scan(counts())
    assert mails.mail_log == [] and db.query("SELECT COUNT(*) FROM scout_mail")[0][0] == 0  # not even recorded
    monkeypatch.undo()
    cancel = asyncio.Event()
    cancel.set()
    await scout._mail_scan(counts(), cancel=cancel)
    assert mails.mail_log == [] and db.query("SELECT COUNT(*) FROM scout_mail")[0][0] == 0
    calls = []
    real = scout.esi.blocked_for

    def window(reserve=5):  # the scan's own checks use the scout's reserve; open for the header list, then closed
        if reserve != SCOUT_ERROR_RESERVE:
            return 0.0
        calls.append(1)
        return 0.0 if len(calls) <= 1 else 30.0

    monkeypatch.setattr(scout.esi, "blocked_for", window)
    stats = counts()
    await scout._mail_scan(stats)
    state = mail_row(db, A)
    assert {k for k, *_ in mails.mail_log} == {"list"} and state["checked_at"] == 0 and "error window" in state["error"]
    assert stats["mail_errors"] == 1 and real(5) == 0.0


async def test_a_body_that_is_not_json_is_an_error_without_a_piece_of_it(
    settings, services, mails, monkeypatch, caplog
):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    fail_mail(scout, monkeypatch, ValueError(f"Expecting value near {SECRET_BODY}"), only="/mail/10/")
    caplog.set_level(logging.DEBUG)
    await scout._mail_scan(counts())
    assert mail_row(db, A)["error"] == "ESI answered with something unreadable"
    assert SECRET_BODY not in caplog.text and SECRET_BODY not in str(mail_row(db, A))


async def test_a_corporation_link_in_a_type_the_type_table_knows_makes_a_structure(settings, services, world):
    await link(services, A, A, MAILER)
    world.mails[A] = [mail(10, pair(M1, "Jita - New kind", type_id=99_077) + pair(M2, "Jita - Not one", type_id=587))]
    scout, db = scout_for(settings, services), services["db"]
    await scout._mail_scan(counts())
    assert finds(db) == {}  # neither type is an Upwell structure to the scout
    db.execute("INSERT INTO type_groups(type_id, name, group_id, group_name, category_id) VALUES(99077,'X',1,'G',65)")
    db.execute("INSERT INTO type_groups(type_id, name, group_id, group_name, category_id) VALUES(587,'Rifter',2,'G',6)")
    db.execute("DELETE FROM mail_reads")
    db.execute("DELETE FROM scout_mail")
    await scout._mail_scan(counts())
    assert set(finds(db)) == {M1}  # category 65 only


# ------------------------------------------------------------------------------------ the storage rules
async def test_a_newer_mail_replaces_a_find_and_an_older_one_only_fills_what_is_missing(settings, services, world):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    world.mails[A] = [mail(20, a(35832, M1, "Jita - Newest Name") + a(2, OWNER + 1, "Newer corp"), age_days=1)]
    await scout._mail_scan(counts())
    assert (finds(db)[M1]["name"], finds(db)[M1]["owner_id"], finds(db)[M1]["mail_id"]) == (
        "Jita - Newest Name",
        OWNER + 1,
        20,
    )
    # a mail from earlier days turns up later (another character): its values do not replace the newer ones
    world.mails[A].append(mail(5, a(35833, M1, "Perimeter - Old Name") + a(2, OWNER, "Old corp"), age_days=9))
    await link(services, B, B, MAILER)
    world.mails[B] = [world.mails[A][1]]
    await scout._mail_scan(counts(), force=True)
    row = finds(db)[M1]
    assert (row["name"], row["owner_id"], row["type_id"], row["mail_id"], row["system_id"]) == (
        "Jita - Newest Name",
        OWNER + 1,
        35832,
        20,
        JITA,
    )
    # a NULL in the newer find is filled from the older one
    lone = mail(30, a(35832, M2, "Jita - Gets an owner"), age_days=3)
    owner = mail(31, a(35832, M2, "Jita - Gets an owner") + a(2, OWNER, "Corp"), age_days=8)
    world.mails[A] += [lone, owner]
    await scout._mail_scan(counts(), force=True)
    assert finds(db)[M2]["owner_id"] == OWNER and finds(db)[M2]["mail_id"] == 30  # newest mail, owner from the older
    # and a newer mail with an owner replaces the owner
    world.mails[A].append(mail(40, a(35832, M2, "Jita - Gets an owner") + a(2, OWNER + 7, "Other"), age_days=0.5))
    await scout._mail_scan(counts(), force=True)
    assert finds(db)[M2]["owner_id"] == OWNER + 7 and finds(db)[M2]["mail_id"] == 40


async def test_a_structure_that_is_gone_is_not_brought_back_but_its_find_is_kept(settings, services, mails):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    db.execute(
        "INSERT INTO known_structures(structure_id, status, first_seen, last_seen) VALUES(?,?,?,?)",
        (M1, "gone", 5.0, 5.0),
    )
    await scout._mail_scan(counts())
    row = db.query("SELECT status, first_seen, last_seen FROM known_structures WHERE structure_id=?", (M1,))[0]
    assert tuple(row) == ("gone", 5.0, 5.0)  # untouched
    assert M1 in finds(db)
    assert M1 not in {r["structure_id"] for r in scout.effective()}  # and never listed


async def test_a_known_structure_keeps_its_status_and_only_its_last_seen_moves(settings, services, mails):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    db.execute(
        "INSERT INTO known_structures(structure_id, name, status, first_seen, last_seen) VALUES(?,?,?,?,?)",
        (M1, "Jita - ESI name", "ok", 5.0, 5.0),
    )
    await scout._mail_scan(counts())
    row = db.query("SELECT status, name, first_seen, last_seen FROM known_structures WHERE structure_id=?", (M1,))[0]
    assert (row["status"], row["name"], row["first_seen"]) == ("ok", "Jita - ESI name", 5.0) and row["last_seen"] > 5


async def test_no_mail_text_reaches_the_database_the_logs_or_the_response_cache(settings, services, mails, caplog):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    caplog.set_level(logging.DEBUG)
    await scout.run()
    mails.mail_fail[A] = 403
    due_again(db)
    await scout._mail_scan(counts(), force=True)  # an error is logged and stored too
    assert len(opened(mails)) == 1 and finds(db)  # the scan did read the mail
    with db._lock:
        dump = "\n".join(db._conn.iterdump())
    for secret in (SECRET_BODY, SECRET_SUBJECT, str(SENDER), str(RECIPIENT), "recipient", "<font", "showinfo"):
        assert secret not in dump, f"{secret!r} is stored"
        assert secret not in caplog.text, f"{secret!r} is logged"
    assert [k for k in services["esi"]._cache if "/mail/" in k] == []  # not kept in memory either
    columns = {
        t: [r[1] for r in db.query(f"PRAGMA table_info({t})")]
        for t in ("structure_mail_finds", "mail_reads", "scout_mail")
    }  # noqa: S608
    assert columns == {
        "structure_mail_finds": [
            "structure_id",
            "owner_id",
            "type_id",
            "name",
            "system_id",
            "mail_id",
            "mail_ts",
            "found_at",
        ],
        "mail_reads": ["mail_id", "read_at", "finds"],
        "scout_mail": ["character_id", "checked_at", "last_mail_id", "mails", "found", "error"],
    }


async def test_mail_rows_of_characters_that_no_longer_exist_and_old_markers_are_pruned(settings, services, mails):
    await link(services, A, A, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    await scout._mail_scan(counts())
    now = time.time()
    keep = (settings.structures.mail_max_age_days + 7) * 86400
    with db.tx() as conn:
        conn.execute("INSERT INTO scout_mail(character_id, checked_at) VALUES(555, 1)")
        conn.execute("INSERT INTO mail_reads(mail_id, read_at) VALUES(1, ?)", (now - keep - 60,))
        conn.execute("INSERT INTO mail_reads(mail_id, read_at) VALUES(2, ?)", (now - keep + 3600,))
    await scout.run()
    assert {r[0] for r in db.query("SELECT character_id FROM scout_mail")} == {A}
    assert {r[0] for r in db.query("SELECT mail_id FROM mail_reads")} == {10, 2}


async def test_the_orphan_purge_spares_structures_a_mail_names(settings, services, world):
    await link(services, A, A, [*MAILER, "esi-assets.read_assets.v1", "esi-universe.read_structures.v1"])
    scout, db = scout_for(settings, services), services["db"]
    await scout.run()
    now = time.time()
    with db.tx() as conn:  # refused a moment ago: the hourly step leaves them alone
        for sid in (M1, M2):
            conn.execute(
                "INSERT INTO known_structures(structure_id, status, first_seen, last_seen, checked_at) "
                "VALUES(?,?,?,?,?)",
                (sid, "no_access", now, now, now),
            )
        conn.execute(
            "INSERT INTO structure_mail_finds(structure_id, mail_id, mail_ts, found_at) VALUES(?,?,?,?)",
            (M1, 1, now, now),
        )
    stats = await scout.run()
    assert stats["purged_orphans"] == 1
    left = {r[0] for r in db.query("SELECT structure_id FROM known_structures")}
    assert M1 in left and M2 not in left


async def test_the_names_of_owners_and_types_a_mail_names_are_resolved(settings, services, mails):
    mails.corp_names[OWNER] = "Mail Owners Inc"
    await link(services, A, A, MAILER)
    scout = scout_for(settings, services)
    await scout._mail_scan(counts())
    await scout._names()
    names = {r[0]: r[1] for r in services["db"].query("SELECT entity_id, name FROM entity_names")}
    assert names[OWNER] == "Mail Owners Inc" and 35832 in names and 35833 in names


# --------------------------------------------------------------------------------------------- the lists
def put(db, sid: int, **kw) -> None:
    """A known_structures row (status new, nothing known) and, when ``mail`` is given, its mail find."""
    mail_find = kw.pop("mail", None)
    row = {"status": "new", "first_seen": 1.0, "last_seen": 1.0, "checked_at": 0.0} | kw
    db.execute(
        f"INSERT INTO known_structures(structure_id, {','.join(row)}) VALUES(?,{','.join('?' * len(row))})",  # noqa: S608
        (sid, *row.values()),
    )
    if mail_find:
        find = {
            "owner_id": None,
            "type_id": None,
            "name": None,
            "system_id": None,
            "mail_id": 1,
            "mail_ts": 1_700_000_000.0,
        }
        find |= mail_find
        db.execute(
            "INSERT INTO structure_mail_finds(structure_id, owner_id, type_id, name, system_id, mail_id, mail_ts, "
            "found_at) VALUES(?,?,?,?,?,?,?,1)",
            (sid, find["owner_id"], find["type_id"], find["name"], find["system_id"], find["mail_id"], find["mail_ts"]),
        )


def history(db, sid: int, **kw) -> None:
    row = {"name": None, "owner_id": None, "owner_name": None, "system_id": None, "type_id": None, "type_name": None}
    row |= kw | {"observed": "2025-01-02", "source": "EVE Ref"}
    db.execute(
        f"INSERT INTO public_structure_history(structure_id,{','.join(row)}) VALUES(?,{','.join('?' * len(row))})",  # noqa: S608
        (sid, *row.values()),
    )


def seed_overlays(db) -> None:
    full = {"name": "Jita - Mail Name", "owner_id": OWNER, "system_id": JITA, "type_id": 35832}
    put(db, M1, mail=full)  # unread: everything comes from the mail
    put(  # read by ESI: ESI wins everywhere
        db,
        M2,
        name="Perimeter - ESI Name",
        owner_id=CORP,
        solar_system_id=PERIMETER,
        type_id=35836,
        status="ok",
        mail=full | {"name": "Jita - Other", "owner_id": OWNER, "system_id": JITA, "type_id": 35832},
    )
    put(db, M3, mail={"name": "Jita - Name Only"})  # the mail has a name, the dataset the rest
    history(
        db,
        M3,
        name="Jita - Dataset Name",
        owner_id=OWNER + 1,
        owner_name="Dataset Corp",
        system_id=PERIMETER,
        type_id=35834,
        type_name="Keepstar",
    )
    put(db, M4)  # only a dataset knows it
    history(db, M4, name="Jita - Dataset Only", owner_id=OWNER + 2, system_id=JITA)


async def test_overlay_order_is_esi_then_the_mail_then_the_datasets(settings, services):
    db = services["db"]
    seed_overlays(db)
    db.execute("INSERT INTO entity_names VALUES(?,?,?,1)", (OWNER, "Mail Owners Inc", "corporation"))
    db.execute("INSERT INTO entity_names VALUES(?,?,?,1)", (35832, "Astrahus", "inventory_type"))
    scout = scout_for(settings, services)
    rows = {r["structure_id"]: r for r in scout.effective()}
    m1 = rows[M1]  # all from the mail
    assert (m1["name"], m1["owner_id"], m1["owner"], m1["type"], m1["status"]) == (
        "Jita - Mail Name",
        OWNER,
        "Mail Owners Inc",
        "Astrahus",
        "unread",
    )
    assert m1["system"]["id"] == JITA and m1["system"]["space"] == "high" and m1["system_index"] is not None
    assert m1["mail"] == {"date": "2023-11-14", "owner": True} and m1["mailed"] and "historical_source" not in m1
    assert m1["owner_zkill"].endswith(f"/corporation/{OWNER}/")
    m2 = rows[M2]  # ESI read it: the mail changes nothing
    assert (m2["name"], m2["owner_id"], m2["system"]["id"], m2["type_id"]) == (
        "Perimeter - ESI Name",
        CORP,
        PERIMETER,
        35836,
    )
    assert "mail" not in m2 and m2["mailed"] is True
    m3 = rows[M3]  # the mail's name, the dataset's owner, system and type
    assert m3["name"] == "Jita - Name Only" and m3["owner_id"] == OWNER + 1 and m3["owner"] == "Dataset Corp"
    assert m3["system"]["id"] == PERIMETER and m3["type"] == "Keepstar"
    assert m3["mail"] == {"date": "2023-11-14", "owner": False} and m3["historical_source"] == "EVE Ref"
    m4 = rows[M4]
    assert m4["mailed"] is False and "mail" not in m4 and m4["name"] == "Jita - Dataset Only"
    assert m4["historical_source"] == "EVE Ref" and m4["historical_date"] == "2025-01-02"


async def test_a_mail_find_without_a_known_owner_name_or_type_name_gets_placeholders(settings, services):
    db = services["db"]
    put(db, M1, mail={"name": "Jita - X", "owner_id": OWNER, "type_id": 35832, "system_id": 12})  # 12: not in the graph
    row = {r["structure_id"]: r for r in scout_for(settings, services).effective()}[M1]
    assert row["owner"] == f"Corporation {OWNER}" and row["type"] == "Type 35832"
    assert row["system"]["space"] == "unknown" and row["system_index"] is None  # an unknown system id is not used


async def test_a_destroyed_structure_gets_no_overlay(settings, services):
    db = services["db"]
    put(db, M1, destroyed_at=5.0, mail={"name": "Jita - Gone", "owner_id": OWNER})
    row = scout_for(settings, services).effective()[0]
    assert row["name"] == f"Unknown structure {M1}" and row["owner_id"] is None and row["mailed"] is True


async def test_the_listing_source_mail_shows_the_named_structures_and_counts_them(settings, services):
    db = services["db"]
    seed_overlays(db)
    scout = scout_for(settings, services)
    got = scout.listing({}, [], source="mail", limit=500)
    assert {r["structure_id"] for r in got["rows"]} == {M1, M2, M3}
    assert got["facets"]["source"] == "mail" and got["facets"]["sources"] == {
        "assets": 0,
        "dock": 0,
        "mail": 3,
        "all": 4,
    }
    assert scout.listing({}, [], source="all", limit=500)["matched"] == 4
    assert scout.listing({}, [])["matched"] == 0  # the default view (assets) does not change
    by_id = {r["structure_id"]: r for r in got["rows"]}
    assert by_id[M1]["mail"] == {"date": "2023-11-14", "owner": True} and by_id[M1]["mailed"] is True
    assert all(not {"system_index", "position", "name_unresolved"} & set(r) for r in got["rows"])  # internal only
    assert by_id[M1]["jumps"] is None and by_id[M1]["nearest"] is None  # no character: no jumps
    near = scout.listing({scout.g.index[JITA]: ["Alpha"]}, [], source="mail", limit=500)
    assert {r["structure_id"]: r["jumps"] for r in near["rows"]}[M1] == 0


async def test_the_war_lists_get_a_structure_only_a_mail_knows(settings, services):
    db = services["db"]
    seed_overlays(db)
    put(db, M4 + 1, mail={"name": "Jita - Lone link", "type_id": 35832, "system_id": JITA})  # no owner: not listed
    scout = scout_for(settings, services)
    idx = scout.index()
    assert [s.structure_id for s in idx[OWNER]] == [M1]
    assert idx[OWNER][0] == Structure(M1, "Jita - Mail Name", 35832, JITA, OWNER, None)
    assert [s.structure_id for s in idx[CORP]] == [M2]  # ESI read: as before
    assert {s.structure_id for s in idx[OWNER + 1]} == {M3} and M4 + 1 not in {
        s.structure_id for v in idx.values() for s in v
    }
    seen: list = []
    scout.on_change.append(seen.append)
    scout._notify()
    assert seen and [s.structure_id for s in seen[0][OWNER]] == [M1]
    graph_index = StructureIndex(scout.g, "http://unused", "ua")
    graph_index.set_discovered(idx)
    assert [s.structure_id for s in graph_index.owned_by([OWNER])[OWNER]] == [M1]


async def test_the_stats_have_a_mail_block_and_an_old_database_reports_it_empty(
    settings, services, mails, tmp_path, capsys
):
    await link(services, A, A, MAILER)
    await link(services, B, B, MAILER)
    scout, db = scout_for(settings, services), services["db"]
    mails.mail_fail[B] = 403
    await scout._mail_scan(counts())
    db.execute("UPDATE known_structures SET status='ok' WHERE structure_id=?", (M1,))
    assert scout.stats()["mail"] == {
        "enabled": True, "readers": 2, "characters_scanned": 2, "with_errors": 1, "mails_parsed": 1,
        "finds": 2, "finds_with_owner": 1, "finds_read_by_esi": 1,
    }  # fmt: skip
    import sqlite3

    from evedm.cli import main
    from evedm.db import Database

    data = tmp_path / "old"
    Database(data / "evedm.sqlite3").close()
    con = sqlite3.connect(data / "evedm.sqlite3")  # a v19 database: no mail tables yet
    for table in ("structure_mail_finds", "mail_reads", "scout_mail"):
        con.execute(f"DROP TABLE {table}")  # noqa: S608
    con.execute("PRAGMA user_version=19")
    con.commit()
    con.close()
    assert main(["--data-dir", str(data), "structures", "stats"]) == 0
    assert json.loads(capsys.readouterr().out)["mail"]["finds"] == 0


# ------------------------------------------------------------------------------------ Resolve unknown
@pytest.fixture
def resolve_world(world):
    """A is a main that may read structures and has mail; B is another main with mail only."""
    world.mails[A] = [mail(10, pair(S_HIGH, "Jita - Trade Hub", CORP) + pair(M1, "Jita - Mailed Fort"))]
    world.mails[B] = [mail(11, pair(M2, "Perimeter - Other Fort"))]
    return world


RESOLVER = ["esi-universe.read_structures.v1", "esi-assets.read_assets.v1", MAIL_SCOPE]


async def test_resolve_unknown_scans_the_mails_of_every_reader_first_and_reports_it(settings, services, resolve_world):
    await link(services, A, A, [*MAILER, *RESOLVER])
    await link(services, B, B, MAILER)  # another main: its mail is read too
    scout, db = scout_for(settings, services), services["db"]
    order: list[str] = []
    real_scan, real_refresh, real_ask = scout._mail_scan, scout._refresh_directory, scout._ask

    async def scan(stats, **kw):
        order.append(f"mail force={kw.get('force')} cancel={kw.get('cancel') is not None}")
        await real_scan(stats, **kw)

    async def refresh(force=False):
        order.append("datasets")
        return await real_refresh(force)

    async def ask(sid, order_, pool):
        order.append("ask")
        return await real_ask(sid, order_, pool)

    scout._mail_scan, scout._refresh_directory, scout._ask = scan, refresh, ask
    published: list = []
    scout.on_change.append(published.append)
    job = await scout.run_resolve(A)
    assert order[:2] == ["mail force=True cancel=True", "datasets"] and "ask" in order
    assert (job["mails_read"], job["mail_found"], job["stopped"]) == (2, 3, "done")
    assert set(finds(db)) == {S_HIGH, M1, M2}
    assert published and M1 in {
        s.structure_id for v in published[0].values() for s in v if s.owner_id == OWNER
    }  # shown at once
    # ESI read S_HIGH: its find stays, but the overlay no longer applies because nothing is missing
    assert db.query("SELECT status FROM known_structures WHERE structure_id=?", (S_HIGH,))[0][0] == "ok"
    row = {r["structure_id"]: r for r in scout.effective()}[S_HIGH]
    assert row["mailed"] is True and "mail" not in row and row["owner_id"] == CORP and row["name"] == "Jita - Trade Hub"
    assert "mails_read" in scout.resolve_state() and scout.resolve_state()["mail_found"] == 3


async def test_a_mail_scan_that_fails_does_not_fail_the_job(settings, services, resolve_world, monkeypatch):
    await link(services, A, A, [*MAILER, *RESOLVER])
    scout = scout_for(settings, services)

    async def broken(stats, **kw):
        raise RuntimeError("mail trouble")

    monkeypatch.setattr(scout, "_mail_scan", broken)
    job = await scout.run_resolve(A)
    assert job["stopped"] == "done" and job["mails_read"] == 0 and job["mail_found"] == 0 and job["failure"] is None


async def test_a_cancelled_job_stops_the_mail_scan_and_everything_after_it(settings, services, resolve_world):
    await link(services, A, A, [*MAILER, *RESOLVER])
    scout = scout_for(settings, services)
    job = scout.start_resolve(A)
    scout.stop_resolve()
    await scout._job_task
    state = scout.resolve_state()
    assert job["running"] is True and state["stopped"] == "cancelled" and state["running"] is False
    assert resolve_world.mail_log == []  # cancelled before the first request


async def test_resolve_unknown_does_not_scan_when_the_mail_scan_is_switched_off(settings, services, resolve_world):
    await link(services, A, A, [*MAILER, *RESOLVER])
    settings.structures.mail_enabled = False
    job = await scout_for(settings, services).run_resolve(A)
    assert resolve_world.mail_log == [] and job["mails_read"] == 0


# ------------------------------------------------------------------------------------ settings and schema
def test_the_mail_scope_is_a_default_optional_scope_and_the_settings_have_defaults():
    s = Settings()
    assert s.eve.optional_scopes[-1] == MAIL_SCOPE == "esi-mail.read_mail.v1" and MAIL_SCOPE in s.eve.requested_scopes
    sc = s.structures
    assert (sc.mail_enabled, sc.mail_refresh_minutes, sc.mail_max_pages, sc.mail_max_age_days) == (True, 60, 4, 30)


def test_the_mail_settings_are_read_from_yaml_and_checked(tmp_path):
    from evedm.config import load_settings

    path = tmp_path / "evedm.yaml"
    path.write_text(
        "structures:\n  mail_enabled: false\n  mail_refresh_minutes: 5\n  mail_max_pages: 2\n  mail_max_age_days: 7\n"
    )
    sc = load_settings(path).structures
    assert (sc.mail_enabled, sc.mail_refresh_minutes, sc.mail_max_pages, sc.mail_max_age_days) == (False, 5, 2, 7)
    for field, value in (("mail_refresh_minutes", 0), ("mail_max_pages", 0), ("mail_max_age_days", 0)):
        bad = Settings()
        bad.secret_key = "k"
        bad.demo = True
        setattr(bad.structures, field, value)
        with pytest.raises(ConfigError, match="mail_"):
            bad.validate_for_serve()
        bad.structures.mail_enabled = False  # not checked while switched off
        bad.validate_for_serve()


def test_the_example_config_and_env_file_list_the_mail_scope():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert "esi-mail.read_mail.v1" in (root / "config" / "evedm.example.yaml").read_text(encoding="utf-8")
    assert "esi-mail.read_mail.v1" in (root / ".env.example").read_text(encoding="utf-8")
    assert "mail_max_pages" in (root / "config" / "evedm.example.yaml").read_text(encoding="utf-8")


def test_an_old_database_gets_the_mail_tables_in_place(tmp_path):
    import sqlite3

    from evedm.db import SCHEMA_VERSION, Database

    path = tmp_path / "v19.sqlite3"
    Database(path).close()
    con = sqlite3.connect(path)  # a v19 database: no mail tables
    for table in ("structure_mail_finds", "mail_reads", "scout_mail"):
        con.execute(f"DROP TABLE {table}")  # noqa: S608
    con.execute(
        "INSERT INTO known_structures(structure_id, status, first_seen, last_seen) VALUES(1000000000001,'ok',1,1)"
    )
    con.execute("PRAGMA user_version=19")
    con.commit()
    con.close()
    d = Database(path)
    assert d.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION == 20
    for table in ("structure_mail_finds", "mail_reads", "scout_mail"):
        assert d.query(f"SELECT COUNT(*) FROM {table}")[0][0] == 0  # noqa: S608
    assert d.query("SELECT COUNT(*) FROM known_structures")[0][0] == 1  # existing rows are kept
    d.close()


async def test_the_demo_seeds_two_structures_named_in_mails(tmp_path):
    from evedm.db import Database
    from evedm.scout import seed_demo_structures
    from evedm.universe import GateGraph

    db = Database(tmp_path / "demo.sqlite3")
    graph = GateGraph.load()
    corps: dict[str, int] = {}
    assert seed_demo_structures(db, graph, corps) == 7  # the structures ESI "read": the mail finds are extra
    rows = finds(db)
    assert len(rows) == 2 and sum(1 for r in rows.values() if r["owner_id"]) == 1
    assert {r["system_id"] for r in rows.values()} == {graph.ids[graph.find("Dodixie")], graph.ids[graph.find("Hek")]}
    assert seed_demo_structures(db, graph, corps) == 0 and len(finds(db)) == 2  # seeded once
    db.close()
