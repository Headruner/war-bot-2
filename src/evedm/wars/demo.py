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
