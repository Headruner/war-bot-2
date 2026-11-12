"""Notification delay: what ESI says about its freshness, the read log, the lag report, the scheduling and the
event-loop monitor (the delay between a notification's timestamp and its delivery)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time

import httpx
import pytest
from test_notifications import A, B, FakeNotifier, item, push, sight, stamp

from common import add_character
from evedm.cli import main
from evedm.config import Settings
from evedm.db import READS_RETENTION_DAYS, SCHEMA_VERSION, Database
from evedm.esi import EsiError, _expires
from evedm.looplag import LoopLagMonitor
from evedm.notifications import (
    MIN_SLEEP_S,
    READ_MARGIN_S,
    SHORT_REARMS,
    NotificationService,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def ns(settings, services, graph):  # noqa: ANN001, ANN201
    return NotificationService(
        settings.notifications, services["db"], graph, services["chars"], services["esi"], demo=False
    )


def near(got: float, want: float, tol: float = 0.01) -> bool:
    """Epoch seconds: ``pytest.approx`` compares relatively and is far too loose at 1.7e9."""
    return abs(got - want) <= tol


def reads_of(db: Database, cid: int = A) -> list[sqlite3.Row]:
    return db.query("SELECT * FROM notification_reads WHERE character_id=? ORDER BY read_at", (cid,))


def next_wait(db: Database, cid: int = A) -> float:
    return db.notification_checks()[cid]["next_check_at"] - time.time()


def make_stale(esi, cid: int = A) -> None:  # noqa: ANN001
    """ESI's copy of the character's list has expired (the next read is a revalidation)."""
    esi._cache[f"/characters/{cid}/notifications"].expires = 0.0


def add_read(db: Database, cid: int, at: float, **kw) -> None:  # noqa: ANN003
    kw.setdefault("status", 200)
    kw.setdefault("lifetime_s", 600.0)
    kw.setdefault("late_s", 0.0)
    db.save_notification_read(cid, read_at=at, **kw)


def store(db: Database, cid: int, nid: int, delay_s: float, now: float) -> None:
    """A notification that was stored ``delay_s`` seconds after its timestamp (stored just now)."""
    stamp_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - delay_s))
    db.save_notifications(
        [
            {
                "character_id": cid, "notification_id": nid, "type": "StructureUnderAttack", "sender_id": 1,
                "sender_type": "corporation", "timestamp": stamp_at, "body": "", "solar_system_id": None,
                "structure_id": None, "structure_type_id": None,
            }
        ]
    )
    db.execute("UPDATE character_notifications SET created_at=? WHERE notification_id=?", (now, nid))


# --------------------------------------------------------------------------- response metadata (esi.py)
async def test_the_answer_metadata_is_read_for_200_and_304(services, http, fake_app):
    st = fake_app.state.fake
    st.notif_age_s = 40
    await add_character(services, A)
    await push(http, A, [item(1)])
    esi = services["esi"]
    token = await services["chars"].access_token(A, 0)
    path = f"/characters/{A}/notifications"
    assert esi.last_meta(path) is None
    body, meta = await esi.get_with_meta(path, token)
    assert len(body) == 1 and meta.status == 200
    assert 38 <= meta.data_age_s <= 42  # Date - Last-Modified
    assert 598 <= meta.lifetime_s <= 602  # Expires - Date
    assert esi.last_meta(path) is meta
    assert (await esi.get_with_meta(path, token))[1] is None  # a fresh copy: nothing was asked
    make_stale(esi)
    body, again = await esi.get_with_meta(path, token)
    assert len(body) == 1 and again.status == 304 and again is not meta  # revalidated: 304 carries the headers too
    assert 598 <= again.lifetime_s <= 602 and esi.stats["not_modified"] == 1
    assert esi.last_meta(path) is again


def test_a_missing_or_past_expires_means_a_prompt_reread():
    now = time.time()
    # a 304 without Expires and Cache-Control: a few seconds, never the old copy's window
    assert _expires(httpx.Response(304)) - now < 6
    past = httpx.Response(
        200, headers={"Date": "Tue, 10 Nov 2026 12:10:05 GMT", "Expires": "Tue, 10 Nov 2026 12:00:00 GMT"}
    )
    assert _expires(past) - now < 1  # an Expires in the past: expired now, not a window later
    # a cache in between held the copy for 100 s already (Age): 500 s are left of the 600
    aged = httpx.Response(
        200,
        headers={"Date": "Tue, 10 Nov 2026 12:00:00 GMT", "Expires": "Tue, 10 Nov 2026 12:10:00 GMT", "Age": "100"},
    )
    assert abs(_expires(aged) - (now + 500)) < 2


@pytest.mark.parametrize("skew", [-300.0, 300.0])
async def test_a_server_clock_off_by_minutes_still_reads_at_esis_expiry(ns, services, http, fake_app, skew):
    fake_app.state.fake.notif_skew_s = skew  # Date and Expires are minutes away from the host clock
    await add_character(services, A)
    await push(http, A, [item(1)])
    await ns.poll_once()
    assert 598 < next_wait(services["db"]) < 602  # the lifetime is Expires - Date, not Expires - our clock


async def test_an_unusable_expires_is_retried_in_min_interval_not_a_window(ns, services, http, fake_app):
    st = fake_app.state.fake
    await add_character(services, A)
    await push(http, A, [item(1)])
    st.notif_ttl = -30  # a 200 whose Expires is already in the past
    await ns.poll_once()
    assert ns.s.min_interval_s - 2 < next_wait(services["db"]) < ns.s.min_interval_s + 1
    st.notif_ttl = 600  # a good answer, then a 304 that has no Expires at all
    services["db"].set_notification_check(A, 0)
    make_stale(services["esi"])
    await ns.poll_once()
    assert 598 < next_wait(services["db"]) < 602
    st.notif_no_expires = True
    services["db"].set_notification_check(A, 0)
    make_stale(services["esi"])
    await ns.poll_once()
    assert reads_of(services["db"])[-1]["status"] == 304
    assert ns.s.min_interval_s - 2 < next_wait(services["db"]) < ns.s.min_interval_s + 1


# --------------------------------------------------------------------------- the read log
async def test_every_read_is_logged_with_what_esi_said(ns, services, http, fake_app):
    st, db = fake_app.state.fake, services["db"]
    st.notif_age_s = 25
    await add_character(services, A)
    await push(http, A, [item(1, timestamp=stamp(2))])
    db.set_notification_check(A, time.time() - 45)  # it was due 45 s ago
    await ns.poll_once()
    first = reads_of(db)[0]
    assert first["status"] == 200 and first["error"] is None and first["new"] == 1
    assert 23 <= first["data_age_s"] <= 27 and 598 <= first["lifetime_s"] <= 602
    assert 44 <= first["late_s"] <= 47 and 119 <= first["max_delay_s"] <= 123

    db.set_notification_check(A, time.time() - 1)
    make_stale(services["esi"])
    await ns.poll_once()  # nothing new: a 304
    second = reads_of(db)[1]
    assert second["status"] == 304 and second["new"] == 0 and second["max_delay_s"] is None
    assert 0.5 <= second["late_s"] <= 3

    st.notif_status = 403  # a failed read is a row too
    db.set_notification_check(A, 0)
    make_stale(services["esi"])
    await ns.poll_once()
    failed = reads_of(db)[2]
    assert failed["status"] == 0 and "403" in failed["error"] and failed["data_age_s"] is None


async def test_the_error_of_a_failed_read_is_redacted(ns, services, http):
    await add_character(services, A)

    async def boom(cid: int, owner: int) -> int:
        raise EsiError(503, "access_token: abcdefghijklmnopqrstuvwxyz0123456789")

    ns.read_character = boom  # type: ignore[method-assign]
    await ns.poll_once()
    row = reads_of(services["db"])[0]
    assert "abcdefghijkl" not in row["error"] and "503" in row["error"]
    assert "abcdefghijkl" not in services["db"].notification_checks()[A]["error"]


async def test_old_read_rows_are_pruned_with_the_hourly_purge(services):
    db, now = services["db"], time.time()
    add_read(db, A, now - (READS_RETENTION_DAYS + 1) * 86400)
    add_read(db, A, now - (READS_RETENTION_DAYS - 1) * 86400)
    add_read(db, A, now)
    db.purge_notifications(30)
    assert len(reads_of(db)) == 2
    assert min(r["read_at"] for r in reads_of(db)) > now - READS_RETENTION_DAYS * 86400


async def test_the_purge_in_the_poll_loop_prunes_the_reads(ns, services):
    add_read(services["db"], A, time.time() - 30 * 86400)
    await add_character(services, A)
    ns._purged_at = 0.0
    await ns.poll_once()
    assert all(r["read_at"] > time.time() - 86400 for r in reads_of(services["db"]))


def test_an_old_database_gets_the_read_log_in_place(tmp_path):
    path = tmp_path / "v17.sqlite3"
    Database(path).close()
    con = sqlite3.connect(path)  # a v17 database: no notification_reads yet
    con.execute("DROP TABLE notification_reads")
    con.execute("INSERT INTO notification_characters(character_id, next_check_at) VALUES(5, 9.0)")
    con.execute("PRAGMA user_version=17")
    con.commit()
    con.close()
    d = Database(path)
    assert d.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION == 19
    assert d.query("SELECT COUNT(*) FROM notification_reads")[0][0] == 0
    assert d.notification_checks()[5]["next_check_at"] == 9.0  # what was there is kept
    cols = [r[1] for r in d.query("PRAGMA table_info(notification_reads)")]
    assert cols == [
        "character_id", "read_at", "status", "data_age_s", "lifetime_s", "late_s", "new", "max_delay_s", "error"
    ]
    d.close()


# --------------------------------------------------------------------------- the lag report
async def test_the_lag_report_has_percentiles_a_histogram_and_the_split(ns, services):
    db, now = services["db"], time.time()
    await add_character(services, A)
    await add_character(services, B)
    ns.s.min_structures = 1
    sight(db, A, 3)  # B has no structure: registered but not read
    for i, (status, age, late) in enumerate([(200, 10.0, 0.0), (304, 20.0, 0.0), (304, 30.0, 0.0), (200, 40.0, 100.0)]):
        add_read(db, A, now - 3600 + i * 600, status=status, data_age_s=age, late_s=late, new=1 if i != 1 else 0)
    add_read(db, A, now - 100, status=0, error="ESI 503")
    # delays: 60 and 300 (on time, read #0 and #2), 900 (read #3 was 100 s late), 90000 is history
    store(db, A, 1, 60, now - 3600 + 1)
    store(db, A, 2, 300, now - 2400 + 1)
    store(db, A, 3, 900, now - 1800 + 1)
    store(db, A, 4, 90000, now - 1800 + 2)
    store(db, A, 5, 5000, now - 1800 + 3)  # matched, but older than two windows: backlog, not in the headline
    store(db, B, 6, 100, now - 60)  # B has no read in the log: unmatched, not in the headline
    out = ns.lag(24, now)
    assert out["reads"] == {"n": 5, "200": 2, "304": 2, "no_request": 0, "errors": 1}
    assert out["data_age_s"]["p50"] == 30.0 and out["data_age_s"]["max"] == 40.0 and out["data_age_s"]["n"] == 4
    assert out["lifetime_s"]["p50"] == 600.0 and out["late_s"]["max"] == 100.0
    d = out["delivery_delay_s"]
    assert d["n"] == 3 and near(d["p50"], 300, 1.5) and near(d["max"], 900, 1.5) and d["history_ignored"] == 1
    hist = d["histogram"]
    assert hist["0-120"] == 1 and hist["240-360"] == 1 and hist["840-960"] == 1
    assert sum(hist.values()) == 3 and len(hist) == 10 and list(hist)[-1] == "1080-1200"  # up to two windows
    assert d["backlog"] == 1 and d["unmatched"] == 1 and d["max_delay_s"] == 1200.0
    assert out["data_age_s"]["ge_300"] == {"n": 0, "share": 0.0}
    assert d["read_on_time"]["n"] == 2 and d["read_late"]["n"] == 1 and near(d["read_late"]["max"], 900, 1.5)
    assert out["characters"]["registered"] == 2 and out["characters"]["eligible"] == 1
    assert out["characters"]["excluded_by_gate"] == [B] and out["characters"]["min_structures"] == 1
    assert out["characters"]["not_read_last_hour"] == []  # A was read 10 minutes ago without error
    json.dumps(out)  # it is printed by the CLI
    brief = ns.lag_brief()
    assert brief["excluded_by_gate"] == 1 and brief["eligible"] == 1 and brief["reads"] == 5


async def test_the_lag_report_names_the_biggest_component(ns, services):
    db, now = services["db"], time.time()
    await add_character(services, A)
    assert ns.lag(1, now)["reading"]["dominant"] == "no data"
    for i in range(10):  # reads on time, data fresh: ESI's window
        add_read(db, A, now - 3000 + i * 300, status=304, data_age_s=2.0)
    few = ns.lag(1, now)["reading"]  # no notification in the headline yet: no conclusion
    assert few["dominant"] == "too few samples" and "too few samples: 0" in few["text"]
    for n in range(20):
        store(db, A, 100 + n, 100 + n, now - 200)  # 20 matched notifications within two windows
    assert ns.lag(1, now)["reading"]["dominant"] == "within the cache window"
    for i in range(3):  # a third of the reads late
        add_read(db, A, now - 100 + i, late_s=200.0)
    r = ns.lag(1, now)["reading"]
    assert r["dominant"] == "late reads" and r["thresholds"]["late_s"] == 30.0
    db.execute("DELETE FROM notification_reads")
    for i in range(10):
        add_read(db, A, now - 3000 + i * 300, status=304, data_age_s=240.0)
    assert ns.lag(1, now)["reading"]["dominant"] == "ESI data age"
    for i in range(3):
        add_read(db, A, now - 100 + i, status=0, error="ESI 503")
    assert ns.lag(1, now)["reading"]["dominant"] == "errors"
    assert "3 of 13" in ns.lag(1, now)["reading"]["text"]


async def test_the_lag_report_lists_the_characters_not_read_in_the_last_hour(ns, services):
    db, now = services["db"], time.time()
    await add_character(services, A)
    await add_character(services, B)
    add_read(db, A, now - 5000)  # A was read, but not within the hour
    add_read(db, B, now - 60)
    out = ns.lag(24, now)["characters"]
    assert out["not_read_last_hour"] == [A] and out["eligible"] == 2


async def test_the_headline_counts_only_matched_notifications_within_two_windows(ns, services):
    db, now = services["db"], time.time()
    await add_character(services, A)
    add_read(db, A, now - 3000)
    for n in range(25):
        store(db, A, 200 + n, 50 + n * 10, now - 2000 + n)  # delays 50..290 s: the headline
    for n in range(5):
        store(db, A, 300 + n, 14000 + n, now - 1500 + n)  # a character not read for hours: backlog
    store(db, A, 400, 1199, now - 1400)  # right under two windows: still the headline
    store(db, A, 401, 1250, now - 1300)  # right over: backlog
    store(db, A, 410, 200000, now - 1200)  # more than a day: history, nothing else
    store(db, B, 420, 70, now - 1100)  # a character without a read in the log: unmatched
    out = ns.lag(2, now)["delivery_delay_s"]
    assert out["n"] == 26 and out["backlog"] == 6 and out["unmatched"] == 1 and out["history_ignored"] == 1
    assert out["p50"] < 300 and near(out["max"], 1199, 1.5)  # the 14 000 s backlog does not reach the percentiles
    assert sum(out["histogram"].values()) == 26 and out["read_on_time"]["n"] == 26 and out["read_late"]["n"] == 0
    brief = ns.lag_brief(2)
    assert brief["delay_p50_s"] == out["p50"] and brief["delay_samples"] == 26 and brief["backlog"] == 6
    assert ns.lag(2, now)["reading"]["dominant"] == "within the cache window"  # 26 samples: enough


async def test_a_conclusion_from_reads_still_carries_the_sample_warning(ns, services):
    db, now = services["db"], time.time()
    await add_character(services, A)
    for i in range(5):
        add_read(db, A, now - 100 + i, status=0, error="ESI 503")
    r = ns.lag(1, now)["reading"]
    assert r["dominant"] == "errors" and r["text"].endswith("(delay figures: too few samples: 0)")


async def test_reads_with_old_data_are_counted_and_a_recurring_pattern_is_named(ns, services):
    db, now = services["db"], time.time()
    await add_character(services, A)
    for i in range(20):
        add_read(db, A, now - 3000 + i * 100, status=304, data_age_s=2.0)
    out = ns.lag(1, now)
    assert out["data_age_s"]["ge_300"] == {"n": 0, "share": 0.0} and out["reading"]["dominant"] != "ESI data age"
    add_read(db, A, now - 50, status=200, data_age_s=600.0)  # the one read with data age 600 seen in production
    out = ns.lag(1, now)
    assert out["data_age_s"]["ge_300"] == {"n": 1, "share": round(1 / 21, 4)}  # 4.8 %: under 5 %, one is no pattern
    assert out["reading"]["dominant"] != "ESI data age"
    add_read(db, A, now - 40, status=200, data_age_s=310.0)  # a second one: 9.5 %, a pattern
    out = ns.lag(1, now)
    assert out["data_age_s"]["ge_300"]["n"] == 2 and out["reading"]["dominant"] == "ESI data age"
    assert "2 of 22 reads" in out["reading"]["text"] and ns.lag_brief(1)["data_age_ge_300"] == 2


def test_the_lag_command_prints_json_and_leaves_the_database_alone(tmp_path, capsys):
    data = tmp_path / "data"
    db = Database(data / "evedm.sqlite3")
    add_read(db, A, time.time() - 60, status=304, data_age_s=3.0)
    db.close()
    assert main(["--data-dir", str(data), "notifications", "lag", "--hours", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hours"] == 2.0 and out["reads"]["n"] == 1 and out["reads"]["304"] == 1
    assert set(out) >= {"data_age_s", "lifetime_s", "late_s", "delivery_delay_s", "characters", "reading"}
    # an old database that was not migrated: no table, nothing written, no failure
    con = sqlite3.connect(data / "evedm.sqlite3")
    con.execute("DROP TABLE notification_reads")
    con.execute("PRAGMA user_version=17")
    con.commit()
    con.close()
    assert main(["--data-dir", str(data), "notifications", "lag"]) == 0
    assert json.loads(capsys.readouterr().out)["reads"]["n"] == 0
    con = sqlite3.connect(data / "evedm.sqlite3")
    assert con.execute("PRAGMA user_version").fetchone()[0] == 17
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='notification_reads'").fetchone()
    con.close()


# --------------------------------------------------------------------------- the structure gate
def test_the_default_reads_every_character_with_a_structure():
    assert Settings().notifications.min_structures == 1


async def test_the_gate_count_and_the_log_line_follow_the_eligible_set(settings, services, graph, caplog):
    settings.notifications.min_structures = 1
    ns = NotificationService(settings.notifications, services["db"], graph, services["chars"], services["esi"])
    await add_character(services, A)
    await add_character(services, B)
    sight(services["db"], A, 1)  # one structure is enough
    with caplog.at_level(logging.INFO, logger="evedm.notifications"):
        assert set(ns.eligible()) == {A} and ns.excluded_by_gate() == [B]
        ns.eligible()  # same size again: no second line
        lines = [r.getMessage() for r in caplog.records if "reading" in r.getMessage()]
        assert len(lines) == 1 and "1 of 2" in lines[0] and "min_structures=1" in lines[0]
        sight(services["db"], B, 1)
        assert set(ns.eligible()) == {A, B} and ns.excluded_by_gate() == []
        lines = [r.getMessage() for r in caplog.records if "reading" in r.getMessage()]
        assert len(lines) == 2 and "2 of 2" in lines[1]


# --------------------------------------------------------------------------- scheduling
async def test_a_short_remaining_lifetime_is_followed_but_not_forever(ns, services):
    now = time.time()
    await add_character(services, A)
    ns._expires[A] = now + 4  # ESI says: stale for 4 more seconds
    for _ in range(SHORT_REARMS):
        assert near(ns.next_check_at(A, now), now + 4 + READ_MARGIN_S)
    # ESI keeps saying "a moment" and nothing changes: back to min_interval_s instead of a hot loop
    assert near(ns.next_check_at(A, now), now + ns.s.min_interval_s)
    ns._expires[A] = now + 400  # a real window resets the count
    ns.next_check_at(A, now)
    ns._expires[A] = now + 4
    assert near(ns.next_check_at(A, now), now + 4 + READ_MARGIN_S)


async def test_the_next_read_waits_for_the_expiry_and_nothing_else(ns, services):
    now = time.time()
    await add_character(services, A)
    ns._expires[A] = now + 600
    assert near(ns.next_check_at(A, now), now + 600 + READ_MARGIN_S) and READ_MARGIN_S <= 0.25


async def test_the_loop_wakes_when_the_next_character_is_due_not_a_second_later(ns, services):
    await add_character(services, A)
    await add_character(services, B)
    db = services["db"]
    db.set_notification_check(A, time.time() + 30.4)
    db.set_notification_check(B, time.time() + 12.34)
    assert 12.2 < ns.sleep_s() <= 12.34  # the earliest of them, to the tenth of a second
    db.set_notification_check(B, time.time() + 0.3)
    assert 0.1 < ns.sleep_s() <= 0.3  # was: never under a second
    db.execute("DELETE FROM notification_characters WHERE character_id=?", (B,))
    assert ns.sleep_s() == MIN_SLEEP_S  # B was never read: due now (was: ignored until A is due)


async def test_the_stagger_never_pushes_a_read_past_stagger_s_and_settles(ns):
    ns.s.stagger_s = 60
    for count, seed in ((11, 1), (32, 4)):
        rnd = random.Random(seed)
        plan = {c: rnd.uniform(0, 600) for c in range(count)}
        last: dict[int, float] = {}
        gaps: list[float] = []
        t = 0.0
        while t < 4 * 86400:
            c = min(plan, key=plan.__getitem__)
            t = plan[c]
            base = t + 600 + READ_MARGIN_S  # ESI's window restarts at the read
            nxt = ns.staggered(c, base, [v for k, v in plan.items() if k != c])
            assert 0 <= nxt - base <= ns.s.stagger_s  # past the expiry by stagger_s at most, every single time
            if c in last and t > 3 * 86400:
                gaps.append(t - last[c])
            last[c], plan[c] = t, nxt
        assert max(gaps) < 600 + 2 * READ_MARGIN_S + 1  # spread out: reads are a window apart again, no drift


async def test_a_short_rearm_is_exact_and_not_staggered(ns, services, http, fake_app):
    await add_character(services, A)
    await add_character(services, B)
    await push(http, A, [item(1)])
    fake_app.state.fake.notif_ttl = 3  # ESI says the copy lives 3 s: ask again then, never a stagger later
    services["db"].set_notification_check(B, time.time() + 1.3)  # a neighbour just before A: would push it 60 s later
    await ns.poll_once()
    assert 2 < next_wait(services["db"], A) < 4.5


async def test_a_restart_keeps_the_saved_schedule(settings, services, graph, http, fake_app):
    await add_character(services, A)
    await push(http, A, [item(1)])
    db, now = services["db"], time.time()
    db.set_notification_check(A, now + 200)  # saved by the process that ran before
    fresh = NotificationService(settings.notifications, db, graph, services["chars"], services["esi"])
    assert fresh._expires == {}  # nothing known about ESI's cache after a restart
    assert await fresh.poll_once() == 0  # not due: no request, no new window
    assert fake_app.state.fake.calls.get("notifications", 0) == 0
    assert near(fresh.next_check_at(A, now), now + 200)  # not a full window from now (was: +600)
    # due after the restart: one read, then ESI's own expiry decides, not an extra window
    db.set_notification_check(A, now - 1)
    assert await fresh.poll_once() == 1
    assert 598 < next_wait(db) < 602


async def test_the_error_limit_is_retried_when_it_ends(ns, services):
    await add_character(services, A)
    await add_character(services, B)

    async def limited(cid: int, owner: int) -> int:
        raise EsiError(420, "error limited, retry in 7 s")

    ns.read_character = limited  # type: ignore[method-assign]
    ns.esi.blocked_for = lambda reserve=5: 7.0  # type: ignore[method-assign]
    await ns.poll_once()
    for cid in (A, B):
        assert 7 < next_wait(services["db"], cid) < 8  # exactly the block (was: retry_s = 60 s)
    ns.esi.blocked_for = lambda reserve=5: 0.0  # type: ignore[method-assign]
    services["db"].set_notification_check(A, 0)
    await ns.poll_once()
    assert 0.5 < next_wait(services["db"], A) < 2  # the block is over already: try again at once


# --------------------------------------------------------------------------- Discord is not held back
async def test_discord_gets_a_character_as_soon_as_it_is_read_not_after_the_round(ns, services, http):
    await add_character(services, A)
    await add_character(services, B)
    ns.notifier = FakeNotifier()
    await push(http, A, [item(1, timestamp=stamp())])
    await push(http, B, [item(2, "StructureItemsMovedToSafety", timestamp=stamp())])
    release = asyncio.Event()
    original = ns.read_character

    async def slow_b(cid: int, owner: int) -> int:
        if cid == B:
            await release.wait()  # B's read hangs (a slow ESI answer)
        return await original(cid, owner)

    ns.read_character = slow_b  # type: ignore[method-assign]
    task = asyncio.ensure_future(ns.poll_once(post=True))
    for _ in range(200):
        if ns.notifier.sent:
            break
        await asyncio.sleep(0.01)
    assert len(ns.notifier.sent) == 1 and not task.done()  # A's message is out while B is still being read
    assert "under attack" in ns.notifier.sent[0]["embeds"][0]["title"]
    release.set()
    assert await task == 2
    assert len(ns.notifier.sent) == 2  # B follows as soon as it is stored; nothing posted twice
    assert services["db"].pending_notification_posts() == []


async def test_a_failed_post_is_not_hammered_by_every_read_and_is_retried_when_due(ns, services, http):
    from evedm.wars.discord import NotifyError

    await add_character(services, A)
    await add_character(services, B)

    class Down(FakeNotifier):
        attempts = 0

        async def send(self, payload: dict) -> dict:
            Down.attempts += 1
            await asyncio.sleep(0.05)  # both reads are done and waiting to post by the time this fails
            raise NotifyError("Discord error 500")

    ns.notifier = Down()
    await push(http, A, [item(1, timestamp=stamp())])
    await push(http, B, [item(2, "StructureItemsMovedToSafety", timestamp=stamp())])
    assert await ns.poll_once(post=True) == 2
    assert Down.attempts == 1  # the second character's read did not try again within retry_s
    assert ns._post_pending and ns._post_retry_at > time.time()
    assert ns.sleep_s() <= ns.s.retry_s  # the loop wakes for the retry
    ns.notifier = FakeNotifier()
    assert await ns.post_discord() == 2  # the rows were kept: nothing lost
    assert ns._post_retry_at == 0.0


# --------------------------------------------------------------------------- event-loop monitor
class FakeClock:
    def __init__(self, extras: list[float]):
        self.now = 1000.0
        self.extras = list(extras)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if not self.extras:
            raise asyncio.CancelledError
        self.now += seconds + self.extras.pop(0)  # the loop woke this much late


async def test_the_loop_monitor_keeps_max_and_p99_and_warns_once_a_minute(caplog):
    clock = FakeClock([0.0, 0.1, 3.0, 0.2, 2.5, 0.0])
    mon = LoopLagMonitor(clock=clock, sleep=clock.sleep)
    with caplog.at_level(logging.WARNING, logger="evedm.looplag"):
        with pytest.raises(asyncio.CancelledError):
            await mon.run()
    snap = mon.snapshot()
    assert snap["samples"] == 6 and near(snap["max_s"], 3.0, 0.002) and near(snap["p99_s"], 3.0, 0.002)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "3.0 s" in warnings[0].getMessage()  # the 2.5 s one is inside the same minute
    assert snap["warnings"] == 1
    clock.now += 61  # a minute later: a new long block warns again
    mon.record(2.6)
    assert mon.warnings == 2
    mon.record(1.9)  # under the 2 s threshold: no warning
    assert mon.warnings == 2


async def test_the_loop_monitor_forgets_what_is_older_than_ten_minutes():
    now = [0.0]
    mon = LoopLagMonitor(clock=lambda: now[0])
    mon.record(5.0, 0.0)
    for t in range(1, 11):
        mon.record(0.01, t * 60.0 + 1)
    assert mon.snapshot()["max_s"] < 1  # the 5 s block of ten minutes ago is gone
    assert mon.snapshot()["samples"] == 10
    assert LoopLagMonitor().snapshot() == {"max_s": 0.0, "p99_s": 0.0, "samples": 0, "window_s": 600.0, "warnings": 0}


async def test_the_loop_monitor_measures_a_blocked_loop():
    mon = LoopLagMonitor(interval_s=0.02)
    mon.start()
    await asyncio.sleep(0.05)
    time.sleep(0.3)  # a synchronous section on the loop
    await asyncio.sleep(0.05)
    await mon.stop()
    assert mon.max_s() > 0.2


def test_health_shows_the_loop_lag_and_the_notification_lag(tmp_path):
    from fastapi.testclient import TestClient
    from test_notifications import web_settings

    from evedm.wars.config import WarsSettings
    from evedm.web.app import create_app

    with TestClient(create_app(web_settings(tmp_path, []), WarsSettings(enabled=False))) as c:
        health = c.get("/api/health").json()
    assert set(health["event_loop"]) == {"max_s", "p99_s", "samples", "window_s", "warnings"}
    lag = health["notifications"]["lag"]
    assert lag["reading"] == "no data" and "excluded_by_gate" in lag and "eligible" in lag


# --------------------------------------------------------------------------- long synchronous sections
async def test_the_dataset_parsing_runs_off_the_event_loop(settings, services, graph, monkeypatch):
    import threading

    from evedm.scout import StructureScout

    scout = StructureScout(settings, services["db"], graph, services["chars"], services["esi"])
    seen: list[int] = []
    real = StructureScout._parse_adam4eve

    def spy(content: bytes, wanted: set[int]) -> list:
        seen.append(threading.get_ident())
        return real(content, wanted)

    monkeypatch.setattr(StructureScout, "_parse_adam4eve", staticmethod(spy))

    class Static:
        async def get(self, url: str, timeout: float = 0) -> httpx.Response:
            body = b"structureID;name;ownerID;lastSeen\n1030000000001;Home;5;2026-01-01\n1030000000002;Other;6;x\n"
            return httpx.Response(200, content=body, request=httpx.Request("GET", url))

    services["esi"].http = Static()  # type: ignore[assignment]
    rows = await scout._fetch_adam4eve({1030000000001})
    assert [r[0] for r in rows] == [1030000000001]
    assert seen and seen[0] != threading.get_ident()  # parsed in a worker thread: the loop stayed free
