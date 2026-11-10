"""Ansiblex jump gates on the Ranking page (UC-16): the gate name, routing over gates plus links, reading them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from common import AMARR, JITA, PERIMETER
from evedm.ansiblex import ANSIBLEX_TYPE_ID, AnsiblexService, parse_gate_name
from evedm.config import ANSIBLEX_SCOPE, STRUCTURE_SCOPES, AnsiblexSettings, Settings
from evedm.db import CharacterRow
from evedm.esi import EsiError
from evedm.evescout import min_size, normalize_size
from evedm.wars.config import WarsSettings
from evedm.web.app import create_app

pytestmark = pytest.mark.anyio
SCOPES = [*STRUCTURE_SCOPES, ANSIBLEX_SCOPE]


def test_the_destination_comes_from_the_gate_name():
    assert parse_gate_name("Jita » Perimeter - Gate One") == ("Jita", "Perimeter")
    assert parse_gate_name("1DQ1-A » 4-HWWF - BB Bridge") == ("1DQ1-A", "4-HWWF")
    assert parse_gate_name("Jita » Perimeter") == ("Jita", "Perimeter")
    assert parse_gate_name("Jita - Fort Knox") is None and parse_gate_name(None) is None


def test_ship_sizes_are_normalised_and_the_smaller_one_wins():
    assert [normalize_size(x) for x in ("small", "Medium", "LARGE", "xlarge", "X-Large", "capital", "?", None)] == [
        "small", "medium", "large", "xlarge", "xlarge", "capital", None, None,
    ]
    assert min_size("capital", "large") == "large" and min_size("xlarge", None) == "xlarge" and min_size(None) is None


# --------------------------------------------------------------------------- routing over gates plus links
def test_a_link_shortens_the_route_and_the_hop_is_reported(graph):
    jita, amarr = graph.index[JITA], graph.index[AMARR]
    plain = graph.route(graph.bfs(amarr), jita)
    assert plain is not None and len(plain) > 3
    path, hops = graph.route_with_links(jita, amarr, {jita: [amarr]})
    assert path == [jita, amarr] and hops == [0]
    # a link in the middle: gates, link, gates
    mid_from, mid_to = plain[2], plain[-3]
    path, hops = graph.route_with_links(jita, amarr, {mid_from: [mid_to]})
    assert path[:3] == plain[:3] and hops == [2] and path[-3:] == plain[-3:] and len(path) < len(plain)


def test_links_are_one_way_and_a_tie_keeps_the_stargates(graph):
    jita, amarr, perimeter = (graph.index[x] for x in (JITA, AMARR, PERIMETER))
    plain = graph.route(graph.bfs(amarr), jita)
    path, hops = graph.route_with_links(jita, amarr, {amarr: [jita]})  # the wrong way round
    assert path == plain and hops == []
    path, hops = graph.route_with_links(jita, perimeter, {jita: [perimeter]})  # as long as the gate: gate wins
    assert hops == [] and len(path) == 2


def test_avoided_systems_are_not_transit_nodes_but_can_be_the_target(graph):
    jita, amarr = graph.index[JITA], graph.index[AMARR]
    plain = graph.route(graph.bfs(amarr), jita)
    blocker = plain[len(plain) // 2]
    path, _ = graph.route_with_links(jita, amarr, {}, [blocker])
    assert blocker not in path[1:-1]
    assert graph.route_with_links(jita, blocker, {}, [blocker])[0][-1] == blocker


# --------------------------------------------------------------------------- reading the gates for a character
class StubEsi:
    """Character structure search + structure reads; records the calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.found = [1_029_000_000_001, 1_029_000_000_002, 1_029_000_000_003, 1_029_000_000_004]
        self.structures = {
            1_029_000_000_001: {"name": "Jita » Amarr - Gate", "type_id": ANSIBLEX_TYPE_ID, "solar_system_id": JITA,
                                "owner_id": 98000001},
            1_029_000_000_002: {"name": "Jita » Nowhere - Gate", "type_id": ANSIBLEX_TYPE_ID, "solar_system_id": JITA},
            1_029_000_000_003: {"name": "Jita » Amarr - Fort", "type_id": 35832, "solar_system_id": JITA},
        }
        self.fail_search: EsiError | None = None

    def blocked_for(self, reserve: int = 5) -> float:
        return 0.0

    async def get(self, path: str, token: str | None = None):  # noqa: ANN201
        self.calls.append(path)
        if "/search/" in path:
            if self.fail_search:
                raise self.fail_search
            return {"structure": self.found}
        sid = int(path.rsplit("/", 1)[1])
        if sid in self.structures:
            return self.structures[sid]
        raise EsiError(403, "forbidden")


class StubChars:
    async def access_token(self, character_id: int, owner_id: int = 0) -> str:
        return "token"


def char(cid: int = 2100000001, scopes=None, status: str = "OK") -> CharacterRow:  # noqa: ANN001
    return CharacterRow(cid, f"Char {cid}", True, [], status, SCOPES if scopes is None else scopes, None, False, 0.0, 0)


@pytest.fixture
def svc(db, graph):  # noqa: ANN001, ANN201
    return AnsiblexService(AnsiblexSettings(), db, graph, StubChars(), StubEsi())  # type: ignore[arg-type]


async def test_a_character_gets_the_gates_it_can_see(svc, db, graph):
    c = char()
    assert svc.status_for(c, svc.checks()) == "pending" and [x.character_id for x in svc.stale([c])] == [c.character_id]
    await svc.ensure([c], wait_s=5)
    assert svc.status_for(c, svc.checks()) == "ok"
    # the search needs three characters: " » " (a lone sign would be refused), and only the first one is a usable gate
    search = svc.esi.calls[0]  # type: ignore[union-attr]
    assert "categories=structure" in search and "search=%20%C2%BB%20" in search
    assert svc.links_for(c.character_id) == {graph.index[JITA]: [graph.index[AMARR]]}
    states = {r["structure_id"]: r["status"] for r in db.query("SELECT structure_id, status FROM ansiblex_gates")}
    assert states == {1_029_000_000_001: "ok", 1_029_000_000_002: "not_gate", 1_029_000_000_003: "not_gate",
                      1_029_000_000_004: "no_access"}
    assert svc.gate_count() == 1
    # fresh: no second round, and the gates are not read again
    calls = len(svc.esi.calls)  # type: ignore[union-attr]
    await svc.ensure([c], wait_s=5)
    assert len(svc.esi.calls) == calls and svc.stale([c]) == []  # type: ignore[union-attr]


async def test_gates_are_per_character(svc, graph):
    a, b = char(1), char(2)
    await svc.ensure([a], wait_s=5)
    assert svc.links_for(1) and svc.links_for(2) == {}
    svc.esi.found = []  # type: ignore[union-attr]
    svc.db.execute("UPDATE ansiblex_characters SET checked_at=0")
    await svc.ensure([a, b], wait_s=5)
    assert svc.links_for(1) == {} and svc.links_for(2) == {}  # the gate access is gone again


async def test_missing_scope_and_search_errors_are_reported(svc):
    no_scope = char(3, scopes=[STRUCTURE_SCOPES[0]])
    assert svc.status_for(no_scope, svc.checks()) == "no_scope" and svc.stale([no_scope]) == []
    reauth = char(4, status="REAUTH")
    assert svc.stale([reauth]) == []
    svc.esi.fail_search = EsiError(400, "search too short")  # type: ignore[union-attr]
    c = char(5)
    await svc.ensure([c], wait_s=5)
    assert svc.status_for(c, svc.checks()) == "error" and svc.state["last_error"]
    assert svc.links_for(5) == {}


# --------------------------------------------------------------------------- the Ranking API (demo gates)
def demo_client(tmp_path, **kw):  # noqa: ANN001, ANN201
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.secret_key = "ansiblex-secret"
    s.demo = True
    s.web.auth = "none"
    s.evescout.enabled = False
    for k, v in kw.items():
        setattr(s.ansiblex, k, v)
    return TestClient(create_app(s, WarsSettings(enabled=False)))


def test_the_ansiblex_button_routes_over_the_gates_of_each_character(tmp_path):
    with demo_client(tmp_path) as c:
        plain = {r["name"]: r for r in c.get("/api/rank?target=Amarr&refresh=0").json()["rows"]}
        res = c.get("/api/rank?target=Amarr&refresh=0&hub=ansiblex").json()
        rows = {r["name"]: r for r in res["rows"]}
        assert res["ansiblex"]["gates_known"] == 4 and res["evescout"] is None
        kael = rows["Kael Varn"]  # parked in Jita, where the demo gate starts
        assert plain["Kael Varn"]["jumps"] > 1 and plain["Kael Varn"]["ansiblex"] is None
        assert kael["jumps"] == 1 and kael["ansiblex"]["used"] and kael["ansiblex"]["gates"] == 4
        assert kael["ansiblex"]["direct_jumps"] == plain["Kael Varn"]["jumps"]
        assert [leg["mode"] for leg in kael["route_legs"]] == ["gate", "ansiblex", "gate"]
        hop = kael["ansiblex"]["hops"][0]
        assert hop["from"]["name"] == "Jita" and hop["to"]["name"] == "Amarr"
        assert res["rows"][0]["recommended"] and res["rows"][0]["jumps"] == 0  # a demo character already in Amarr


def test_the_ansiblex_filter_can_be_switched_off(tmp_path):
    with demo_client(tmp_path, enabled=False) as c:
        assert c.get("/api/rank?target=Amarr&refresh=0&hub=ansiblex").status_code == 422
        assert c.get("/api/rank?target=Amarr&refresh=0").status_code == 200


def test_the_default_scopes_ask_for_the_character_search():
    assert ANSIBLEX_SCOPE in Settings().eve.optional_scopes
