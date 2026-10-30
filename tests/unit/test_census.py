"""War census: every active war in New Eden (statistics) and the high-sec structures of both sides (loot list)."""

import random
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from evedm.config import ConfigError, Settings
from evedm.wars.census import TOMBSTONE, WarCensus, iso, war_state
from evedm.wars.config import WarsSettings, load_wars_settings
from evedm.wars.esi import WarEsiError
from evedm.wars.structures import Structure, StructureIndex
from evedm.web.app import create_app
from fake_war_esi import NAMES, FakeEsi, make_war

pytestmark = pytest.mark.anyio
NOW = datetime.now(UTC)
JITA, PERIMETER, TAMA, AMARR, HEK = 30000142, 30000144, 30002813, 30002187, 30002053
ALLIANCE, CORP_A, CORP_B, DEFENDER, ALLY = 99010015, 98000001, 98000002, 98845884, 98777777


def t(hours: float) -> str:
    return (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def wars_fixture() -> dict[int, dict]:
    """12 wars, newest ID last: a mix of finished, ending, pending, fighting and a years-old mutual war."""
    w = {}
    for i in range(12):
        w[700 + i] = make_war(700 + i, NOW - timedelta(days=200 - i), finished=t(-24 * (100 - i)))  # all finished
    w[701] = make_war(701, NOW - timedelta(days=900), mutual=True)  # old mutual war, still fighting
    w[705] = make_war(705, NOW - timedelta(days=30), retracted=t(-2), finished=t(20))  # retracted, ending
    w[709] = make_war(709, NOW - timedelta(days=3))  # fighting
    w[710] = make_war(710, NOW - timedelta(hours=2), started=t(22))  # pending
    w[709]["aggressor"].update(ships_killed=7, isk_destroyed=3e9)
    w[709]["allies"] = [{"corporation_id": ALLY}]
    del w[703]  # unknown to ESI: 404
    return w


def census_for(db, graph, esi, structures=None, **kw) -> WarCensus:
    ws = WarsSettings(**({"census_max_ids": 100, "census_pages_per_cycle": 1, "census_concurrency": 4} | kw))
    return WarCensus(ws, db, graph, esi, structures)


async def test_scan_walks_back_in_pages_and_keeps_active_wars(db, graph):
    esi = FakeEsi(wars_fixture(), page_size=4)  # 703 is not listed by ESI
    census = census_for(db, graph, esi)
    await census.run()  # newest page (711..708) + one page back (707..704)
    assert db.get_meta("census.examined") == "8" and db.get_meta("census.floor") == "704"
    await census.run()  # 702..700
    assert db.get_meta("census.examined") == "11" and db.get_meta("census.complete") is None
    await census.run()  # empty page: the scan is complete
    assert db.get_meta("census.complete") == "true"
    active = {r["war_id"]: r for r in census.active_rows()}
    assert set(active) == {701, 705, 709, 710}
    now_iso = iso()
    assert {w: war_state(r["started"], r["finished"], now_iso) for w, r in active.items()} == {
        701: "fighting",
        705: "ending",
        709: "fighting",
        710: "pending",
    }
    calls = len(esi.war_calls)
    await census.run()  # nothing new, nothing due: no war re-read
    assert len(esi.war_calls) == calls


async def test_unknown_war_is_a_tombstone_and_failures_retry_the_same_page(db, graph):
    wars = wars_fixture()
    esi = FakeEsi(wars, page_size=4)
    real = esi.war
    broken = {"on": True}

    async def war(wid):
        if wid == 703:
            raise WarEsiError("HTTP 404", 404)
        if wid == 702 and broken["on"]:
            raise WarEsiError("HTTP 503", 503)
        return await real(wid)

    esi.war = war
    esi.wars_db[703] = make_war(703, NOW)  # listed, but reading it answers 404
    census = census_for(db, graph, esi, census_pages_per_cycle=5)
    await census.run()
    assert db.get_meta("census.floor") == "704"  # the page with the 503 is not passed over
    assert db.query("SELECT finished FROM war_census WHERE war_id=703")[0][0] == TOMBSTONE
    broken["on"] = False
    await census.run()
    assert db.get_meta("census.complete") == "true"
    assert 702 in {r[0] for r in db.query("SELECT war_id FROM war_census")}
    assert 703 not in {r["war_id"] for r in census.active_rows()}


async def test_scan_stops_at_census_max_ids(db, graph):
    esi = FakeEsi(wars_fixture(), page_size=4)
    census = census_for(db, graph, esi, census_max_ids=6, census_pages_per_cycle=10)
    await census.run()
    assert db.get_meta("census.complete") == "true" and db.get_meta("census.examined") == "8"
    assert esi.list_calls == [None, 708]  # newest page, one page back, no more


async def test_active_wars_are_reread_and_drop_out_when_they_end(db, graph):
    wars = wars_fixture()
    esi = FakeEsi(wars, page_size=50)
    census = census_for(db, graph, esi, census_refresh_hours=6)
    await census.run()
    assert {r["war_id"] for r in census.active_rows()} == {701, 705, 709, 710}
    wars[709] = make_war(709, NOW - timedelta(days=3), retracted=t(-30), finished=t(-1))  # ended meanwhile
    wars[701]["defender"].update(ships_killed=99)
    db.execute("UPDATE war_census SET checked_at=0 WHERE war_id IN (701, 709)")  # due for a refresh
    await census.run()
    assert {r["war_id"] for r in census.active_rows()} == {701, 705, 710}
    assert db.query("SELECT defender_ships FROM war_census WHERE war_id=701")[0][0] == 99


async def test_statistics(db, graph):
    esi = FakeEsi(wars_fixture(), page_size=50)
    census = census_for(db, graph, esi)
    await census.run()
    s = census.stats()
    t_ = s["totals"]
    assert (t_["active"], t_["fighting"], t_["pending"], t_["ending"], t_["mutual"]) == (4, 2, 1, 1, 1)
    assert t_["declared_24h"] == 1 and t_["declared_7d"] == 2 and t_["with_allies"] == 1
    assert t_["ships_killed"] == 7 and t_["isk_destroyed"] == 3e9
    assert s["top_aggressors"] == [{"id": ALLIANCE, "name": "BLACKFLAG.", "type": "alliance", "wars": 4}]
    assert s["top_defenders"][0]["name"] == "islands in the stream" and s["top_defenders"][0]["wars"] == 4
    assert s["top_isk"] == [
        {
            "war_id": 709,
            "aggressor": "BLACKFLAG.",
            "defender": "islands in the stream",
            "isk_destroyed": 3e9,
            "ships_killed": 7,
            "state": "fighting",
        }
    ]
    assert s["census"]["complete"] is True and s["census"]["known_wars"] == 11
    assert s["structures"] == {"highsec": 0, "by_type": [], "by_region": [], "loaded": False}


def structure_index(graph) -> StructureIndex:
    idx = StructureIndex(graph, "http://unused", "ua")
    idx.index = {
        CORP_A: [  # member of the aggressor alliance
            Structure(1, "Jita - Trade Hub", 35832, JITA, CORP_A),
            Structure(2, "Tama - Lowsec Base", 35832, TAMA, CORP_A),  # low-sec: never listed
        ],
        DEFENDER: [
            Structure(3, "Amarr - Fort", 35833, AMARR, DEFENDER),
            Structure(4, "Hek - Yard", 35825, HEK, DEFENDER),
        ],
        ALLY: [Structure(5, "Perimeter - Ally Post", 35832, PERIMETER, ALLY)],
        12345: [Structure(6, "Jita - Bystander", 35832, JITA, 12345)],  # not at war
    }
    idx.last_refresh = 9e18
    return idx


async def test_loot_lists_high_sec_structures_of_both_sides_nearest_first(db, graph):
    NAMES.update(
        {
            35832: ("Astrahus", "inventory_type"),
            35833: ("Fortizar", "inventory_type"),
            35825: ("Raitaru", "inventory_type"),
            ALLY: ("Allied Corp", "corporation"),
        }
    )
    wars = {
        709: make_war(709, NOW - timedelta(days=3), allies=[{"corporation_id": ALLY}]),
        710: make_war(710, NOW - timedelta(hours=2), started=t(22)),
    }
    esi = FakeEsi(wars)
    esi.alliance_members = {ALLIANCE: [CORP_A, CORP_B]}
    census = census_for(db, graph, esi, structure_index(graph))
    await census.run()
    sources = {graph.index[JITA]: ["Alpha"], graph.index[HEK]: ["Bravo", "Charlie"]}
    res = census.loot(sources, [])
    rows = res["rows"]
    assert [r["structure_id"] for r in rows][:2] in ([1, 4], [4, 1])  # both 0 jumps
    by_id = {r["structure_id"]: r for r in rows}
    assert set(by_id) == {1, 3, 4, 5}  # low-sec Tama and the bystander are not listed
    assert by_id[1]["sides"] == ["aggressor"] and by_id[1]["owner"] == "Flag Corp One"
    assert by_id[1]["entities"][0]["name"] == "BLACKFLAG." and by_id[1]["name"] == "Trade Hub"
    assert by_id[1]["state"] == "fighting"  # fighting wins over the pending war
    assert {w["state"] for w in by_id[1]["wars"]} == {"fighting", "pending"}
    assert by_id[5]["sides"] == ["defender"] and by_id[5]["wars"][0]["side"] == "ally"
    assert by_id[4]["jumps"] == 0 and by_id[4]["nearest"] == {"system": "Hek", "characters": ["Bravo", "Charlie"]}
    assert by_id[5]["jumps"] == 1 and by_id[5]["nearest"]["system"] == "Jita"
    assert by_id[3]["type"] == "Fortizar" and by_id[3]["wars"][0]["opponent"] == "BLACKFLAG."
    assert res["facets"]["total"] == 4 and dict(res["facets"]["types"]) == {"Astrahus": 2, "Fortizar": 1, "Raitaru": 1}

    def ids(**kw):
        return [r["structure_id"] for r in census.loot(sources, [], **kw)["rows"]]

    assert ids(side="aggressor") == [1]
    assert sorted(ids(side="defender")) == [3, 4, 5]
    assert ids(type_name="Fortizar") == [3]
    assert ids(region="Metropolis") == [4]
    assert ids(state="pending") == []
    assert sorted(ids(min_sec=0.95)) == [3, 5]  # Amarr and Perimeter (1.0)
    assert ids(sort="owner")[0] == 5  # "Allied Corp" sorts first
    assert len(census.loot(sources, [], limit=2)["rows"]) == 2 and census.loot(sources, [], offset=3)["matched"] == 4
    nobody = census.loot({}, [])["rows"]
    assert all(r["jumps"] is None and r["nearest"] is None for r in nobody)


async def test_loot_is_empty_until_structures_are_loaded(db, graph):
    census = census_for(db, graph, FakeEsi({709: make_war(709, NOW)}), StructureIndex(graph, "http://x", "ua"))
    await census.run()
    res = census.loot({}, [])
    assert res["rows"] == [] and res["structures_loaded"] is False


def test_nearest_matches_single_source_bfs(graph):
    rng = random.Random(7)
    sources = [graph.index[s] for s in (JITA, AMARR, HEK)]
    avoid = [graph.find("Zarzakh")]
    dist, origin = graph.nearest(sources, avoid)
    for _ in range(40):
        target = rng.randrange(len(graph.ids))
        parent = graph.bfs(target, avoid)
        best = [len(p) - 1 for p in (graph.route(parent, s) for s in sources) if p]
        if best:
            assert dist[target] == min(best)
            assert origin[target] in sources and len(graph.route(parent, origin[target])) - 1 == min(best)
        else:
            assert dist[target] == -1 and origin[target] == -1
    assert dist[graph.index[JITA]] == 0 and graph.nearest([], [])[0][graph.index[JITA]] == -1


def test_census_settings_are_validated(tmp_path):
    cfg = tmp_path / "wars.yaml"
    for text in ("census_concurrency: 0\n", "census_pages_per_cycle: 0\n", "census_max_ids: -1\n"):
        cfg.write_text(text)
        with pytest.raises(ConfigError, match="census"):
            load_wars_settings(cfg)
    cfg.write_text("census_enabled: false\n")
    assert load_wars_settings(cfg).census_enabled is False


# --------------------------------------------------------------------------- web
def demo(tmp_path, **ws) -> TestClient:
    s = Settings()
    s.data_dir, s.secret_key, s.demo, s.web.auth = tmp_path / "d", "k", True, "none"
    return TestClient(create_app(s, WarsSettings(enabled=True, **ws)))


def test_stats_and_structures_api_in_demo(tmp_path):
    with demo(tmp_path) as c:
        stats = c.get("/api/wars/stats").json()
        assert stats["totals"]["active"] == 8 and stats["census"]["complete"] is True
        assert stats["structures"]["highsec"] == 8 and stats["top_aggressors"][0]["name"] == "Crimson Ledger Syndicate"
        body = c.get("/api/wars/structures").json()
        assert body["matched"] == 8 and body["characters"] == 9
        jumps = [r["jumps"] for r in body["rows"]]
        assert jumps == sorted(jumps) and body["rows"][0]["nearest"]["characters"]
        assert {r["system"]["class"] for r in body["rows"]} == {"high"}
        assert c.get("/api/wars/structures", params={"side": "aggressor"}).json()["matched"] == 1
        assert c.get("/api/wars/structures", params={"limit": 2, "offset": 7}).json()["rows"][0]
        assert c.get("/api/wars/structures", params={"min_sec": "high"}).status_code == 422
        assert c.get("/api/wars/990001").json()["war_id"] == 990001  # not shadowed by the new routes
        page = c.get("/wars").text
        assert 'id="tab-btn-stats"' in page and 'id="tab-btn-loot"' in page and "prefs.js" in page


def test_census_off_hides_its_tabs(tmp_path):
    with demo(tmp_path, census_enabled=False) as c:
        assert c.get("/api/wars/stats").status_code == 404
        assert c.get("/api/wars/structures").status_code == 404
        assert 'id="tab-btn-stats"' not in c.get("/wars").text


def test_structure_jumps_are_per_main(tmp_path):
    from test_accounts_web import SECRET, as_user, link, settings

    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=True, structures_enabled=True))) as c:
        svc = c.app.state.svc
        census = svc.wars.census
        census.structures.index = {DEFENDER: [Structure(4, "Hek - Yard", 35825, HEK, DEFENDER)]}
        census.structures.last_refresh = 9e18
        svc.db.execute(
            "INSERT INTO war_census(war_id,declared,started,aggressor_id,aggressor_type,defender_id,defender_type,"
            "checked_at) VALUES(1,?,?,?,?,?,?,?)",
            (t(-48), t(-24), ALLIANCE, "alliance", DEFENDER, "corporation", time.time()),
        )
        census.version += 1
        for main, system in ((2112000001, HEK), (2112000002, JITA)):
            svc.db.upsert_user(main, str(main))
            link(svc, main, main, str(main), system)
        assert SECRET
        near = as_user(c, 2112000001).get("/api/wars/structures", params={"refresh": 0}).json()["rows"][0]
        far = as_user(c, 2112000002).get("/api/wars/structures", params={"refresh": 0}).json()["rows"][0]
        assert near["jumps"] == 0 and near["nearest"]["characters"] == ["2112000001"]
        assert far["jumps"] > 0 and far["nearest"]["characters"] == ["2112000002"]
        assert as_user(c, None).get("/api/wars/structures").status_code == 401


def test_demo_census_is_removed_when_the_database_goes_live(tmp_path, monkeypatch):
    from evedm.wars.service import WarService

    monkeypatch.setattr(WarService, "start", lambda self: None)
    s = Settings()
    s.data_dir, s.secret_key, s.demo, s.web.auth = tmp_path / "d", "k", True, "none"
    with TestClient(create_app(s, WarsSettings(enabled=True))) as c:
        assert c.get("/api/wars/stats").json()["totals"]["active"] == 8
    s.demo = False
    s.eve.client_id, s.eve.client_secret = "cid", "sec"
    with TestClient(create_app(s, WarsSettings(enabled=True))) as c:
        assert c.get("/api/wars/stats").json()["totals"]["active"] == 0
        assert c.app.state.svc.db.get_meta("demo_census") is None
