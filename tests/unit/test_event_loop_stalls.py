"""The war census at production size must not block the event loop (a stall every 15 minutes, 3.6-5.8 s), and the
event-loop monitor names the activity it happened in."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import pytest

from evedm.activity import activity, describe
from evedm.looplag import LoopLagMonitor
from evedm.wars import census as census_mod
from evedm.wars.census import WarCensus
from evedm.wars.config import WarsSettings
from evedm.wars.structures import Structure, StructureIndex

pytestmark = pytest.mark.anyio

# Production size: about 20 000 war IDs examined, half of them active, 2 000 alliances with up to 120 corporations,
# 19 000 public structures of 6 000 owners, 40 000 corporations.
WARS, ALLIANCES, CORPS, STRUCTURES = 20000, 1500, 40000, 19000
BOUND_S = 1.0  # the largest gap between two ticks of the event loop: measured 0.15-0.2 s, 6 s with the old code


class BigEsi:
    """The ESI answers of a steady-state census step: the 2 000 newest IDs and 6 that nobody stored yet."""

    def __init__(self, db) -> None:  # noqa: ANN001
        self.db = db

    async def wars(self, max_war_id: int | None = None) -> list[int]:
        if max_war_id is not None:
            return []
        ids = [r[0] for r in self.db.query("SELECT war_id FROM war_census ORDER BY war_id DESC LIMIT 2000")]
        return [ids[0] + k + 1 for k in range(6)] + ids

    async def war(self, wid: int) -> dict:
        return {
            "declared": "2026-11-12T00:00:00Z", "started": "2026-11-13T00:00:00Z", "mutual": False,
            "open_for_allies": False,
            "aggressor": {"corporation_id": 98000005, "ships_killed": 0, "isk_destroyed": 0},
            "defender": {"alliance_id": 99000007, "ships_killed": 0, "isk_destroyed": 0},
        }

    async def alliance_corporations(self, aid: int) -> list[int]:
        return []

    async def names(self, ids) -> dict:  # noqa: ANN001
        return {int(i): (f"New {i}", "corporation") for i in ids}


def big_census(db, graph) -> WarCensus:  # noqa: ANN001
    rnd = random.Random(1)
    now = time.time()
    alliances = [99000000 + i for i in range(ALLIANCES)]
    corps = [98000000 + i for i in range(CORPS)]
    rows = []
    for i in range(WARS):
        agg_alliance, dfn_alliance = rnd.random() < 0.4, rnd.random() < 0.4
        allies = [
            [rnd.choice(alliances) if rnd.random() < 0.5 else rnd.choice(corps), "alliance"]
            for _ in range(rnd.choice([0, 0, 0, 1, 2, 5]))
        ]
        rows.append(
            (
                100000 + i, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
                None if rnd.random() < 0.5 else "2026-02-01T00:00:00Z", None,
                int(rnd.random() < 0.3), int(rnd.random() < 0.5),
                rnd.choice(alliances) if agg_alliance else rnd.choice(corps),
                "alliance" if agg_alliance else "corporation", rnd.randint(0, 500), rnd.random() * 1e10,
                rnd.choice(alliances) if dfn_alliance else rnd.choice(corps),
                "alliance" if dfn_alliance else "corporation", rnd.randint(0, 500), rnd.random() * 1e10,
                json.dumps(allies), now,
            )
        )
    with db.tx() as c:
        c.executemany("INSERT INTO war_census VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.executemany(
            "INSERT INTO alliance_corps(alliance_id,corps,updated_at) VALUES(?,?,?)",
            [(a, json.dumps(rnd.sample(corps, rnd.choice([3, 8, 15, 40, 120]))), now) for a in alliances],
        )
        c.executemany(
            "INSERT OR REPLACE INTO entity_names(entity_id,name,category,updated_at) VALUES(?,?,?,?)",
            [(e, f"Entity {e}", "corporation", now) for e in corps + alliances],
        )
    systems = list(graph.ids)
    owners = rnd.sample(corps, 6000)
    public: dict[int, list[Structure]] = {}
    for k in range(STRUCTURES):
        o = rnd.choice(owners)
        public.setdefault(o, []).append(
            Structure(1_030_000_000_000 + k, f"S - {k}", 35832, int(rnd.choice(systems)), o)
        )
    index = StructureIndex(graph, "", "ua")
    index.set_public(public)
    index.last_refresh = now
    census = WarCensus(WarsSettings(), db, graph, BigEsi(db), index)  # type: ignore[arg-type]
    census._set_meta("floor", 100000)
    census._set_meta("examined", WARS)
    census._set_meta("complete", "true")
    return census


async def test_a_census_step_at_production_size_leaves_the_event_loop_free(db, graph):
    census = big_census(db, graph)
    monitor = LoopLagMonitor(interval_s=0.01)
    monitor.start()
    await asyncio.sleep(0.05)
    result = await census.run()
    await asyncio.sleep(0.05)
    await monitor.stop()
    assert result["new"] == 6 and result["failed"] == 0  # the step did its work
    # the largest overshoot of a 10 ms sleep over the whole step; the old per-ID `_known` call was one block of 6 s
    assert monitor.max_s() < BOUND_S, f"event loop blocked for {monitor.max_s():.2f} s"
    assert monitor.snapshot()["samples"] > 20  # and the loop really turned many times during it


async def test_the_census_caches_are_warm_after_a_step(db, graph):
    census = big_census(db, graph)
    await census.run()
    t0 = time.perf_counter()
    census.corps_at_war()  # served from the cache the step built
    census._loot_base()
    assert time.perf_counter() - t0 < 0.05


async def test_corps_at_war_is_cached_until_the_census_changes_or_a_war_ends(db, graph, monkeypatch):
    now = time.time()
    with db.tx() as c:
        c.execute(
            "INSERT INTO war_census(war_id,declared,started,finished,aggressor_id,aggressor_type,defender_id,"
            "defender_type,allies,checked_at) VALUES(1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',"
            "'2999-01-01T00:00:00Z',98000001,'corporation',98000002,'corporation','[]',?)",
            (now,),
        )
        c.execute(
            "INSERT INTO war_census(war_id,declared,started,finished,aggressor_id,aggressor_type,defender_id,"
            "defender_type,allies,checked_at) VALUES(2,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',"
            "'2026-12-01T00:00:00Z',98000001,'corporation',98000003,'corporation','[[98000009,\"corporation\"]]',?)",
            (now,),
        )
    census = WarCensus(WarsSettings(), db, graph, BigEsi(db), None)  # type: ignore[arg-type]
    first = census.corps_at_war()
    assert first == {98000001: [2, 1], 98000002: [1], 98000003: [2], 98000009: [2]}
    assert census.corps_at_war() is first  # cached
    census.version += 1  # the census stored something
    assert census.corps_at_war() is not first
    again = census.corps_at_war()
    monkeypatch.setattr(census_mod, "iso", lambda t=None: "2026-12-02T00:00:00Z")  # war 2 has ended since
    after = census.corps_at_war()
    assert after is not again and after == {98000001: [1], 98000002: [1]}


# --------------------------------------------------------------------------- naming the culprit
def test_activities_nest_and_always_close():
    assert describe() is None
    with activity("wars.cycle"):
        with activity("wars.census"):
            text = describe()
            assert text.startswith("wars.census (running ") and "wars.cycle (running " in text
        assert describe().startswith("wars.cycle")
    with pytest.raises(RuntimeError):
        with activity("scout.run"):
            raise RuntimeError("boom")
    assert describe() is None  # an error leaves nothing open


def test_the_warning_names_the_activity_it_happened_in(caplog):
    mon = LoopLagMonitor()
    with caplog.at_level(logging.WARNING, logger="evedm.looplag"):
        with activity("wars.cycle"):
            mon.record(4.6, 1000.0)
        mon.record(4.7, 2000.0)  # nothing open: no "during"
    first, second = (r.getMessage() for r in caplog.records)
    assert "blocked for 4.6 s during wars.cycle (running " in first
    assert "blocked for 4.7 s (max" in second and "during" not in second


async def test_every_service_marks_its_work(settings, services, graph, db):
    from evedm.alerts import AlertService
    from evedm.notifications import NotificationService
    from evedm.scout import StructureScout
    from evedm.wars.service import WarService
    from fake_war_esi import FakeEsi

    seen: dict[str, str | None] = {}

    def stop_after(name: str, svc) -> None:  # noqa: ANN001
        async def fake(*a, **k):  # noqa: ANN002, ANN003, ANN202
            seen[name] = describe()
            svc._stop.set()  # the loop ends after this round

        return fake

    ns = NotificationService(settings.notifications, db, graph, services["chars"], services["esi"])
    ns.poll_once = stop_after("notifications", ns)  # type: ignore[method-assign]
    await ns._loop()
    wars = WarService(WarsSettings(), db, graph, FakeEsi({}))
    wars.run_cycle = stop_after("wars", wars)  # type: ignore[method-assign]
    await wars._loop()
    scout = StructureScout(settings, db, graph, services["chars"], services["esi"])
    scout.run = stop_after("scout", scout)  # type: ignore[method-assign]
    await scout._loop()
    alerts = AlertService(settings.alerts, db, graph, services["esi"])
    alerts.poll_once = stop_after("alerts", alerts)  # type: ignore[method-assign]
    await alerts._loop()
    assert {k: (v or "").split(" ")[0] for k, v in seen.items()} == {
        "notifications": "notifications.poll",
        "wars": "wars.cycle",
        "scout": "scout.run",
        "alerts": "alerts.poll",
    }
    assert describe() is None
