"""EVE-Scout Thera / Turnur list and the two-leg route (UC-15, EVS-01..EVS-10)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from evedm.config import EveScoutSettings, Settings
from evedm.evescout import EveScout
from evedm.web.app import create_app

H = {"X-EVEDM": "1"}


def sig(hub, system, hours=20, **extra):
    return {
        "out_system_name": hub,
        "out_system_id": {"Thera": 31000005, "Turnur": 30002086}.get(hub, 1),
        "in_system_name": system,
        "wh_type": "K162",
        "max_ship_size": "large",
        "remaining_hours": hours,
        **extra,
    }


def scout(graph, payload, **kw):
    s = EveScout(EveScoutSettings(), graph, None)
    s.load(payload)
    return s


def names(graph, ids):
    return [graph.names[i] for i in ids]


def test_load_filters_hubs_wspace_and_expired(graph):
    wspace = next(graph.names[i] for i in range(len(graph.ids)) if graph.is_wspace(i) and graph.names[i] != "Thera")
    s = scout(
        graph,
        [
            sig("Thera", "Jita"),
            sig("Thera", "Jita", hours=5),  # duplicate: the longer-lived one is kept
            sig("Thera", "Amarr", hours=0),  # expired
            sig("Thera", wspace),  # J-space: no gates
            sig("Thera", "Nowhere-123"),  # unknown
            sig("Turnur", "Rens"),
            sig("Other", "Hek"),  # not a configured hub
            "junk",
        ],
    )
    assert {h: len(v["systems"]) for h, v in s.hubs.items()} == {"Thera": 1, "Turnur": 1}
    assert s.hubs["Thera"]["links"][graph.find("Jita")]["remaining_hours"] == 20
    assert s.status()["connections"] == 2
    assert {row["hub"] for row in s.listing()} == {"Thera", "Turnur"}


def test_load_orders_by_security_then_name(graph):
    s = scout(graph, [sig("Thera", n) for n in ("Rens", "Jita", "Amarr", "Tama")])
    secs = [graph.sec[i] for i in s.hubs["Thera"]["systems"]]
    assert secs == sorted(secs, reverse=True)


def test_via_uses_closest_listed_system_each_side(graph):
    s = scout(graph, [sig("Thera", "Jita"), sig("Thera", "Amarr")])
    plan = s.via(graph.find("Perimeter"), graph.find("Amarr"))
    assert plan and plan["hub"] == "Thera"
    assert names(graph, plan["to_entry"]) == ["Perimeter", "Jita"]
    assert plan["entry"] == graph.find("Jita") and plan["exit"] == graph.find("Amarr")
    assert plan["from_exit"] == [graph.find("Amarr")]
    assert plan["jumps"] == 1 + 2 + 0  # shorter than the 12 gate jumps


def test_via_walks_gates_on_both_sides(graph):
    s = scout(graph, [sig("Turnur", "Jita"), sig("Turnur", "Amarr")])
    origin, target = graph.find("Ikuchi"), graph.find("Sarum Prime")
    plan = s.via(origin, target)
    assert plan
    assert plan["to_entry"][0] == origin and plan["to_entry"][-1] == plan["entry"]
    assert plan["from_exit"][0] == plan["exit"] and plan["from_exit"][-1] == target
    assert plan["jumps"] == len(plan["to_entry"]) - 1 + 2 + len(plan["from_exit"]) - 1


def test_equal_distance_prefers_higher_security(graph):
    origin = graph.find("Jita")
    dist = graph.bfs(origin)  # parent pointers; derive distances with route()
    ring = {}
    for i in range(len(graph.ids)):
        if graph.is_wspace(i) or i == origin:
            continue
        try:
            d = len(graph.route(dist, i)) - 1
        except Exception:
            continue
        if d == 3:
            ring[i] = d
    a, b = next((x, y) for x in ring for y in ring if x < y and graph.sec[x] != graph.sec[y])
    far = graph.find("Amarr")
    s = scout(graph, [sig("Thera", graph.names[a]), sig("Thera", graph.names[b]), sig("Thera", "Amarr")])
    plan = s.via(origin, far)
    assert plan["entry"] == (a if graph.sec[a] > graph.sec[b] else b)


def test_same_entry_and_exit_is_skipped(graph):
    s = scout(graph, [sig("Thera", "Jita")])
    assert s.via(graph.find("Perimeter"), graph.find("Ikuchi")) is None


def test_best_hub_is_the_shorter_one(graph):
    s = scout(
        graph,
        [sig("Thera", "Jita"), sig("Thera", "Rens"), sig("Turnur", "Jita"), sig("Turnur", "Amarr")],
    )
    plan = s.via(graph.find("Jita"), graph.find("Amarr"))
    assert plan["hub"] == "Turnur" and plan["jumps"] == 2


def test_avoided_system_is_not_used_as_transit(graph):
    s = scout(graph, [sig("Thera", "Jita"), sig("Thera", "Amarr")])
    perimeter, amarr, jita = graph.find("Perimeter"), graph.find("Amarr"), graph.find("Jita")
    plan = s.via(perimeter, amarr, avoid=[jita])
    assert plan is None or jita not in plan["to_entry"][:-1]


@pytest.mark.anyio
async def test_ensure_fresh_ttl_and_keep_last_on_error(graph):
    calls = {"n": 0, "down": False}

    def handler(request):
        calls["n"] += 1
        if calls["down"]:
            return httpx.Response(503)
        return httpx.Response(200, json=[sig("Thera", "Jita"), sig("Turnur", "Tama")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        s = EveScout(EveScoutSettings(refresh_s=3600), graph, http)
        await s.ensure_fresh()
        await s.ensure_fresh()
        assert calls["n"] == 1 and s.status()["connections"] == 2 and s.error is None
        calls["down"] = True
        await s.ensure_fresh(force=True)
        assert calls["n"] == 2 and s.error and s.status()["connections"] == 2  # old list kept
        await s.ensure_fresh()
        assert calls["n"] == 2  # no hammering


@pytest.fixture
def anyio_backend():
    return "asyncio"


def demo_client(tmp_path, enabled=True):
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.secret_key = "evs-test"
    s.demo = True
    s.web.auth = "none"
    s.evescout.enabled = enabled
    return TestClient(create_app(s))


def test_rank_with_thera_adds_wormhole_route(tmp_path):
    with demo_client(tmp_path) as c:
        plain = c.get("/api/rank?target=Amarr", headers=H).json()
        thera = c.get("/api/rank?target=Amarr&thera=1", headers=H).json()
        assert plain["evescout"] is None and thera["evescout"]["connections"] == 6
        assert all(r["wormhole"] is None for r in plain["rows"])
        used = [r for r in thera["rows"] if r["wormhole"] and r["wormhole"]["used"]]
        assert used, "at least one character is closer through Thera/Turnur"
        row = used[0]
        modes = [leg["mode"] for leg in row["route_legs"]]
        assert modes in (["gate", "wormhole", "gate"],)
        base = next(r for r in plain["rows"] if r["name"] == row["name"])
        assert row["jumps"] < base["jumps"]
        assert row["route_legs"][1]["hub"] in ("Thera", "Turnur")
        for r in thera["rows"]:
            if r["reachable"] and plain_row(plain, r)["reachable"]:
                assert r["jumps"] <= plain_row(plain, r)["jumps"]


def plain_row(plain, r):
    return next(p for p in plain["rows"] if p["name"] == r["name"])


def test_evescout_endpoint_and_disabled(tmp_path):
    with demo_client(tmp_path) as c:
        body = c.get("/api/evescout", headers=H).json()
        assert body["status"]["demo"] is True and len(body["connections"]) == 6
        assert c.get("/").status_code == 200 and 'id="thera"' in c.get("/").text
    with demo_client(tmp_path / "off", enabled=False) as c:
        assert c.get("/api/evescout", headers=H).status_code == 404
        assert 'id="thera"' not in c.get("/").text
        assert c.get("/api/rank?target=Amarr&thera=1", headers=H).json()["evescout"] is None
