"""EVE-Scout Thera / Turnur filter end to end: the fake's /evescout/signatures list -> evedm's EveScout service ->
/api/rank?thera=1 with a two-gate-leg route joined by a wormhole (real processes, real sockets)."""

from __future__ import annotations

import httpx
import pytest
from helpers import evedm_env, evedm_server, fake_eve, free_port, sso_flow

pytestmark = pytest.mark.integration
FIRST = 2_100_000_000
JITA, PERIMETER, AMARR, RENS, TAMA = 30000142, 30000144, 30002187, 30002510, 30002813


def sig(hub: str, system: int, hours: int = 20) -> dict:
    hub_id = {"Thera": 31000005, "Turnur": 30002086}[hub]
    return {
        "out_system_name": hub,
        "out_system_id": hub_id,
        "in_system_id": system,
        "in_system_name": "",
        "wh_type": "K162",
        "max_ship_size": "large",
        "remaining_hours": hours,
    }


def rank(c: httpx.Client, target: str, thera: int) -> dict:
    return c.get(f"/api/rank?target={target}&thera={thera}&refresh=2").json()


def test_thera_route_replaces_the_gate_route_only_when_shorter(tmp_path):
    port = free_port()
    with fake_eve(characters=2, systems=str(PERIMETER)) as eve:
        httpx.post(f"{eve}/_admin/evescout", json={"signatures": [sig("Thera", JITA), sig("Thera", AMARR)]})
        env = evedm_env(
            tmp_path / "data",
            eve,
            port,
            EVEDM_EVESCOUT_URL=f"{eve}/evescout/signatures",
            EVEDM_EVESCOUT_REFRESH_S="0",
            EVEDM_REFRESH_INTERVAL_S="0",
            EVEDM_MAX_WAIT_AGE_S="0",
        )
        with evedm_server(env, log_path=tmp_path / "server.out") as (base, _):
            c = httpx.Client(base_url=base, headers={"X-EVEDM": "1"}, timeout=30)
            assert sso_flow(c, "/sso/login", FIRST).status_code == 303
            assert 'id="thera"' in c.get("/").text

            plain = rank(c, "Amarr", 0)
            row0 = plain["rows"][0]
            assert plain["evescout"] is None and row0["wormhole"] is None
            assert row0["jumps"] == 12 and [leg["mode"] for leg in row0["route_legs"]] == ["gate"]

            via = rank(c, "Amarr", 1)
            assert via["evescout"]["connections"] == 2 and via["evescout"]["error"] is None
            row = via["rows"][0]
            assert row["jumps"] == 3  # Perimeter -> Jita, wormhole in and out, Amarr
            assert row["wormhole"]["used"] and row["wormhole"]["hub"] == "Thera"
            assert row["wormhole"]["direct_jumps"] == 12
            legs = row["route_legs"]
            assert [leg["mode"] for leg in legs] == ["gate", "wormhole", "gate"]
            assert legs[0]["ids"] == [PERIMETER, JITA] and legs[1]["ids"] == [JITA, AMARR] and legs[2]["ids"] == [AMARR]
            assert row["route"] == [PERIMETER, JITA, AMARR]

            # a target the gates reach faster keeps its gate route; the hub only shows as a note
            near = rank(c, "Jita", 1)["rows"][0]
            assert near["jumps"] == 1 and near["route_legs"][0]["mode"] == "gate"
            assert not (near["wormhole"] or {}).get("used")

            lst = c.get("/api/evescout?refresh=1").json()
            assert len(lst["connections"]) == 2 and {x["hub"] for x in lst["connections"]} == {"Thera"}


def test_closest_listed_system_and_security_tie_break_follow_the_list(tmp_path):
    port = free_port()
    with fake_eve(characters=1, systems=str(PERIMETER)) as eve:
        httpx.post(
            f"{eve}/_admin/evescout",
            json={"signatures": [sig("Turnur", TAMA), sig("Turnur", RENS), sig("Turnur", AMARR)]},
        )
        env = evedm_env(
            tmp_path / "data", eve, port, EVEDM_EVESCOUT_URL=f"{eve}/evescout/signatures", EVEDM_EVESCOUT_REFRESH_S="0"
        )
        with evedm_server(env, log_path=tmp_path / "server.out") as (base, _):
            c = httpx.Client(base_url=base, headers={"X-EVEDM": "1"}, timeout=30)
            sso_flow(c, "/sso/login", FIRST)
            row = rank(c, "Rens", 1)["rows"][0]
            wh = row["wormhole"]
            assert wh["used"] and wh["hub"] == "Turnur" and wh["direct_jumps"] == 14
            assert wh["entry"]["name"] == "Tama" and wh["exit"]["name"] == "Rens"  # nearest to each end
            assert row["jumps"] == 6 and row["route"][0] == PERIMETER and row["route"][-1] == RENS


def test_list_is_kept_when_eve_scout_goes_down_and_off_switch_hides_the_filter(tmp_path):
    port = free_port()
    with fake_eve(characters=1, systems=str(PERIMETER)) as eve:
        httpx.post(f"{eve}/_admin/evescout", json={"signatures": [sig("Thera", JITA), sig("Thera", AMARR)]})
        env = evedm_env(
            tmp_path / "data", eve, port, EVEDM_EVESCOUT_URL=f"{eve}/evescout/signatures", EVEDM_EVESCOUT_REFRESH_S="0"
        )
        with evedm_server(env, log_path=tmp_path / "server.out") as (base, _):
            c = httpx.Client(base_url=base, headers={"X-EVEDM": "1"}, timeout=30)
            sso_flow(c, "/sso/login", FIRST)
            assert rank(c, "Amarr", 1)["rows"][0]["wormhole"]["used"]
            httpx.post(f"{eve}/_admin/evescout", json={"down": True})
            after = rank(c, "Amarr", 1)
            assert after["evescout"]["error"] and after["evescout"]["connections"] == 2
            assert after["rows"][0]["wormhole"]["used"]  # last list kept
    port = free_port()
    with fake_eve(characters=1, systems=str(PERIMETER)) as eve:
        env = evedm_env(tmp_path / "off", eve, port, EVEDM_EVESCOUT_ENABLED="0")
        with evedm_server(env, log_path=tmp_path / "off.out") as (base, _):
            c = httpx.Client(base_url=base, headers={"X-EVEDM": "1"}, timeout=30)
            sso_flow(c, "/sso/login", FIRST)
            assert 'id="thera"' not in c.get("/").text
            assert c.get("/api/evescout").status_code == 404
            assert rank(c, "Amarr", 1)["evescout"] is None
