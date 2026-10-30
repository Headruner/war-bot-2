"""Fictional wars for demo mode, so the Wars page has content without network access."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from ..universe import GateGraph
from .source import Party, dotlan_link
from .store import WarStore

# (war_id, hours ago, type, aggressor, defender, [(system, structure, type, side)], [(hot system, kills)])
DEMO_WARS: list[tuple[int, int, str, str, str, list[tuple[str, str, str, str]], list[tuple[str, int]]]] = [
    (
        990001,
        3,
        "none",
        "Crimson Ledger Syndicate",
        "Quiet Harbor Logistics",
        [
            ("Tama", "Harbor Refinery", "Athanor", "defender"),
            ("Kedama", "Quiet Dock", "Raitaru", "defender"),
            ("Nourvukaiken", "Hangar 2", "Astrahus", "defender"),
        ],
        [("Tama", 4), ("Kedama", 1)],
    ),
    (
        990002,
        9,
        "mutual",
        "Ashen Veil",
        "Northgate Fleet Yards",
        [("Amamake", "Ash Keep", "Fortizar", "aggressor"), ("Dodixie", "Yard One", "Azbel", "defender")],
        [("Amamake", 7)],
    ),
    (
        990003,
        20,
        "open_for_allies",
        "Silent Tithe",
        "Meridian Prospectors",
        [("Hek", "Meridian Market", "Astrahus", "defender")],
        [],
    ),
]


def _party(name: str, entity_id: int) -> dict[str, Any]:
    party = Party(name, "corporation", entity_id, 40, dotlan_link("corporation", name), name[:4].upper(), None)
    return party.as_dict() | {"logo": None}  # fictional: no image server logo


def seed_demo_wars(store: WarStore, graph: GateGraph) -> int:
    """Insert the demo wars once. Returns the number inserted."""
    inserted = 0
    now = datetime.now(UTC)
    for wid, hours, war_type, agg, dfn, structures, hot in DEMO_WARS:
        if store.has(wid):
            continue
        declared = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sides: dict[str, Any] = {}
        targets: dict[int, dict[str, Any]] = {}
        for side in ("aggressor", "defender"):
            items = []
            for system, name, stype, owner in structures:
                idx = graph.find(system)
                if owner != side or idx is None:
                    continue
                d = graph.describe(idx) | {"highsec": graph.sec[idx] >= 0.45}
                items.append({"name": name, "type_id": 0, "type": stype, "owner_id": 0, "system": d})
                entry = targets.setdefault(d["id"], d | {"why": []})
                entry["why"].append(f"1 {side} structure(s)")
            systems = [i["system"] | {"structures": 1} for i in items]
            by_type: dict[str, int] = {}
            for i in items:
                by_type[i["type"]] = by_type.get(i["type"], 0) + 1
            sides[side] = {
                "total": len(items),
                "by_type": by_type,
                "corps": [],
                "corp_count": 1,
                "structures": items,
                "systems": systems,
            }
        for system, kills in hot:
            idx = graph.find(system)
            if idx is not None:
                d = graph.describe(idx)
                targets.setdefault(d["id"], d | {"why": []})["why"].append(f"{kills} recent kill(s)")
        kills_total = sum(k for _, k in hot)
        summary = {
            "war_id": wid,
            "declared": declared,
            "type": war_type,
            "report_url": f"https://evemaps.dotlan.net/war/{wid}",
            "aggressor": _party(agg, 98000000 + wid % 1000),
            "defender": _party(dfn, 98100000 + wid % 1000),
            "report": {
                "aggressor": {"ships_killed": kills_total, "isk_destroyed": kills_total * 45e6, "efficiency": 80.0},
                "defender": {"ships_killed": kills_total // 4, "isk_destroyed": kills_total * 11e6},
                "kills_total": kills_total,
                "allies": [],
                "recent_kills": [],
                "top_systems": hot,
                "zkill_war_url": None,
            },
            "structures": sides,
            "targets": list(targets.values()),
            "built_at": time.time(),
            "demo": True,
        }
        store.add_if_new(wid, "pending", declared, war_type)
        store.save_summary(wid, summary, "ready")
        inserted += 1
    return inserted


# --------------------------------------------------------------------------- census (Statistics, structures)
TYPE_IDS = {
    "Astrahus": 35832,
    "Fortizar": 35833,
    "Raitaru": 35825,
    "Azbel": 35826,
    "Athanor": 35835,
    "Tatara": 35836,
}

# extra active wars for the statistics: (war_id, hours ago, aggressor, defender, mutual, ships, isk, state)
DEMO_CENSUS: list[tuple[int, int, str, str, bool, int, float, str]] = [
    (980101, 400, "Crimson Ledger Syndicate", "Lantern Reach Mining", False, 14, 2.1e9, "fighting"),
    (980102, 300, "Crimson Ledger Syndicate", "Saltmarsh Couriers", False, 3, 0.4e9, "fighting"),
    (980103, 2000, "Ashen Veil", "Iron Psalm", True, 61, 18.5e9, "fighting"),
    (980104, 10, "Silent Tithe", "Lantern Reach Mining", False, 0, 0.0, "pending"),
    (980105, 900, "Grey Tollkeepers", "Saltmarsh Couriers", False, 5, 0.9e9, "ending"),
]

# extra high-sec structures of corporations at war: (owner, system, name, type)
DEMO_STRUCTURES: list[tuple[str, str, str, str]] = [
    ("Lantern Reach Mining", "Sobaseki", "Ore Hall", "Athanor"),
    ("Lantern Reach Mining", "Josameto", "Lantern Market", "Astrahus"),
    ("Saltmarsh Couriers", "Hek", "Courier Hub", "Raitaru"),
    ("Iron Psalm", "Rens", "Psalm Yard", "Azbel"),
    ("Crimson Ledger Syndicate", "Perimeter", "Ledger Office", "Astrahus"),
]


def _entity_ids() -> dict[str, int]:
    """Stable fictional corporation IDs (the demo wars' IDs follow seed_demo_wars)."""
    ids: dict[str, int] = {}
    for wid, *_rest in DEMO_WARS:
        agg, dfn = _rest[2], _rest[3]
        ids.setdefault(agg, 98000000 + wid % 1000)
        ids.setdefault(dfn, 98100000 + wid % 1000)
    for n, (*_r, agg, dfn, _m, _s, _i, _st) in enumerate(DEMO_CENSUS):
        ids.setdefault(agg, 98200000 + n)
        ids.setdefault(dfn, 98300000 + n)
    return ids


def seed_demo_census(db: Any, graph: GateGraph) -> dict[int, list[Any]]:
    """Active wars, names and a structure index for demo mode. Returns the structure index (owner -> structures)."""
    from .census import iso
    from .structures import Structure

    now = time.time()
    ids = _entity_ids()
    rows = []
    for wid, hours, war_type, agg, dfn, _s, hot in DEMO_WARS:
        kills = sum(k for _, k in hot)
        declared = now - hours * 3600
        rows.append(
            (
                wid,
                iso(declared),
                iso(declared + 86400),
                None,
                None,
                int(war_type == "mutual"),
                int(war_type == "open_for_allies"),
                ids[agg],
                "corporation",
                kills,
                kills * 45e6,
                ids[dfn],
                "corporation",
                kills // 4,
                kills * 11e6,
                "[]",
                now,
            )
        )
    for wid, hours, agg, dfn, mutual, ships, isk, state in DEMO_CENSUS:
        declared = now - hours * 3600
        started = declared + 86400
        finished = iso(now + 86400) if state == "ending" else None
        rows.append(
            (
                wid,
                iso(declared),
                iso(started),
                finished,
                finished,
                int(mutual),
                0,
                ids[agg],
                "corporation",
                ships - ships // 3,
                isk * 0.7,
                ids[dfn],
                "corporation",
                ships // 3,
                isk * 0.3,
                "[]",
                now,
            )
        )
    names = [(i, n, "corporation", now) for n, i in ids.items()]
    names += [(tid, name, "inventory_type", now) for name, tid in TYPE_IDS.items()]
    with db.tx() as c:
        c.executemany("INSERT OR IGNORE INTO war_census VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.executemany("INSERT OR IGNORE INTO entity_names VALUES(?,?,?,?)", names)
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('census.complete', 'true')")
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('census.examined', ?)", (str(len(rows)),))
    index: dict[int, list[Any]] = {}
    sid = 1_100_000_000_000
    structures = [
        (dfn if side == "defender" else agg, system, name, stype)
        for _w, _h, _t, agg, dfn, sts, _hot in DEMO_WARS
        for system, name, stype, side in sts
    ]
    for owner, system, name, stype in structures + DEMO_STRUCTURES:
        idx = graph.find(system)
        if idx is None:
            continue
        sid += 1
        index.setdefault(ids[owner], []).append(
            Structure(sid, f"{system} - {name}", TYPE_IDS[stype], graph.ids[idx], ids[owner])
        )
    return index
