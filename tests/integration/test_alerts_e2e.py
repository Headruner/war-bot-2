"""Structure alerts end to end: the fake's R2Z2 feed -> evedm's alert service -> Alerts API with the closest
characters -> 'Set destination' through ESI's autopilot endpoint (real processes, real sockets)."""

from __future__ import annotations

import time

import httpx
import pytest
from helpers import evedm_env, evedm_server, fake_eve, free_port, sso_flow

pytestmark = pytest.mark.integration
H = {"X-EVEDM": "1"}
FIRST = 2_100_000_000
JITA, PERIMETER, AMARR = 30000142, 30000144, 30002187
OWNER, SHOOTERS = 98000001, 98000002


def killmail(kid: int, system: int, victim_type: int, corp: int, attackers: list[dict] | None = None) -> dict:
    return {
        "killmail_id": kid,
        "killmail_time": "2026-10-31T12:00:00Z",
        "solar_system_id": system,
        "victim": {"ship_type_id": victim_type, "corporation_id": corp, "position": {"x": 1.0, "y": 2.0, "z": 3.0}},
        "attackers": attackers or [{"ship_type_id": 587, "corporation_id": SHOOTERS, "final_blow": True}],
    }


def test_feed_alerts_closest_characters_and_set_destination(tmp_path):
    port = free_port()
    with fake_eve(characters=4, systems=str(JITA)) as eve:
        httpx.post(f"{eve}/_admin/structures", json={"corp_names": {OWNER: "Fort Owners", SHOOTERS: "Shooters"}})
        packages = [
            {"killmail_id": 1, "hash": "h1", "esi": killmail(1, PERIMETER, 35832, OWNER), "zkb": {"totalValue": 2e9}},
            {
                "killmail_id": 2,
                "hash": "h2",
                "esi": killmail(2, AMARR, 587, SHOOTERS, [{"ship_type_id": 35836, "corporation_id": OWNER}]),
            },
            {"killmail_id": 3, "hash": "h3", "esi": killmail(3, JITA, 587, SHOOTERS)},  # a plain kill: no alert
            {"killmail_id": 4, "hash": "h4", "zkb": {"hash": "h4"}},  # body only through ESI
        ]
        extra = [{"killmail_id": 4, "hash": "h4", "killmail": killmail(4, JITA, 35825, OWNER)}]
        httpx.post(f"{eve}/_admin/r2z2", json={"packages": packages, "killmails": extra})
        env = evedm_env(tmp_path / "data", eve, port, EVEDM_REFRESH_INTERVAL_S="0", EVEDM_MAX_WAIT_AGE_S="0")
        with evedm_server(env, log_path=tmp_path / "server.out") as (base, _):
            c = httpx.Client(base_url=base)
            assert sso_flow(c, "/sso/login", FIRST).headers["location"] == "/characters?welcome=1"
            assert c.get("/alerts").status_code == 200

            groups: list[dict] = []
            deadline = time.time() + 30
            while time.time() < deadline and len(groups) < 3:
                groups = c.get("/api/alerts").json()["groups"]  # refreshes locations first
                time.sleep(0.3)
            by_system = {g["system"]["name"]: g for g in groups}
            assert set(by_system) == {"Perimeter", "Amarr", "Jita"}, groups
            assert by_system["Perimeter"]["state"] == "destroyed" and by_system["Perimeter"]["core_drop"]
            assert by_system["Perimeter"]["owner"]["name"] == "Fort Owners"
            assert by_system["Amarr"]["state"] == "firing" and not by_system["Amarr"]["core_drop"]
            assert by_system["Jita"]["structure"]["type"] == "Raitaru" and by_system["Jita"]["core_drop"]
            closest = by_system["Perimeter"]["closest"][0]
            assert closest["character_id"] == FIRST and closest["jumps"] == 1 and closest["can_set_route"]
            stats = httpx.get(f"{eve}/_admin/stats").json()
            assert any(ua.startswith("EVE-Distance-Meter/") for ua in stats["r2z2_user_agents"])
            assert stats["calls"].get("killmail", 0) >= 1

            # Set destination: online -> ESI autopilot call; offline -> a clear error
            httpx.post(f"{eve}/_admin/move/{FIRST}", json={"online": True})
            r = c.post(f"/api/characters/{FIRST}/destination", json={"destination_id": PERIMETER}, headers=H)
            assert r.status_code == 200 and r.json() == {"ok": True, "destination": "Perimeter"}, r.text
            waypoint = httpx.get(f"{eve}/_admin/stats").json()["waypoints"][-1]
            assert waypoint == {
                "character_id": FIRST,
                "destination_id": PERIMETER,
                "add_to_beginning": False,
                "clear_other_waypoints": True,
            }
            httpx.post(f"{eve}/_admin/move/{FIRST}", json={"online": False})
            r = c.post(f"/api/characters/{FIRST}/destination", json={"destination_id": AMARR}, headers=H)
            assert r.status_code == 502 and "online" in r.json()["detail"]
