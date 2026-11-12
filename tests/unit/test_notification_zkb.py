"""Structure type in every notification and the zKillboard check of asset safety notifications (NTF-27..NTF-33)."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
from test_notifications import A, B, FakeNotifier, item, push, stamp

from common import AMARR, JITA, add_character
from evedm.cli import main
from evedm.db import SCHEMA_VERSION, Database
from evedm.notifications import NotificationService, parse_body, structure_of
from evedm.zkb_check import ZkbApi
from fake_eve import FakeZkbApi

pytestmark = pytest.mark.anyio

MOVED = "StructureItemsMovedToSafety"
ASTRAHUS, FORTIZAR = 35832, 35833
FORT_BODY = f"solarsystemID: {AMARR}\nstructureID: 1035000000002\nstructureTypeID: {FORTIZAR}\n"
BODY = f"solarsystemID: {AMARR}\nstructureID: 1035000000001\nstructureTypeID: {ASTRAHUS}\n"


@pytest.fixture
def ns(settings, services, graph):  # noqa: ANN001, ANN201
    s = NotificationService(
        settings.notifications, services["db"], graph, services["chars"], services["esi"], demo=False
    )
    services["db"].save_type_names({ASTRAHUS: "Astrahus", FORTIZAR: "Fortizar"})
    return s


@pytest.fixture
def zkb(ns):  # noqa: ANN001, ANN201
    fake = FakeZkbApi()
    ns.zkb = ZkbApi("https://zkb.test/api", "EVE-Distance-Meter/test", min_gap_s=10, transport=fake.transport)
    return fake


def epoch(text: str) -> float:
    return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone


def iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


async def moved(ns, services, http, nid: int = 1, cid: int = A, minutes_ago: float = 1, body: str = BODY,  # noqa: ANN001
                ntype: str = MOVED, when: str | None = None):  # noqa: ANN001
    """Push one notification through ESI, read it and return its stored row."""
    when = when or stamp(minutes_ago)
    await push(http, cid, [item(nid, ntype, text=body, timestamp=when)])
    services["db"].set_notification_check(cid, 0)
    services["esi"]._cache.clear()
    await ns.poll_once()
    return services["db"].notification_row(cid, nid)


def check_of(ns, row):  # noqa: ANN001, ANN201
    return ns.db.zkb_check(ns.event_key(row))


def feed_kill(db: Database, kid: int, at: float, system: int = AMARR, type_id: int = ASTRAHUS) -> None:
    db.execute(
        "INSERT INTO structure_alerts(kind, killmail_id, killmail_hash, killmail_time, solar_system_id, "
        "structure_type_id, created_at) VALUES('destroyed',?,?,?,?,?,?)",
        (kid, f"h{kid}", iso(at), system, type_id, time.time()),
    )


def api_kill(kid: int, at: float | None = None, type_id: int = ASTRAHUS, system: int = AMARR) -> dict:
    """An entry of zKillboard's list; with ``at`` it carries the killmail as well (time, victim, system)."""
    entry: dict = {"killmail_id": kid, "zkb": {"hash": f"h{kid}", "totalValue": 1.0}}
    if at is not None:
        entry |= {"killmail_time": iso(at), "solar_system_id": system, "victim": {"ship_type_id": type_id}}
    return entry


# --------------------------------------------------------------------------- the structure type of a notification
def test_the_structure_type_is_read_where_the_game_puts_it():
    assert structure_of(MOVED, parse_body(BODY)) == (1035000000001, ASTRAHUS)
    info = parse_body("structureShowInfoData:\n- showinfo\n- 35826\n- 1035000000009\nsolarsystemID: 30002187\n")
    assert structure_of("StructureUnderAttack", info) == (1035000000009, 35826)
    link = parse_body('structureID: 1035000000002\nstructureLink: <a href="showinfo:35827//1035000000002">Keep</a>\n')
    assert structure_of(MOVED, link) == (1035000000002, 35827)
    # typeID is the structure for towers and attacked orbitals, but not for every notification
    assert structure_of("TowerAlertMsg", {"typeID": 16213, "solarSystemID": 1}) == (None, 16213)
    assert structure_of("SkyhookUnderAttack", {"typeID": 81080}) == (None, 81080)
    assert structure_of("InsurancePayoutMsg", {"typeID": 587}) == (None, None)
    assert structure_of(MOVED, {"structureID": 5}) == (5, None)


async def test_a_stored_notification_carries_the_type_of_its_body(ns, services, http):
    await add_character(services, A)
    shape = f"solarsystemID: {AMARR}\nstructureShowInfoData: [showinfo, {FORTIZAR}, 1035000000007]\n"
    row = await moved(ns, services, http, 1, body=shape, ntype="StructureUnderAttack")
    assert row["structure_id"] == 1035000000007 and row["structure_type_id"] == FORTIZAR
    d = ns.describe(row)
    assert d["structure_type"] == "Fortizar" and d["show_structure_type"] is True


async def test_the_type_falls_back_to_the_scout_then_to_the_public_history(ns, services, http):
    await add_character(services, A)
    db = services["db"]
    bare = f"solarsystemID: {AMARR}\nstructureID: 1035000000003\n"  # the body names no type
    row = await moved(ns, services, http, 1, body=bare, ntype="StructureUnderAttack")
    assert row["structure_type_id"] is None and ns.describe(row)["structure_type"] is None
    db.execute("INSERT INTO public_structure_history(structure_id, type_id, type_name) VALUES(?,?,?)",
               (1035000000003, FORTIZAR, "Fortizar"))
    assert ns.describe(row)["structure_type"] == "Fortizar"  # public history
    now = time.time()
    db.execute("INSERT INTO known_structures(structure_id, type_id, first_seen, last_seen) VALUES(?,?,?,?)",
               (1035000000003, ASTRAHUS, now, now))
    assert ns.describe(row)["structure_type"] == "Astrahus"  # the scout's find comes first


async def test_a_row_stored_before_the_type_was_read_is_derived_from_its_body_when_viewed(ns, services):
    db = services["db"]
    shape = f"solarsystemID: {AMARR}\nstructureShowInfoData: [showinfo, {FORTIZAR}, 1035000000008]\n"
    db.save_notifications([{
        "character_id": A, "notification_id": 5, "type": "StructureDestroyed", "timestamp": stamp(),
        "body": shape, "solar_system_id": AMARR, "structure_id": 1035000000008, "structure_type_id": None,
    }])
    row = db.notification_row(A, 5)
    assert ns.describe(row)["structure_type"] == "Fortizar"
    assert db.notification_row(A, 5)["structure_type_id"] is None  # nothing is written back


async def test_unknown_type_is_unknown_on_the_page_and_absent_on_discord(ns, services, http):
    await add_character(services, A)
    ns.notifier = FakeNotifier()
    bare = f"solarsystemID: {AMARR}\nstructureID: 1035000000004\n"
    row = await moved(ns, services, http, 1, body=bare, ntype="StructureUnderAttack")
    d = ns.describe(row)
    assert d["show_structure_type"] is True and d["structure_type"] is None  # the page prints "Unknown"
    names = [f["name"] for f in ns.embed_payload(row, ["Alpha"])["embeds"][0]["fields"]]
    assert "Structure type" not in names
    # no structure and no type: no field at all
    none = await moved(ns, services, http, 2, body=f"solarsystemID: {AMARR}\n", ntype="StructureUnderAttack")
    assert ns.describe(none)["show_structure_type"] is False
    assert "Structure type" not in [f["name"] for f in ns.embed_payload(none, ["Alpha"])["embeds"][0]["fields"]]


async def test_discord_names_the_structure_type(ns, services, http):
    await add_character(services, A)
    row = await moved(ns, services, http, 1, ntype="StructureUnderAttack")
    fields = {f["name"]: f["value"] for f in ns.embed_payload(row, ["Alpha"])["embeds"][0]["fields"]}
    assert fields["Structure type"] == "Astrahus"


async def test_unknown_type_names_are_asked_from_esi_in_one_batch(ns, services, http):
    await add_character(services, A)
    db = services["db"]
    db.execute("DELETE FROM type_names")
    other = f"solarsystemID: {AMARR}\nstructureID: 1035000000005\nstructureTypeID: 35826\n"
    r1 = await moved(ns, services, http, 1, ntype="StructureUnderAttack")
    r2 = await moved(ns, services, http, 2, body=other, ntype="StructureUnderAttack")
    assert ns.describe(r1)["structure_type"] is None  # viewing never calls ESI
    posts = []
    real = services["esi"].post

    async def spy(path, body, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
        posts.append((path, list(body)))
        return await real(path, body, *a, **kw)

    services["esi"].post = spy
    ns.view_context([r1, r2])
    assert await ns.resolve_type_names() == 2
    assert [p for p, _ in posts] == ["/universe/names"] and sorted(posts[0][1]) == [35826, ASTRAHUS]
    assert ns.describe(r1)["structure_type"] == "Type 35832"  # the fake ESI's name
    assert await ns.resolve_type_names() == 0 and len(posts) == 1  # remembered


# --------------------------------------------------------------------------- which notifications are checked
async def test_only_the_configured_type_is_checked(ns, services, http):
    await add_character(services, A)
    assert ns.s.zkb_check.types == [MOVED]
    for nid, ntype in enumerate(["StructureUnderAttack", "StructureItemsDelivered", "StructureDestroyed", MOVED], 1):
        await moved(ns, services, http, nid, body=BODY.replace("1035000000001", f"10350000000{nid:02d}"), ntype=ntype)
    rows = services["db"].query("SELECT notification_type, status FROM notification_zkb")
    assert [(r[0], r[1]) for r in rows] == [(MOVED, "pending")]
    ns.s.zkb_check.types = ["StructureItemsDelivered"]  # configurable
    await moved(ns, services, http, 9, ntype="StructureItemsDelivered", when=stamp(0.5))
    assert services["db"].query("SELECT COUNT(*) FROM notification_zkb")[0][0] == 2


async def test_a_disabled_check_stores_nothing(ns, services, http):
    await add_character(services, A)
    ns.s.zkb_check.enabled = False
    row = await moved(ns, services, http)
    assert check_of(ns, row) is None and ns.describe(row)["zkb"] is None


async def test_what_cannot_be_checked_is_skipped_and_history_is_not_checked(ns, services, http):
    await add_character(services, A)
    old = await moved(ns, services, http, 1, when=stamp(60 * 5))  # an hour old on its first read
    assert check_of(ns, old)["status"] == "skipped" and "history" in check_of(ns, old)["error"]
    bare = await moved(ns, services, http, 2, body=f"solarsystemID: {AMARR}\nstructureID: 1035000000006\n")
    assert check_of(ns, bare)["status"] == "skipped" and "type" in check_of(ns, bare)["error"]
    assert ns.describe(bare)["zkb"] is None  # nothing to say about a check that was not made


# --------------------------------------------------------------------------- the feed
async def test_the_feed_confirms_a_kill_inside_the_window_only(ns, services, http, zkb):
    await add_character(services, A)
    db = services["db"]
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    at = epoch(when)
    feed_kill(db, 1, at + 301)  # just outside
    feed_kill(db, 2, at - 301)
    feed_kill(db, 3, at + 10, system=JITA)  # another system
    feed_kill(db, 4, at + 10, type_id=FORTIZAR)  # another structure type
    c = check_of(ns, row)
    assert await ns.zkb_step(now=c["received_at"]) == 1
    assert check_of(ns, row)["status"] == "pending" and zkb.requests  # the API was asked, nothing found
    feed_kill(db, 5, at + 299)
    assert await ns.zkb_step(now=c["received_at"] + 60) == 1
    got = check_of(ns, row)
    assert got["status"] == "found" and got["killmail_id"] == 5 and got["source"] == "feed"
    assert got["zkb_url"] == "https://zkillboard.com/kill/5/" and got["checks"] == 2
    assert got["next_check_at"] is None


async def test_the_closest_kill_wins(ns, services, http, zkb):
    await add_character(services, A)
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    at = epoch(when)
    feed_kill(services["db"], 7, at + 200)
    feed_kill(services["db"], 8, at + 20)
    await ns.zkb_step(now=check_of(ns, row)["received_at"])
    assert check_of(ns, row)["killmail_id"] == 8 and zkb.requests == []  # the feed answered: no API request


# --------------------------------------------------------------------------- the API
async def test_the_api_confirms_a_late_kill_and_names_the_request(ns, services, http, zkb):
    await add_character(services, A)
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    at = epoch(when)
    zkb.kills = [api_kill(11, at + 30), api_kill(12, at + 900)]  # the second is far outside the window
    c = check_of(ns, row)
    await ns.zkb_step(now=c["received_at"])
    got = check_of(ns, row)
    assert got["status"] == "found" and got["killmail_id"] == 11 and got["source"] == "api"
    url = str(zkb.requests[0].url)
    assert url.startswith(f"https://zkb.test/api/kills/systemID/{AMARR}/shipTypeID/{ASTRAHUS}/pastSeconds/")
    assert url.endswith("/") and zkb.requests[0].headers["user-agent"] == "EVE-Distance-Meter/test"
    past = int(url.rstrip("/").rsplit("/", 1)[1])
    assert 300 <= past <= 300 + 90  # from the start of the window to now, a minute after the notification


async def test_a_list_without_times_is_resolved_through_esi_a_few_at_a_time(ns, services, http, zkb):
    await add_character(services, A)
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    at = epoch(when)
    ns.s.zkb_check.max_resolve = 2
    kills = {21: at + 2000, 22: at + 3000, 23: at + 40}  # only the third is inside the window
    await http.post("/_admin/r2z2", json={"killmails": [
        {"killmail_id": k, "hash": f"h{k}", "killmail": {
            "killmail_time": iso(t), "solar_system_id": AMARR, "victim": {"ship_type_id": ASTRAHUS}}}
        for k, t in kills.items()]})
    zkb.kills = [api_kill(k) for k in kills]
    c = check_of(ns, row)
    await ns.zkb_step(now=c["received_at"])
    assert check_of(ns, row)["status"] == "pending"  # two were read, the matching one was the third
    ns.s.zkb_check.max_resolve = 3
    await ns.zkb_step(now=c["received_at"] + 60)
    got = check_of(ns, row)
    assert got["status"] == "found" and got["killmail_id"] == 23 and got["killmail_time"] == iso(at + 40)


async def test_an_api_failure_is_recorded_and_the_check_goes_on_with_the_feed(ns, services, http, zkb):
    await add_character(services, A)
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    c = check_of(ns, row)
    zkb.broken = True
    await ns.zkb_step(now=c["received_at"])
    got = check_of(ns, row)
    assert got["status"] == "pending" and "ConnectError" in got["error"] and got["checks"] == 1
    zkb.broken, zkb.status = False, 500
    await ns.zkb_step(now=c["received_at"] + 60)
    assert "HTTP 500" in check_of(ns, row)["error"]
    feed_kill(services["db"], 31, epoch(when) + 5)  # the feed still decides
    await ns.zkb_step(now=c["received_at"] + 120)
    got = check_of(ns, row)
    assert got["status"] == "found" and got["source"] == "feed"
    zkb.status, zkb.body = 200, {"error": "odd"}
    services["db"].execute("DELETE FROM structure_alerts")
    other = await moved(ns, services, http, 2, body=BODY.replace("1035000000001", "1035000000002"))
    await ns.zkb_step(now=check_of(ns, other)["received_at"] + 130)
    assert "unexpected" in check_of(ns, other)["error"]


async def test_one_api_request_per_gap_for_all_checks(ns, services, http, zkb):
    await add_character(services, A)
    rows = [await moved(ns, services, http, n, body=BODY.replace("1035000000001", f"10350000000{n:02d}"),
                        when=stamp(1)) for n in (1, 2, 3)]
    # three structures of the same type in one system and minute are one event key per structure: three checks
    keys = {ns.event_key(r) for r in rows}
    assert len(keys) == 3
    t0 = max(check_of(ns, r)["received_at"] for r in rows) + 1
    assert await ns.zkb_step(now=t0) == 3
    assert len(zkb.requests) == 1  # the others waited for the gap and used the feed alone
    assert ns.zkb.ready_in(t0 + 1) > 8 and ns.zkb.ready_in(t0 + 11) == 0
    assert await ns.zkb_step(now=t0 + 60) == 3
    assert len(zkb.requests) == 2  # a minute later: again one request for the three of them


async def test_a_refusal_pauses_the_api_for_ten_minutes(ns, services, http, zkb):
    await add_character(services, A)
    row = await moved(ns, services, http)
    t0 = check_of(ns, row)["received_at"]
    zkb.status = 429
    await ns.zkb_step(now=t0)
    assert "HTTP 429" in check_of(ns, row)["error"] and ns.zkb.stats["blocked"] == 1
    zkb.status = 200
    for step in (60, 120, 180, 240):  # paused: the API is not asked, the feed alone is read
        await ns.zkb_step(now=t0 + step)
    assert len(zkb.requests) == 1 and check_of(ns, row)["checks"] == 5
    assert ns.zkb.ready_in(t0 + 5) > 500


async def test_api_disabled_asks_nobody(ns, services, http, zkb):
    await add_character(services, A)
    ns.s.zkb_check.api_enabled = False
    row = await moved(ns, services, http)
    await ns.zkb_step(now=check_of(ns, row)["received_at"])
    assert zkb.requests == [] and check_of(ns, row)["status"] == "pending"


# --------------------------------------------------------------------------- the schedule
async def test_checks_run_every_interval_until_the_deadline_then_close_as_not_found(ns, services, http, zkb):
    await add_character(services, A)
    row = await moved(ns, services, http)
    c = check_of(ns, row)
    t0 = c["received_at"]
    assert c["planned"] == 6 and c["next_check_at"] == t0 and abs(c["deadline_at"] - (t0 + 300)) < 1e-6
    assert await ns.zkb_step(now=t0 - 1) == 0  # not due yet
    seen = []
    for step in range(6):
        assert await ns.zkb_step(now=t0 + 60 * step + 0.5) == 1
        got = check_of(ns, row)
        seen.append((got["status"], got["checks"], got["next_check_at"]))
    assert [s[0] for s in seen] == ["pending"] * 5 + ["not_found"]
    assert [s[1] for s in seen] == [1, 2, 3, 4, 5, 6]
    assert [round(s[2] - t0, 1) for s in seen[:4]] == [60.5, 120.5, 180.5, 240.5]
    assert round(seen[4][2] - t0, 1) == 300.0  # the last check is at the deadline, not a minute after it
    assert seen[5][2] is None and await ns.zkb_step(now=t0 + 400) == 0
    assert len(zkb.requests) == 6


async def test_the_first_kill_ends_the_checks(ns, services, http, zkb):
    await add_character(services, A)
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    t0 = check_of(ns, row)["received_at"]
    await ns.zkb_step(now=t0)
    await ns.zkb_step(now=t0 + 60)
    zkb.kills = [api_kill(41, epoch(when) + 100)]
    await ns.zkb_step(now=t0 + 120)
    assert check_of(ns, row)["status"] == "found" and len(zkb.requests) == 3
    assert await ns.zkb_step(now=t0 + 180) == 0 and len(zkb.requests) == 3  # nothing is asked after a match
    assert check_of(ns, row)["checks"] == 3


async def test_several_characters_of_one_event_share_one_check(ns, services, http, zkb):
    await add_character(services, A)
    await add_character(services, B)
    when = stamp(1)
    r1 = await moved(ns, services, http, 1, cid=A, when=when)
    r2 = await moved(ns, services, http, 9, cid=B, when=when)
    assert services["db"].query("SELECT COUNT(*) FROM notification_zkb")[0][0] == 1
    assert check_of(ns, r1)["event_key"] == check_of(ns, r2)["event_key"]
    await ns.zkb_step(now=check_of(ns, r1)["received_at"])
    assert len(zkb.requests) == 1 and ns.describe(r2)["zkb"]["checks"] == 1  # both copies show the one state


async def test_pending_checks_survive_a_restart_and_expired_ones_close(ns, services, http, zkb, settings, graph):
    await add_character(services, A)
    db = services["db"]
    row = await moved(ns, services, http, 1)
    stale = await moved(ns, services, http, 2, body=BODY.replace("1035000000001", "1035000000002"))
    t0 = check_of(ns, row)["received_at"]
    await ns.zkb_step(now=t0)
    again = NotificationService(settings.notifications, db, graph, services["chars"], services["esi"], demo=False)
    again.zkb = ns.zkb
    assert [c["event_key"] for c in db.pending_zkb_checks()].count(ns.event_key(row)) == 1
    assert check_of(again, row)["checks"] == 1 and check_of(again, row)["next_check_at"] == t0 + 60
    # a long outage: the first check is still within reach of its deadline, the second is not
    db.update_zkb_check(ns.event_key(stale), deadline_at=t0 - 1000, next_check_at=t0 - 900)
    assert await again.zkb_step(now=t0 + 60) == 2
    assert check_of(again, stale)["status"] == "not_found" and check_of(again, stale)["checks"] == 0
    assert check_of(again, row)["checks"] == 2 and check_of(again, row)["status"] == "pending"


async def test_the_loop_sleeps_until_the_next_zkb_check(ns, services):
    now = time.time()
    services["db"].create_zkb_check(
        event_key="k", notification_type=MOVED, solar_system_id=AMARR, structure_type_id=ASTRAHUS, event_at=now,
        received_at=now, status="pending", planned=6, next_check_at=now + 3, deadline_at=now + 300,
    )
    assert 2.5 < ns.sleep_s() <= 3.0
    assert 0.0 < ns.zkb_wait() <= 3.0


# --------------------------------------------------------------------------- Discord and the page
async def test_discord_is_posted_at_once_and_edited_when_the_check_ends(ns, services, http, zkb):
    await add_character(services, A)
    ns.notifier = FakeNotifier()
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    assert await ns.post_discord() == 1
    first = {f["name"]: f["value"] for f in ns.notifier.sent[0]["embeds"][0]["fields"]}
    assert first["Kill check"] == "zKillboard: checking… (0 of 6)" and first["Structure type"] == "Astrahus"
    t0 = check_of(ns, row)["received_at"]
    await ns.zkb_step(now=t0)  # nothing yet: the message is not touched
    assert ns.notifier.edits == []
    feed_kill(services["db"], 51, epoch(when) + 61)
    await ns.zkb_step(now=t0 + 60)
    assert len(ns.notifier.sent) == 1 and len(ns.notifier.edits) == 1
    mid, payload = ns.notifier.edits[0]
    value = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}["Kill check"]
    assert mid == "m1" and value == (
        f"zKillboard: Astrahus destroyed in Amarr at {time.strftime('%H:%M', time.gmtime(epoch(when) + 61))}"
        " — [zkillboard.com](https://zkillboard.com/kill/51/)"
    )
    assert "content" not in payload  # the asset safety group pings nobody, and an edit pings nobody again


async def test_a_check_that_finds_nothing_edits_the_message_with_the_closing_line(ns, services, http, zkb):
    await add_character(services, A)
    ns.notifier = FakeNotifier()
    row = await moved(ns, services, http)
    await ns.post_discord()
    t0 = check_of(ns, row)["received_at"]
    for step in range(6):
        await ns.zkb_step(now=t0 + 60 * step + 0.5)
    assert len(ns.notifier.sent) == 1 and len(ns.notifier.edits) == 1
    value = {f["name"]: f["value"] for f in ns.notifier.edits[0][1]["embeds"][0]["fields"]}["Kill check"]
    assert value == "zKillboard: no Astrahus kill in Amarr within 5 min"


async def test_the_result_is_in_the_first_post_when_the_check_ended_before_discord_posted(ns, services, http, zkb):
    await add_character(services, A)
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    feed_kill(services["db"], 52, epoch(when) + 5)
    await ns.zkb_step(now=check_of(ns, row)["received_at"])
    ns.notifier = FakeNotifier()
    await ns.post_discord()
    value = {f["name"]: f["value"] for f in ns.notifier.sent[0]["embeds"][0]["fields"]}["Kill check"]
    assert value.startswith("zKillboard: Astrahus destroyed in Amarr at ") and ns.notifier.edits == []


async def test_a_failed_discord_edit_is_tried_again(ns, services, http, zkb):
    from evedm.wars.discord import NotifyError

    await add_character(services, A)
    ns.notifier = FakeNotifier()
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    await ns.post_discord()
    feed_kill(services["db"], 53, epoch(when) + 5)
    real = ns.notifier.edit

    async def broken(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        raise NotifyError("discord down")

    ns.notifier.edit = broken
    t0 = check_of(ns, row)["received_at"]
    await ns.zkb_step(now=t0)
    assert check_of(ns, row)["status"] == "found" and ns.notifier.edits == []
    ns.notifier.edit = real
    await ns.zkb_step(now=t0 + 60)
    assert len(ns.notifier.edits) == 1 and len(ns.notifier.sent) == 1


async def test_the_page_payload_carries_the_three_lines_and_the_progress(ns, services, http, zkb):
    await add_character(services, A)
    when = stamp(1)
    row = await moved(ns, services, http, when=when)
    t0 = check_of(ns, row)["received_at"]
    d = ns.describe(row)
    assert d["zkb"]["text"] == "zKillboard: checking… (0 of 6)" and d["zkb"]["status"] == "pending"
    version = ns.version
    await ns.zkb_step(now=t0)
    assert ns.version > version  # the long poll wakes the page on every check
    assert ns.describe(row)["zkb"]["text"] == "zKillboard: checking… (1 of 6)"
    feed_kill(services["db"], 54, epoch(when) + 61)
    await ns.zkb_step(now=t0 + 60)
    z = ns.describe(row)["zkb"]
    assert z["status"] == "found" and z["url"] == "https://zkillboard.com/kill/54/" and z["source"] == "feed"
    assert z["text"].startswith("zKillboard: Astrahus destroyed in Amarr at ")
    other = await moved(ns, services, http, 2, body=FORT_BODY)
    t1 = check_of(ns, other)["received_at"]
    for step in range(6):
        await ns.zkb_step(now=t1 + 60 * step + 0.5)
    assert ns.describe(other)["zkb"]["text"] == "zKillboard: no Fortizar kill in Amarr within 5 min"
    ns.s.zkb_check.window_s = 120
    assert ns.describe(other)["zkb"]["text"].endswith("within 2 min")
    plain = await moved(ns, services, http, 3, ntype="StructureUnderAttack")
    assert ns.describe(plain)["zkb"] is None


async def test_the_api_lists_the_structure_type_and_the_check(tmp_path):
    from test_notifications import client_for, seed

    with client_for(tmp_path) as client:
        svc = client.app.state.svc
        seed(svc, AMARR)
        svc.db.save_type_names({ASTRAHUS: "Astrahus"})
        hdr = {"X-EVEDM": "1"}
        got = client.get("/api/notifications?hours=100000", headers=hdr).json()
        shown = [i for i in got["items"] if i["type"] == MOVED][0]
        assert shown["structure_type"] == "Astrahus" and shown["show_structure_type"] is True
        assert shown["zkb"] is None  # seeded straight into the table: no check was started
        health = client.get("/api/health", headers=hdr).json()
        assert "zkb_check" in health["notifications"]


# --------------------------------------------------------------------------- counts, lag report, migration
async def test_counts_are_in_the_state_the_health_output_and_the_lag_report(ns, services, http, zkb):
    await add_character(services, A)
    when = stamp(1)
    await moved(ns, services, http, when=when)
    other = await moved(ns, services, http, 2, body=FORT_BODY)
    feed_kill(services["db"], 61, epoch(when) + 5)  # the feed confirms the first; the second needs the API
    zkb.status = 500
    t1 = check_of(ns, other)["received_at"]
    await ns.zkb_step(now=t1 + 1)
    state = ns.state["zkb_check"]
    assert (state["pending"], state["found"], state["not_found"], state["api_errors"]) == (1, 1, 0, 1)
    assert state["api"]["requests"] == 1
    for step in range(1, 7):
        await ns.zkb_step(now=t1 + 60 * step + 1)
    report = ns.lag()["zkb_check"]
    assert report["found"] == 1 and report["not_found"] == 1 and report["pending"] == 0
    assert report["types"] == [MOVED] and report["api"]["requests"] >= 1
    assert ns.lag_brief()["zkb_check"]["found"] == 1


def test_the_lag_command_has_a_zkb_check_block(tmp_path, capsys, settings):
    settings.data_dir = tmp_path / "d"
    Database(settings.db_path, settings.sqlite_journal).close()
    import os

    os.environ["EVEDM_DATA_DIR"] = str(settings.data_dir)
    try:
        main(["notifications", "lag"])
    finally:
        del os.environ["EVEDM_DATA_DIR"]
    out = json.loads(capsys.readouterr().out)
    assert out["zkb_check"]["pending"] == 0 and out["zkb_check"]["types"] == [MOVED]


def test_an_old_database_gets_the_check_table_in_place(tmp_path):
    path = tmp_path / "old.db"
    Database(path, "WAL").close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE notification_zkb")
    raw.execute("PRAGMA user_version=18")
    raw.commit()
    raw.close()
    d = Database(path, "WAL")
    assert d.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION == 19
    assert d.create_zkb_check(event_key="k", notification_type=MOVED, received_at=1.0, status="pending", planned=6)
    assert not d.create_zkb_check(event_key="k", notification_type=MOVED, received_at=2.0, status="pending", planned=6)
    d.close()


def test_old_checks_are_purged_with_their_notifications(db):
    db.create_zkb_check(event_key="old", notification_type=MOVED, received_at=time.time() - 40 * 86400,
                        status="not_found", planned=6)
    db.create_zkb_check(event_key="new", notification_type=MOVED, received_at=time.time(), status="pending", planned=6)
    db.purge_notifications(30)
    assert [r["event_key"] for r in db.query("SELECT event_key FROM notification_zkb")] == ["new"]


def test_the_settings_are_validated():
    from evedm.config import ConfigError, Settings

    s = Settings(secret_key="k", demo=True)
    assert s.notifications.zkb_check.types == [MOVED] and s.notifications.zkb_check.window_s == 300
    s.notifications.zkb_check.interval_s = 1
    with pytest.raises(ConfigError, match="interval_s"):
        s.validate_for_serve()
    s = Settings(secret_key="k", demo=True)
    s.notifications.zkb_check.api_url = "ftp://x"
    with pytest.raises(ConfigError, match="api_url"):
        s.validate_for_serve()


def test_the_check_can_be_switched_off_by_environment(monkeypatch):
    from evedm.config import _load_settings

    monkeypatch.setenv("EVEDM_NOTIFICATIONS_ZKB_CHECK_ENABLED", "0")
    monkeypatch.setenv("EVEDM_NOTIFICATIONS_ZKB_CHECK_API_ENABLED", "false")
    s = _load_settings(None)
    assert s.notifications.zkb_check.enabled is False and s.notifications.zkb_check.api_enabled is False


def test_the_api_url_form():
    api = ZkbApi("https://zkillboard.com/api/", "ua")
    assert api.url(AMARR, ASTRAHUS, 400.2) == f"https://zkillboard.com/api/kills/systemID/{AMARR}/shipTypeID/{ASTRAHUS}/pastSeconds/400/"
    assert api.url(1, 2, 10**9).endswith("/pastSeconds/604800/") and api.url(1, 2, 1).endswith("/pastSeconds/60/")
