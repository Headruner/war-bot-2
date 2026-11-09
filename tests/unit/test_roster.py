"""Static roster (UC-12): import and merge, resolution, ranking without ESI, the alerts line and export.

Covers AC-15..AC-24 of the roster delta (ROS-01..ROS-22, ALR-11).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from evedm.config import Settings
from evedm.db import Database
from evedm.ranking import RankingService
from evedm.resolver import TargetResolver
from evedm.roster import RosterRejected, RosterService, norm_key, validate
from evedm.universe import GateGraph
from evedm.wars.config import WarsSettings
from evedm.web.app import create_app

H = {"X-EVEDM": "1"}
SCOPES = ["esi-location.read_location.v1"]


class NoEsi:
    """A state provider that fails the test if a roster ranking ever reaches for live data (D10, ROS-10)."""

    async def fetch(self, character):  # noqa: ANN001, ANN201
        raise AssertionError("a static roster entry must never be sent to ESI")


@pytest.fixture
def roster(db: Database, graph: GateGraph) -> RosterService:
    ranking = RankingService(graph, db, NoEsi(), avoid_names=["Zarzakh"])
    return RosterService(db, graph, TargetResolver(graph), ranking, "test-instance")


def entry(name: str, system: str, **kw: object) -> dict:
    return {"char_name": name, "star_system_name": system, **kw}


def file_of(entries: list[dict], **head: object) -> bytes:
    return json.dumps({"schema": "evedm-static-roster", "version": 1, **head, "characters": entries}).encode()


def names(rows: list[dict]) -> list[str]:
    return [r["name"] for r in rows]


# --------------------------------------------------------------------------- schema and file level
def test_example_file_from_the_spec_is_valid():
    doc = {
        "schema": "evedm-static-roster",
        "version": 1,
        "label": "Alt parking list",
        "characters": [
            {"char_name": "Tanya Vermaak", "star_system_name": "Perimeter", "tags": ["hauler"], "note": "Jita alt"},
            {"char_name": "Rork Sanhai", "star_system_name": "Osmon"},
        ],
    }
    assert validate(doc) == []


def test_schema_errors_report_json_paths():
    errors = validate({"schema": "evedm-static-roster", "characters": [{"char_name": "ab"}]})
    text = "\n".join(errors)
    assert "$.characters[0]" in text and "star_system_name" in text


def test_file_level_rejections(roster: RosterService):
    with pytest.raises(RosterRejected, match="JSON"):
        roster.import_file(b"{not json")
    with pytest.raises(RosterRejected) as err:  # unknown property: the whole file is rejected (ROS-02)
        roster.import_file(file_of([{"char_name": "Able Baker", "star_system_name": "Jita", "ship": "Rifter"}]))
    assert err.value.errors


def test_there_is_no_limit_on_file_size_or_entries(roster: RosterService):
    entries = [entry(f"Pilot Number {i:05d}", "Jita", note="x" * 100) for i in range(5000)]
    raw = file_of(entries)
    assert len(raw) > 256_000  # the old size limit
    assert roster.import_file(raw)["summary"]["Added"] == 5000  # the old limit was 1000 entries
    assert roster.db.count_roster(0) == 5000
    assert roster.import_file(file_of([entry("Eli Fenn", "Rens")]))["summary"]["Added"] == 1


def test_a_bare_array_is_the_same_schema(roster: RosterService):
    report = roster.import_file(json.dumps([entry("Able Baker", "Jita")]).encode())
    assert report["summary"]["Added"] == 1


# --------------------------------------------------------------------------- merge (ROS-04, AC-16)
def test_import_merges_and_keeps_local_settings(roster: RosterService):
    first = roster.import_file(file_of([entry("Able Baker", "Jita", tags=["hauler"]), entry("Cora Dane", "Amarr")]))
    assert first["summary"] == {"Added": 2, "Updated": 0, "Unresolved": 0, "Rejected": 0}
    roster.patch(0, norm_key("Cora Dane"), enabled=False, tags=["pvp"])

    second = roster.import_file(file_of([entry("able baker", "Perimeter"), entry("Cora Dane", "Rens")]))
    assert second["summary"] == {"Added": 0, "Updated": 2, "Unresolved": 0, "Rejected": 0}
    rows = {r["name"]: r for r in roster.list_view(0)}
    assert rows["able baker"]["system"]["name"] == "Perimeter"  # the name as uploaded last, system moved
    cora = rows["Cora Dane"]
    assert cora["system"]["name"] == "Rens"
    assert cora["enabled"] is False and cora["tags"] == ["pvp"]  # local settings survive the upload


def test_bad_entries_are_rejected_one_by_one(roster: RosterService):
    report = roster.import_file(
        file_of([entry("Ab", "Jita"), entry("Able Baker", "Jita"), entry("ABLE baker", "Amarr")])
    )
    assert report["summary"] == {"Added": 1, "Updated": 0, "Unresolved": 0, "Rejected": 2}
    rejected = [e for e in report["entries"] if e["result"] == "Rejected"]
    assert "3-37" in rejected[0]["reason"]
    assert "earlier in this file" in rejected[1]["reason"]
    assert roster.db.count_roster(0) == 1


# --------------------------------------------------------------------------- resolution (ROS-06, AC-17)
def test_unknown_system_is_stored_unresolved_with_suggestions(roster: RosterService):
    report = roster.import_file(file_of([entry("Able Baker", "Jit")]))
    assert report["summary"]["Unresolved"] == 1 and report["summary"]["Rejected"] == 0
    row = roster.list_view(0)[0]
    assert row["resolve_state"] == "not_found" and row["system"] is None
    assert "Jita" in row["suggestions"]

    fixed = roster.patch(0, norm_key("Able Baker"), system_name="Jita")
    assert fixed["resolve_state"] == "resolved" and fixed["system"]["name"] == "Jita"


def test_re_resolve_picks_up_a_corrected_universe(roster: RosterService):
    roster.import_file(file_of([entry("Able Baker", "Jit"), entry("Cora Dane", "Jita")]))
    assert roster.resolve_pending(0) == 0  # still unknown
    roster.db.update_roster(0, norm_key("Able Baker"), system_name_raw="Jita")
    assert roster.resolve_pending(0) == 1
    assert roster.stats(0)["unresolved"] == 0


def test_a_station_name_resolves_to_its_system(roster: RosterService):
    roster.import_file(file_of([entry("Able Baker", "Jita IV - Moon 4 - Caldari Navy Assembly Plant")]))
    assert roster.list_view(0)[0]["system"]["name"] == "Jita"


def test_several_systems_in_one_field_are_ambiguous(roster: RosterService):
    roster.import_file(file_of([entry("Able Baker", "Jita or Amarr")]))
    row = roster.list_view(0)[0]
    assert row["resolve_state"] == "ambiguous" and row["system"] is None
    assert row["suggestions"][:2] == ["Jita", "Amarr"]


# --------------------------------------------------------------------------- ranking (ROS-16..ROS-19)
def test_ranking_order_route_and_no_esi(roster: RosterService, graph: GateGraph):
    roster.import_file(
        file_of(
            [
                entry("Amarr Alt", "Amarr"),
                entry("Home Sitter", "Jita"),
                entry("Next Door", "Perimeter"),
                entry("Hole Dweller", "Thera"),
                entry("Lost Soul", "Qqqxyz"),
            ]
        )
    )
    result = roster.rank(graph.find("Jita"))
    assert names(result["rows"]) == ["Home Sitter", "Next Door", "Amarr Alt", "Hole Dweller", "Lost Soul"]
    rows = {r["name"]: r for r in result["rows"]}
    assert rows["Home Sitter"]["jumps"] == 0 and rows["Home Sitter"]["recommended"] is True
    assert rows["Next Door"]["jumps"] == 1
    assert rows["Next Door"]["route"] == [graph.ids[graph.find("Perimeter")], graph.ids[graph.find("Jita")]]
    assert rows["Hole Dweller"]["reachable"] is False and rows["Hole Dweller"]["system"]["name"] == "Thera"
    assert rows["Lost Soul"]["system"] is None
    # no ship, online state or destination button: a static row carries no live data at all (ROS-10)
    assert "ship_type" not in rows["Home Sitter"] and "can_set_route" not in rows["Home Sitter"]
    assert result["stats"]["reachable"] == 3


def test_disabled_entries_are_not_ranked(roster: RosterService, graph: GateGraph):
    roster.import_file(file_of([entry("Able Baker", "Jita"), entry("Cora Dane", "Amarr")]))
    roster.patch(0, norm_key("Cora Dane"), enabled=False)
    result = roster.rank(graph.find("Jita"))
    assert names(result["rows"]) == ["Able Baker"]
    assert result["excluded"] == [{"name": "Cora Dane", "reason": "disabled"}]


def test_ranking_can_be_filtered_by_tag(roster: RosterService, graph: GateGraph):
    roster.import_file(file_of([entry("Able Baker", "Jita", tags=["hauler"]), entry("Cora Dane", "Amarr")]))
    assert names(roster.rank(graph.find("Jita"), tag="hauler")["rows"]) == ["Able Baker"]


# --------------------------------------------------------------------------- scope and alerts
def test_the_pool_is_per_main_and_goes_with_the_account(roster: RosterService, db: Database):
    roster.import_file(file_of([entry("Able Baker", "Jita")]), owner_id=1)
    roster.import_file(file_of([entry("Cora Dane", "Amarr"), entry("Eli Fenn", "Rens")]), owner_id=2)
    assert names(roster.list_view(1)) == ["Able Baker"]
    assert names(roster.list_view(2)) == ["Cora Dane", "Eli Fenn"]
    db.delete_user(1)
    assert roster.list_view(1) == [] and db.count_roster(2) == 2


def test_closest_static_characters_for_an_alert(roster: RosterService, graph: GateGraph, db: Database):
    db.upsert_character(2112000001, "Able Baker", "OK", SCOPES, owner_id=0)  # a linked character of this main
    roster.import_file(
        file_of(
            [
                entry("Able Baker", "Amarr"),  # ROS-15: shadowed by the linked character above
                entry("Next Door", "Perimeter"),
                entry("Home Sitter", "Jita"),
                entry("Hole Dweller", "Thera"),
            ]
        )
    )
    assert roster.list_view(0)[0]["shadowed"] is True
    closest = RosterService.closest(roster.origins(0), roster.ranking.parents(graph.find("Jita")))
    assert closest == [
        {"name": "Home Sitter", "jumps": 0, "static": True},
        {"name": "Next Door", "jumps": 1, "static": True},
    ]


# --------------------------------------------------------------------------- export (ROS-14, AC-24)
def test_export_import_round_trip(roster: RosterService):
    roster.import_file(
        file_of([entry("Able Baker", "Jita", tags=["hauler"], note="main hauler"), entry("Cora", "Jit")])
    )
    roster.patch(0, norm_key("Able Baker"), enabled=False)
    doc = roster.export(0)
    assert doc["schema"] == "evedm-static-roster" and doc["exported_from"] == "test-instance"
    before = roster.list_view(0)

    roster.db.clear_roster(0)
    assert roster.import_file(json.dumps(doc).encode())["summary"]["Added"] == 2
    after = roster.list_view(0)
    assert [(r["name"], r["system_name"], r["tags"], r["note"], r["enabled"]) for r in after] == [
        (r["name"], r["system_name"], r["tags"], r["note"], r["enabled"]) for r in before
    ]


def test_exporting_an_empty_roster_is_refused(roster: RosterService):
    with pytest.raises(RosterRejected, match="empty"):
        roster.export(0)


# --------------------------------------------------------------------------- web API (ROS-01, ROS-12)
def web_settings(tmp_path) -> Settings:  # noqa: ANN001
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.secret_key = "roster-web-secret"
    s.eve.client_id, s.eve.client_secret = "cid", "csecret"
    s.eve.sso_base = s.eve.esi_base = "http://127.0.0.1:9"  # never reached: the roster makes no ESI call
    s.web.auth = "none"
    return s


@pytest.fixture
def client(tmp_path):  # noqa: ANN001, ANN201
    with TestClient(create_app(web_settings(tmp_path), WarsSettings(enabled=False))) as c:
        yield c


def upload(client: TestClient, entries: list[dict]):  # noqa: ANN201
    return client.post(
        "/api/roster/import", headers=H, files={"file": ("roster.json", file_of(entries), "application/json")}
    )


def test_roster_api_import_rank_and_manage(client: TestClient):
    resp = upload(client, [entry("Able Baker", "Jita"), entry("Cora Dane", "Jit"), entry("Ab", "Rens")])
    assert resp.status_code == 200
    assert resp.json()["summary"] == {"Added": 2, "Updated": 0, "Unresolved": 1, "Rejected": 1}

    listing = client.get("/api/roster").json()
    assert listing["stats"]["entries"] == 2 and listing["stats"]["unresolved"] == 1
    assert [r["name"] for r in client.get("/api/roster?state=unresolved").json()["rows"]] == ["Cora Dane"]

    ranked = client.get("/api/roster?target=Jita").json()
    assert ranked["rows"][0]["name"] == "Able Baker" and ranked["rows"][0]["jumps"] == 0
    assert client.get("/api/roster?target=Nowhere").status_code == 404

    key = "cora dane"
    moved = client.patch(f"/api/roster/{key}", headers=H, json={"system_name": "Amarr"})
    assert moved.json()["system"]["name"] == "Amarr"
    assert client.patch(f"/api/roster/{key}", headers=H, json={"enabled": "yes"}).status_code == 400
    assert client.patch("/api/roster/nobody", headers=H, json={"enabled": False}).status_code == 404

    export = client.get("/api/roster/export")
    assert export.status_code == 200 and len(export.json()["characters"]) == 2
    assert "attachment" in export.headers["content-disposition"]

    assert client.delete(f"/api/roster/{key}", headers=H).status_code == 200
    assert client.delete("/api/roster", headers=H).status_code == 400  # confirm required
    assert client.delete("/api/roster?confirm=1", headers=H).json()["removed"] == 1
    assert client.get("/api/roster").json()["stats"]["entries"] == 0


def test_roster_api_needs_the_csrf_header_and_reports_rejections(client: TestClient):
    assert client.post("/api/roster/import", files={"file": ("r.json", b"[]", "application/json")}).status_code == 403
    resp = client.post(
        "/api/roster/import", headers=H, files={"file": ("r.json", b"{not json", "application/json")}
    )
    assert resp.status_code == 422 and "JSON" in resp.json()["rejected"]


def test_roster_page_renders(client: TestClient):
    page = client.get("/roster")
    assert page.status_code == 200 and "Static roster" in page.text
