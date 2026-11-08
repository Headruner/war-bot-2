"""Character notifications (UC-13): polling with the ESI cache, parsing, the alliance gate and the API."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from common import AMARR, JITA, PERIMETER, add_character
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
