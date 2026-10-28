"""Builds a war report (score, timeline, allies, kills) from ESI.

The result is a plain JSON-serialisable dict, stored in SQLite (wars.report_json)
and rendered into the Discord embed by embeds.py.
"""
from __future__ import annotations
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from .esi import EsiClient, EsiError

log = logging.getLogger(__name__)

ZKILL_KILL = "https://zkillboard.com/kill/{id}/"
ZKILL_WAR = "https://zkillboard.com/war/{id}/"

async def build_report(esi: EsiClient, war_id: int, recent_kills: int = 5,
                        kill_details_limit: int = 25, max_killmail_pages: int = 1) -> Dict:
    try:
        war = await esi.war(war_id)
    except Exception as e:
        log.warning("War report for %s unavailable: %s", war_id, e)
        return None

    a_id, a_type = _side_id(war.get("aggressor", {}))
    d_id, d_type = _side_id(war.id if not d_id else d_id) # Correction for id logic
    # Wait, the logic should be based on the war object's agg/def IDs.
    
    # Refined logic:
    agg_raw = war.get("aggressor", {})
    def_raw = war.get("defender", {})
    
    a_id, a_type = _side(agg_raw)
    d_id, d_type = _side(def_raw)
    
    allies_raw = war.get("allies", [])
    allies = [_side_id(x) for x in allies_raw]

    # Fetch killmails
    refs = await esi.killmails(war_id, max_killmail_pages) if war.get("started") else []
    refs.sort(key=lambda r: r["killmail_id"], reverse=True)
    
    details = []
    for ref in refs[:kill_details_limit]:
        try:
            km = await esi.killmail(ref["killmail_id"], ref["killmail_hash"])
            km["killmail_hash"] = ref["killmail_hash"]
            details.append(km)
        except EsiError as e:
            log.debug("killmail %s failed: %s", ref["killmail_id"], e)

    # Resolve IDs for names
    ids = {a_id, d_id, *(i for i, _ in allies)}
    names = await esi.names(ids)
    nm = lambda i: names.get(i, (str(i), ""))[0] if i else "?"

    kills = []
    for km in details[:recent_kills]:
        v = km.get("victim", {})
        victim_side = "aggressor" if a_id in (v.get("alliance_id"), v.get("corporation_id")) \
            else "defender"
        kills.append({
            "id": km["killmail_id"],
            "time": km.get("killmail_time"),
            "ship": nm(v.get("ship_type_id")),
            "victim": nm(v.get("character_id")) if v.get("character_id") else None,
            "victim_corp": nm(v.get("corporation_id")),
            "system": nm(km.get("solar_system_id")),
            "loss_of": victim_side,
            "zkill_url": ZKILL_KILL.format(id=km["killmail_id"]),
        })
    
    systems = Counter(nm(km.get("solar_system_id")) for km in details).most_common(3)

    # Summarize scores
    ag_data = agg_raw.get("ships_killed", 0)
    df_data = def_raw.get("ships_killed", 0)
    ag_isk = agg_raw.get("isk_destroyed", 0.0)
    df_isk = def_raw.get("isk_destroyed", 0.0)
    
    efficiency = 0.0
    if df_isk > 0:
        efficiency = (ag_isk / (ag_isk + df_isk)) * 100

    # Fetch Structure Summaries
    # This will be passed in from main.py or handled via a structure fetcher
    struct_summary = {}

    return {
        "war_id": war_id,
        "declared": war.get("declared"),
        "started": war.get("started"),
        "finished": war.get("finished"),
        "retracted": war.get("retracted"),
        "mutual": bool(war.get("mutual")),
        "open_for_allies": bool(war.get("open_for_allies")),
        "aggressor": {
            "id": a_id, "type": a_type, "name": nm(a_id), "ticker": "...",
            "ships_killed": ag_data, "isk_destroyed": ag_isk, "efficiency": efficiency
        },
        "defender": {
            "id": d_id, "type": d_type, "name": nm(d_id), "ticker": "...",
            "ships_killed": df_data, "isk_destroyed": df_isk, "efficiency": efficiency
        },
        "allies": [{"id": i, "type": k, "name": nm(i)} for i, k in allies],
        "kills_total": len(refs),
        "kills_total_capped": len(refs) >= 2000 * max_killmail_pages,
        "recent_kills": kills,
        "top_systems": systems,
        "zkill_war_url": ZKILL_WAR.format(id=war_id),
        "structure_summary": struct_summary,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

def _side_id(side: Dict) -> tuple[Optional[int], str]:
    if side.get("alliance_id"):
        return side["alliance_id"], "alliance"
    return side.get("corporation_id"), "corporation"
