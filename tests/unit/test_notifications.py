"""Character notifications (UC-13): polling with the ESI cache, parsing, the alliance gate and the API."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from common import AMARR, JITA, PERIMETER, TAMA, add_character
from evedm.config import NOTIFICATION_SCOPE, Settings
from evedm.notifications import NotificationService, parse_body, title_of
from evedm.wars.config import WarsSettings
from evedm.web.app import create_app

pytestmark = pytest.mark.anyio

H = {"X-EVEDM": "1"}
A, B = 2100000001, 2100000002
BODY = "solarsystemID: 30002187\nstructureID: 1035000000001\nstructureTypeID: 35832\n"


@pytest.fixture
def ns(settings, services, graph):  # noqa: ANN001, ANN201
    return NotificationService(
        settings.notifications, services["db"], graph, services["chars"], services["esi"], demo=False
    )


async def push(http, character_id: int, items: list[dict]) -> None:  # noqa: ANN001
    await http.post("/_admin/notifications", json={"character_id": character_id, "items": items})


def item(nid: int, ntype: str = "StructureUnderAttack", text: str = BODY, **kw) -> dict:  # noqa: ANN003
    return {"notification_id": nid, "type": ntype, "text": text, **kw}


# --------------------------------------------------------------------------- parsing
def test_body_is_parsed_and_a_broken_one_is_empty():
    assert parse_body(BODY)["structureID"] == 1035000000001
    assert parse_body("not: [valid: yaml") == {}
    assert parse_body("") == {}


def test_titles_fall_back_to_the_type_name():
    assert title_of("StructureItemsMovedToSafety", "K-3PQW") == "Your assets in K-3PQW have been moved to Asset Safety!"
    assert title_of("StructureUnderAttack", "Amamake") == "A structure in Amamake is under attack!"
    assert title_of("SomeNewCcpType", "Jita") == "Some New Ccp Type in Jita"


# --------------------------------------------------------------------------- polling (NTF-02..NTF-05)
async def test_poll_stores_new_notifications_once(ns, services, http):
    await add_character(services, A)
    await push(http, A, [item(1), item(2, "StructureItemsMovedToSafety")])
    assert await ns.poll_once() == 2
    rows = services["db"].notifications([A])
    assert [r["type"] for r in rows] == ["StructureItemsMovedToSafety", "StructureUnderAttack"] or len(rows) == 2
    stored = {r["notification_id"]: r for r in rows}
    assert stored[1]["solar_system_id"] == AMARR
    assert stored[1]["structure_id"] == 1035000000001 and stored[1]["structure_type_id"] == 35832

    # the character is not due again until its ESI cache expires, so a second round reads nothing
    assert await ns.poll_once() == 0
    services["db"].set_notification_check(A, 0)
    assert await ns.poll_once() == 0  # due again, but ESI has nothing new
    assert len(services["db"].notifications([A])) == 2


async def test_only_characters_that_granted_the_scope_are_polled(ns, services, http):
    await add_character(services, A)
    await add_character(services, B, scopes=["esi-location.read_location.v1"])
    await push(http, A, [item(1)])
    await push(http, B, [item(2)])
    assert set(ns.pollable()) == {A}
    assert await ns.poll_once() == 1
    assert services["db"].notifications([B]) == []


async def test_every_registered_character_with_a_valid_token_is_polled(ns, services, http):
    """NTF-04: whoever linked it, whatever the ranking switch says; only a revoked token drops out."""
    await add_character(services, A, owner_id=A)
    await add_character(services, B, owner_id=B)  # another main's character
    await add_character(services, 2100000003, owner_id=B)
    await add_character(services, 2100000004, owner_id=B)
    db = services["db"]
    db.execute("UPDATE characters SET enabled=0 WHERE character_id=?", (B,))  # off in the ranking only
    db.execute("UPDATE characters SET status='REAUTH' WHERE character_id=?", (2100000003,))  # token revoked
    await add_character(services, 2100000004, scopes=["esi-location.read_location.v1"], owner_id=A)  # 2nd link
    assert set(ns.pollable()) == {A, B, 2100000004}
    assert ns.pollable()[B] == B
    for cid in (A, B, 2100000004):
        await push(http, cid, [item(cid % 1000)])
    assert await ns.poll_once() == 3


async def test_a_notification_without_a_system_is_still_stored(ns, services, http):
    await add_character(services, A)
    await push(http, A, [item(7, "CorpAppNewMsg", text="charID: 90000001\n")])
    assert await ns.poll_once() == 1
    row = services["db"].notifications([A])[0]
    assert row["solar_system_id"] is None and row["type"] == "CorpAppNewMsg"


async def test_a_broken_token_is_recorded_and_does_not_stop_the_round(ns, services, http):
    await add_character(services, A)
    await add_character(services, B)
    await push(http, A, [item(1)])
    await http.post(f"/_admin/revoke/{B}")
    # expire B's access token so the poller has to refresh it, which the revoked refresh token refuses
    services["db"].execute("UPDATE tokens SET access_expires_at=0 WHERE character_id=?", (B,))
    assert await ns.poll_once() == 1
    checks = services["db"].notification_checks()
    assert checks[B]["error"] and checks[A]["error"] is None


async def test_old_notifications_are_purged(ns, services):
    db = services["db"]
    db.save_notifications([{"character_id": A, "notification_id": 5, "type": "StructureUnderAttack",
                            "timestamp": "2026-01-01T00:00:00Z", "body": ""}])
    db.execute("UPDATE character_notifications SET created_at=? WHERE notification_id=5", (time.time() - 90 * 86400,))
    assert db.purge_notifications(ns.s.retention_days) == 1
    assert db.notifications([A]) == []


# --------------------------------------------------------------------------- filter catalogue (NTF-08)
async def test_the_catalogue_groups_the_known_types_and_adds_what_was_seen(ns):
    groups = ns.catalogue({"StructureUnderAttack": 3, "SomeNewCcpType": 1})
    by_name = {g["group"]: g for g in groups}
    assert by_name["asset_safety"]["default"] is True
    assert {t["type"] for t in by_name["structure_attack"]["types"]} >= {"StructureUnderAttack", "StructureDestroyed"}
    assert by_name["other"]["types"] == [{"type": "SomeNewCcpType", "seen": 1, "default": False}]
    assert by_name["sovereignty"]["default"] is False
    assert "StructureItemsMovedToSafety" in ns.default_types()
    assert "SovStructureReinforced" not in ns.default_types()


# --------------------------------------------------------------------------- web API (NTF-06, NTF-09)
def web_settings(tmp_path, alliances: list[int]) -> Settings:  # noqa: ANN001
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.secret_key = "notifications-secret"
    s.eve.client_id, s.eve.client_secret = "cid", "csecret"
    s.eve.sso_base = s.eve.esi_base = "http://127.0.0.1:9"  # never reached in these tests
    s.web.auth = "none"
    s.access.alliance_ids = alliances
    s.alerts.enabled = False
    s.structures.enabled = False
    s.evescout.enabled = False  # no EVE-Scout request from the tests that do not look at Thera or Turnur
    s.notifications.min_structures = 0  # the structure gate has its own tests
    return s


def client_for(tmp_path, alliances: list[int] = []) -> TestClient:  # noqa: ANN001, B006
    return TestClient(create_app(web_settings(tmp_path, alliances), WarsSettings(enabled=False)))


def seed(svc, system_id: int, ntype: str = "StructureItemsMovedToSafety") -> None:  # noqa: ANN001
    svc.db.upsert_character(A, "Alpha One", "OK", [NOTIFICATION_SCOPE], owner_id=0)
    svc.db.save_snapshot(A, solar_system_id=system_id, online=0, fetched_at=time.time(), error=None)
    svc.roster.import_file(
        b'[{"char_name": "Static Scout", "star_system_name": "Perimeter"}]', 0
    )
    svc.db.save_notifications(
        [
            {
                "character_id": A,
                "notification_id": 1,
                "type": ntype,
                "timestamp": "2026-11-01T17:22:00Z",
                "body": BODY,
                "solar_system_id": system_id,
                "structure_id": 1035000000001,
                "structure_type_id": 35832,
            },
            {
                "character_id": A,
                "notification_id": 2,
                "type": "InsurancePayoutMsg",
                "timestamp": "2026-11-01T10:00:00Z",
                "body": "",
                "solar_system_id": None,
            },
        ]
    )


async def test_the_api_lists_notifications_with_region_and_closest_characters(tmp_path):
    with client_for(tmp_path) as c:
        svc = c.app.state.svc
        seed(svc, JITA)
        data = c.get("/api/notifications?refresh=0").json()
        assert [i["type"] for i in data["items"]] == ["StructureItemsMovedToSafety"]  # the other type is filtered out
        item0 = data["items"][0]
        assert item0["title"].startswith("Your assets in Jita")
        assert item0["system"]["region"] == "The Forge" and item0["system"]["name"] == "Jita"
        assert item0["character"] == "Alpha One"
        assert [c0["name"] for c0 in item0["closest"]] == ["Alpha One"]
        assert item0["closest_static"] == [{"name": "Static Scout", "jumps": 1, "static": True}]
        assert data["characters"]["linked"] == 1 and data["characters"]["reading"] == 1

        everything = c.get("/api/notifications?refresh=0&types=InsurancePayoutMsg").json()
        assert [i["type"] for i in everything["items"]] == ["InsurancePayoutMsg"]
        assert everything["items"][0]["system"] is None and everything["items"][0]["closest"] == []


async def test_the_page_and_api_are_hidden_from_other_alliances(tmp_path):
    with client_for(tmp_path, [99010320]) as c:
        svc = c.app.state.svc
        seed(svc, PERIMETER)
        assert c.get("/api/notifications?refresh=0").status_code == 404  # no affiliation known
        assert c.get("/notifications").status_code == 404
        assert "/notifications" not in c.get("/").text

        svc.db.upsert_user(0, "local")
        svc.db.set_affiliation(0, 98000001, 99010320)
        assert c.get("/api/notifications?refresh=0").status_code == 200
        assert c.get("/notifications").status_code == 200
        assert "/notifications" in c.get("/").text


async def test_the_api_lists_the_notifications_of_every_registered_character(tmp_path):
    """NTF-13: the page is site-wide, not limited to the caller's own links."""
    with client_for(tmp_path) as c:
        svc = c.app.state.svc
        seed(svc, JITA)  # Alpha One, linked by the local user 0
        svc.db.upsert_user(77, "Main Seven")
        svc.db.upsert_character(B, "Bravo Two", "OK", [NOTIFICATION_SCOPE], owner_id=77)  # someone else's
        gone = 2100000009
        svc.db.upsert_character(gone, "Revoked Nine", "REAUTH", [NOTIFICATION_SCOPE], owner_id=77)
        rows = [
            {"character_id": cid, "notification_id": nid, "type": "StructureUnderAttack",
             "timestamp": f"2026-11-01T1{nid}:00:00Z", "body": "", "solar_system_id": PERIMETER}
            for cid, nid in ((B, 5), (gone, 6))
        ]
        svc.db.save_notifications(rows)
        data = c.get("/api/notifications?refresh=0&types=StructureUnderAttack,StructureItemsMovedToSafety").json()
        received = {i["character"]: i["main"] for i in data["items"]}
        assert received == {"Alpha One": None, "Bravo Two": "Main Seven"}  # the revoked character is left out
        assert data["characters"]["reading"] == 2 and data["characters"]["linked"] == 3


# --------------------------------------------------------------------------- Thera / Turnur on the page
async def test_a_notification_shows_the_closer_of_thera_and_turnur_and_the_route_type(tmp_path):
    s = web_settings(tmp_path, [])
    s.evescout.enabled = True
    with TestClient(create_app(s, WarsSettings(enabled=False))) as c:
        svc = c.app.state.svc
        svc.evescout.load(svc.evescout.demo_payload())  # Thera: Jita, Amarr, Rens, Amamake; Turnur: Tama, Old Man Star
        svc.evescout.fetched_at = time.time()  # fresh: the test never asks the network
        seed(svc, AMARR)
        svc.db.save_snapshot(A, solar_system_id=JITA, online=0, fetched_at=time.time(), error=None)
        item0 = c.get("/api/notifications?refresh=0").json()["items"][0]
        hub = item0["hub"]
        assert hub["hub"] == "Thera" and hub["gates"] == 0 and hub["jumps"] == 1 and hub["entry"]["name"] == "Amarr"
        near = item0["closest"][0]
        assert near["name"] == "Alpha One" and near["route"] == "Thera" and near["jumps"] == 2  # not ~10 by gates

        seed(svc, TAMA)  # a system that lists a Turnur connection
        assert svc.evescout.nearest(svc.graph.index[TAMA])["hub"] == "Turnur"
        assert svc.evescout.nearest(svc.graph.index[PERIMETER])["hub"] == "Thera"  # one gate from Jita


async def test_closest_characters_keep_the_gates_when_they_are_not_longer(tmp_path):
    s = web_settings(tmp_path, [])
    s.evescout.enabled = True
    with TestClient(create_app(s, WarsSettings(enabled=False))) as c:
        svc = c.app.state.svc
        svc.evescout.load(svc.evescout.demo_payload())
        svc.evescout.fetched_at = time.time()
        seed(svc, JITA)
        svc.db.save_snapshot(A, solar_system_id=PERIMETER, online=0, fetched_at=time.time(), error=None)
        near = c.get("/api/notifications?refresh=0").json()["items"][0]["closest"][0]
        assert near["route"] == "gates" and near["jumps"] == 1


# --------------------------------------------------------------------------- who is read, and when
def sight(db, character_id: int, count: int) -> None:  # noqa: ANN001
    with db.tx() as c:
        c.executemany(
            "INSERT INTO structure_sightings(structure_id, character_id, seen_at) VALUES(?,?,?)",
            [(1_030_000_000_000 + n, character_id, time.time()) for n in range(count)],
        )


async def test_characters_with_few_structures_are_not_read(settings, services, graph, http):
    settings.notifications.min_structures = 10
    ns = NotificationService(settings.notifications, services["db"], graph, services["chars"], services["esi"])
    await add_character(services, A)
    await add_character(services, B)
    await push(http, A, [item(1)])
    await push(http, B, [item(2)])
    sight(services["db"], A, 10)  # exactly the limit counts
    sight(services["db"], B, 9)
    assert set(ns.pollable()) == {A, B} and set(ns.eligible()) == {A}
    assert await ns.poll_once() == 1
    assert services["db"].notifications([B]) == []
    settings.notifications.min_structures = 0  # 0 reads everybody
    assert set(ns.eligible()) == {A, B}


async def test_the_notifications_tick_decides_whether_a_character_is_read(ns, services, http):
    await add_character(services, A, owner_id=A)
    await push(http, A, [item(1)])
    services["db"].set_notify(A, False, A)
    assert ns.pollable() == {} and await ns.poll_once() == 0
    services["db"].set_notify(A, True, A)
    assert set(ns.pollable()) == {A}
    # two links of one character: it is read as long as one of them has the tick
    await add_character(services, A, owner_id=B)
    services["db"].set_notify(A, False, A)
    assert ns.pollable() == {A: B}


async def test_the_next_read_follows_the_expiry_of_esis_cache(ns, services, http):
    await add_character(services, A)
    await push(http, A, [item(1)])
    now = time.time()
    ns._expires[A] = now + 200  # ESI's copy goes stale in 200 s
    assert now + 200 < ns.next_check_at(A, now) < now + 205  # not a fixed 600 s later
    ns._expires[A] = now + 3  # never more often than min_interval_s
    assert ns.next_check_at(A, now) == pytest.approx(now + ns.s.min_interval_s)
    ns._expires[A] = now + 5000  # and never later than character_interval_s
    assert ns.next_check_at(A, now) == pytest.approx(now + ns.s.character_interval_s)
    ns._expires[A] = None
    assert ns.next_check_at(A, now) == pytest.approx(now + ns.s.character_interval_s)


async def test_a_passing_esi_error_is_retried_soon_but_a_refusal_waits(ns, services, http):
    from evedm.esi import EsiError

    await add_character(services, A)
    await add_character(services, B)
    calls = {A: EsiError(503, "down"), B: EsiError(403, "no")}

    async def fake_read(cid: int, owner: int) -> int:
        raise calls[cid]

    ns.read_character = fake_read  # type: ignore[method-assign]
    before = time.time()
    await ns.poll_once()
    checks = services["db"].notification_checks()
    assert before + ns.s.retry_s - 1 < checks[A]["next_check_at"] < before + ns.s.retry_s + 5
    assert checks[B]["next_check_at"] > before + ns.s.character_interval_s - 5


async def test_the_loop_sleeps_until_the_next_character_is_due(ns, services, http):
    await add_character(services, A)
    assert ns.sleep_s() == ns.s.poll_s  # nothing read yet
    services["db"].set_notification_check(A, time.time() + 20)
    assert 15 < ns.sleep_s() <= 21
    services["db"].set_notification_check(A, time.time() + 5000)
    assert ns.sleep_s() == ns.s.poll_s
    services["db"].set_notification_check(A, time.time() - 5)
    assert ns.sleep_s() == 1.0


async def test_the_api_sets_the_notifications_tick(tmp_path):
    with client_for(tmp_path) as c:
        svc = c.app.state.svc
        seed(svc, JITA)
        listed = c.get("/api/characters").json()[0]
        assert listed["notify"] is True and "refresh_tail" not in listed and "token_expires_at" not in listed
        assert c.patch(f"/api/characters/{A}", json={"notify": False}, headers=H).status_code == 200
        assert c.get("/api/characters").json()[0]["notify"] is False
        assert c.patch(f"/api/characters/{A}", json={"notify": "no"}, headers=H).status_code == 400


# --------------------------------------------------------------------------- speed and robustness
async def test_a_reread_parses_only_notifications_that_are_new(ns, services, http, monkeypatch):
    import evedm.notifications as mod

    parsed: list[str] = []
    real = mod.parse_body
    monkeypatch.setattr(mod, "parse_body", lambda text: parsed.append(text) or real(text))
    await add_character(services, A)
    await push(http, A, [item(n) for n in range(1, 41)])
    assert await ns.poll_once() == 40 and len(parsed) == 40

    # ESI repeats the whole list: after a restart (nothing in memory) only the new entry is parsed and stored
    fresh = NotificationService(ns.s, services["db"], ns.g, services["chars"], services["esi"])
    monkeypatch.setattr(services["esi"], "_cache", type(services["esi"]._cache)())
    await push(http, A, [item(n) for n in range(1, 42)])
    services["db"].set_notification_check(A, 0)
    assert await fresh.poll_once() == 1 and len(parsed) == 41
    assert len(services["db"].notifications([A], limit=500)) == 41


async def test_an_odd_answer_backs_off_instead_of_being_read_every_round(ns, services, http, monkeypatch):
    await add_character(services, A)
    await add_character(services, B)
    await push(http, B, [item(7)])

    real = ns.read_character

    async def read(cid: int, owner: int) -> int:
        if cid == A:
            raise RuntimeError("unexpected")
        return await real(cid, owner)

    monkeypatch.setattr(ns, "read_character", read)
    assert await ns.poll_once() == 1  # B is still read
    check = services["db"].notification_checks()[A]
    assert "unexpected" in check["error"]
    assert 0 < check["next_check_at"] - time.time() <= ns.s.retry_s + 1
    assert A not in services["db"].due_notification_characters([A, B])


def test_the_cache_lifetime_is_measured_on_esis_clock():
    import httpx

    from evedm.esi import _expires

    # the host clock is 5 minutes behind ESI: the copy still lives 600 s from now, not 900 s
    resp = httpx.Response(
        200, headers={"Date": "Tue, 10 Nov 2026 12:00:00 GMT", "Expires": "Tue, 10 Nov 2026 12:10:00 GMT"}
    )
    assert abs(_expires(resp) - (time.time() + 600)) < 2
    late = httpx.Response(
        200, headers={"Date": "Tue, 10 Nov 2026 12:10:05 GMT", "Expires": "Tue, 10 Nov 2026 12:10:00 GMT"}
    )
    assert abs(_expires(late) - time.time()) < 2  # already expired: never negative


async def test_many_notifications_of_one_system_are_measured_once(tmp_path, monkeypatch):
    with client_for(tmp_path) as c:
        svc = c.app.state.svc
        seed(svc, AMARR)
        svc.db.save_notifications(
            [
                {
                    "character_id": A,
                    "notification_id": 100 + n,
                    "type": "StructureItemsMovedToSafety",
                    "timestamp": f"2026-11-02T10:{n:02d}:00Z",
                    "body": BODY,
                    "solar_system_id": AMARR,
                }
                for n in range(30)
            ]
        )
        calls: list[int] = []
        real = svc.ranking._parents
        monkeypatch.setattr(svc.ranking, "_parents", lambda idx: calls.append(idx) or real(idx))
        body = c.get("/api/notifications?refresh=0&limit=500", headers=H).json()
        assert len(body["items"]) >= 30
        assert len({i["system"]["name"] for i in body["items"]}) == 1
        assert len(calls) == 1  # one distance calculation for the 30 cards
        assert body["items"][0]["closest"] == body["items"][-1]["closest"]


# --------------------------------------------------------------------------- delivery: push, stagger, Discord
async def test_a_waiting_page_is_woken_as_soon_as_something_new_is_stored(ns, services, http):
    import asyncio

    await add_character(services, A)
    assert await ns.wait_changed(since=-1) == 0  # a stale version is answered at once
    waiter = asyncio.ensure_future(ns.wait_changed(since=0, timeout=5))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await push(http, A, [item(1)])
    started = time.time()
    assert await ns.poll_once() == 1
    assert await waiter == 1 and time.time() - started < 2  # not after the 5 s timeout
    assert await ns.wait_changed(since=1, timeout=0.05) == 1  # nothing new: held, then the same version


async def test_the_wait_endpoint_holds_until_the_timeout(tmp_path):
    settings = web_settings(tmp_path, [])
    settings.notifications.wait_s = 1.0
    with TestClient(create_app(settings, WarsSettings())) as c:
        seed(c.app.state.svc, JITA)
        version = c.get("/api/notifications?refresh=0", headers=H).json()["version"]
        assert c.get(f"/api/notifications/wait?since={version - 1}", headers=H).json()["changed"] is True
        started = time.time()
        held = c.get(f"/api/notifications/wait?since={version}", headers=H).json()
        assert held == {"version": version, "changed": False} and 0.8 < time.time() - started < 5


async def test_reads_are_spread_over_esis_cache_window(ns):
    now = 1_000_000.0
    ns.s.stagger_s = 60
    # alone, or already apart: nothing moves
    assert ns.staggered(A, now, []) == now
    assert ns.staggered(A, now, [now + 300]) == now
    # two characters due almost together: the later one moves on, by at most stagger_s; the earlier one stays
    assert ns.staggered(A, now + 2, [now]) == now + 62
    assert ns.staggered(A, now, [now + 2]) == now
    # positions are compared inside the 600 s window: a read 600 s later is the same slot
    assert ns.staggered(A, now, [now + 600]) > now
    # over the rounds five clustered characters end up about 120 s apart
    times = {c: now for c in range(5)}
    for _ in range(40):
        for c in times:
            times[c] = ns.staggered(c, times[c] + 600, [v for k, v in times.items() if k != c])
    phases = sorted(t % 600 for t in times.values())
    gaps = [b - a for a, b in zip(phases, phases[1:], strict=False)] + [600 - phases[-1] + phases[0]]
    assert min(gaps) > 100
    ns.s.stagger_s = 0
    assert ns.staggered(A, now, [now + 2]) == now


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[tuple[str, dict]] = []
        self.gone = False

    async def send(self, payload: dict) -> dict:
        self.sent.append(payload)
        return {"id": f"m{len(self.sent)}"}

    async def edit(self, message_id: str, payload: dict) -> dict | None:
        self.edits.append((message_id, payload))
        return None if self.gone else {"id": message_id}


def stamp(minutes_ago: float = 1) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))


async def test_a_notification_is_posted_once_and_updated_for_the_next_recipient(ns, services, http):
    await add_character(services, A)
    await add_character(services, B)
    ns.notifier, ns.page_url = FakeNotifier(), "https://evedm.example/notifications"
    when = stamp()
    await push(http, A, [item(1, timestamp=when)])
    await ns.poll_once()
    assert await ns.post_discord() == 1
    embed = ns.notifier.sent[0]["embeds"][0]
    assert embed["title"] == "A structure in Amarr is under attack!" and embed["timestamp"] == when
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert "Amarr" in fields["System"] and "dotlan" in fields["System"]
    assert fields["Received by"].count(",") == 0 and "/notifications" in fields["Closest characters"]

    # the same event reaches a second character: the message is edited, not posted again
    await push(http, B, [item(9, timestamp=when)])
    services["db"].set_notification_check(B, 0)  # its ESI cache has expired
    services["esi"]._cache.clear()
    await ns.poll_once()
    assert await ns.post_discord() == 1
    assert len(ns.notifier.sent) == 1 and ns.notifier.edits[0][0] == "m1"
    received = {f["name"]: f["value"] for f in ns.notifier.edits[0][1]["embeds"][0]["fields"]}["Received by"]
    assert received.count(",") == 1
    assert await ns.post_discord() == 0  # nothing left

    # a message deleted in Discord is posted again
    ns.notifier.gone = True
    services["db"].execute("UPDATE character_notifications SET posted=0 WHERE character_id=?", (B,))
    services["db"].save_notification_post(ns.event_key(services["db"].notifications([A])[0]), "m1", [])
    assert await ns.post_discord() == 1 and len(ns.notifier.sent) == 2


async def test_history_and_filtered_types_are_never_posted(ns, services, http):
    await add_character(services, A)
    ns.notifier = FakeNotifier()
    await push(
        http,
        A,
        [
            item(1, timestamp=stamp(minutes_ago=600)),  # history from a first read
            item(2, "InsurancePayoutMsg", text="", timestamp=stamp()),  # not one of the posted groups
            item(3, "StructureItemsMovedToSafety", timestamp=stamp()),
        ],
    )
    await ns.poll_once()
    assert await ns.post_discord() == 1
    assert [p["embeds"][0]["title"] for p in ns.notifier.sent] == [
        "Your assets in Amarr have been moved to Asset Safety!"
    ]
    states = {r["notification_id"]: r["posted"] for r in services["db"].notifications([A], limit=10)}
    assert states == {1: 2, 2: 2, 3: 1}


async def test_a_failed_post_is_kept_for_the_next_round(ns, services, http):
    from evedm.wars.discord import NotifyError

    await add_character(services, A)

    class Down(FakeNotifier):
        async def send(self, payload: dict) -> dict:
            raise NotifyError("Discord error 500")

    ns.notifier = Down()
    await push(http, A, [item(1, timestamp=stamp())])
    await ns.poll_once()
    assert await ns.post_discord() == 0 and ns._post_pending
    ns.notifier = FakeNotifier()
    assert await ns.post_discord() == 1


async def test_the_role_is_pinged_for_attacks_only(ns, services, http):
    await add_character(services, A)
    ns.notifier = FakeNotifier()
    ns.s.discord.mention_role_id = "4242"
    await push(http, A, [item(1, timestamp=stamp()), item(2, "StructureItemsMovedToSafety", timestamp=stamp(2))])
    await ns.poll_once()
    assert await ns.post_discord() == 2
    by_title = {p["embeds"][0]["title"]: p for p in ns.notifier.sent}
    assert by_title["A structure in Amarr is under attack!"]["content"] == "<@&4242>"
    assert "content" not in by_title["Your assets in Amarr have been moved to Asset Safety!"]
    ns.s.discord.mention_role_id = ""


async def test_the_wars_webhook_is_used_when_notifications_have_none(tmp_path):
    ws = WarsSettings()
    ws.discord.webhook_url = "https://discord.example/api/webhooks/1/abc"
    with TestClient(create_app(web_settings(tmp_path, []), ws)) as c:
        ns = c.app.state.svc.notifications
        assert ns.notifier is not None and ns.notifier.webhook_url == ws.discord.webhook_url
    with TestClient(create_app(web_settings(tmp_path / "none", []), WarsSettings())) as c:
        assert c.app.state.svc.notifications.notifier is None


def test_notifications_stored_before_the_upgrade_are_not_posted(tmp_path):
    import sqlite3

    from evedm.db import SCHEMA_VERSION, Database

    path = tmp_path / "old.sqlite3"
    Database(path).close()
    con = sqlite3.connect(path)  # back to the shape of schema v10: no "posted" column
    con.execute("DROP INDEX IF EXISTS notifications_pending")
    con.execute("ALTER TABLE character_notifications DROP COLUMN posted")
    con.execute("DROP TABLE notification_posts")
    con.execute(
        "INSERT INTO character_notifications(character_id,notification_id,type,timestamp,created_at) "
        "VALUES(1,1,'StructureUnderAttack','2026-11-01T00:00:00Z',1)"
    )
    con.execute("PRAGMA user_version=10")
    con.commit()
    con.close()

    db = Database(path)
    try:
        assert db.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
        assert db.query("SELECT posted FROM character_notifications")[0][0] == 2  # history: never posted
        db.save_notifications(
            [{"character_id": 1, "notification_id": 2, "type": "StructureUnderAttack", "timestamp": stamp()}]
        )
        assert [r["notification_id"] for r in db.pending_notification_posts()] == [2]
    finally:
        db.close()
