"""War report (score, timeline, allies, recent kills, hot systems) from ESI; a JSON-serialisable dict."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime

from .esi import WarEsi, WarEsiError

log = logging.getLogger("evedm.wars.report")

ZKILL_KILL = "https://zkillboard.com/kill/{id}/"
ZKILL_WAR = "https://zkillboard.com/war/{id}/"


def _side_id(side: dict) -> tuple[int | None, str]:
    if side.get("alliance_id"):
        return side["alliance_id"], "alliance"
    return side.get("corporation_id"), "corporation"


def is_over(war: dict) -> bool:
    return bool(war.get("finished") or war.get("retracted"))


async def build_report(
    esi: WarEsi, war_id: int, recent_kills: int = 5, kill_details_limit: int = 25, max_killmail_pages: int = 1
) -> dict | None:
    try:
        war = await esi.war(war_id)
    except WarEsiError as e:
        log.warning("war report for %s unavailable: %s", war_id, e)
        return None

    agg_raw, def_raw = war.get("aggressor", {}), war.get("defender", {})
    a_id, a_type = _side_id(agg_raw)
    d_id, d_type = _side_id(def_raw)
    allies = [_side_id(x) for x in war.get("allies", [])]

    refs: list[dict] = []
    if war.get("started"):  # ESI returns an empty list until the war has kills
        try:
            refs = await esi.killmails(war_id, max_killmail_pages)
        except WarEsiError as e:
            log.debug("killmails for war %s failed: %s", war_id, e)
    refs.sort(key=lambda r: r["killmail_id"], reverse=True)

    details = []
    for ref in refs[:kill_details_limit]:
        try:
            km = await esi.killmail(ref["killmail_id"], ref["killmail_hash"])
        except WarEsiError as e:
            log.debug("killmail %s failed: %s", ref["killmail_id"], e)
            continue
        km["killmail_hash"] = ref["killmail_hash"]
        details.append(km)

    ids = {a_id, d_id, *(i for i, _ in allies)}
    for km in details:
        v = km.get("victim", {})
        ids.update((v.get("ship_type_id"), v.get("character_id"), v.get("corporation_id"), km.get("solar_system_id")))
    try:
        names = await esi.names(ids)
    except WarEsiError as e:
        log.debug("name lookup for war %s failed: %s", war_id, e)
        names = {}

    def nm(i: int | None) -> str:
        return names.get(i, (str(i), ""))[0] if i else "?"

    kills = []
    for km in details[:recent_kills]:
        v = km.get("victim", {})
        loss_of = "aggressor" if a_id in (v.get("alliance_id"), v.get("corporation_id")) else "defender"
        kills.append(
            {
                "id": km["killmail_id"],
                "time": km.get("killmail_time"),
                "ship": nm(v.get("ship_type_id")),
                "victim": nm(v.get("character_id")) if v.get("character_id") else None,
                "victim_corp": nm(v.get("corporation_id")),
                "system": nm(km.get("solar_system_id")),
                "system_id": km.get("solar_system_id"),
                "loss_of": loss_of,
                "zkill_url": ZKILL_KILL.format(id=km["killmail_id"]),
            }
        )

    systems = Counter(nm(km.get("solar_system_id")) for km in details).most_common(3)
    ag_isk = float(agg_raw.get("isk_destroyed", 0) or 0)
    df_isk = float(def_raw.get("isk_destroyed", 0) or 0)
    total_isk = ag_isk + df_isk
    ag_eff = round(ag_isk / total_isk * 100, 1) if total_isk else None
    df_eff = round(100 - ag_eff, 1) if ag_eff is not None else None
    return {
        "war_id": war_id,
        "declared": war.get("declared"),
        "started": war.get("started"),
        "finished": war.get("finished"),
        "retracted": war.get("retracted"),
        "mutual": bool(war.get("mutual")),
        "open_for_allies": bool(war.get("open_for_allies")),
        "aggressor": {
            "id": a_id,
            "type": a_type,
            "name": nm(a_id),
            "ships_killed": agg_raw.get("ships_killed", 0),
            "isk_destroyed": ag_isk,
            "efficiency": ag_eff,
        },
        "defender": {
            "id": d_id,
            "type": d_type,
            "name": nm(d_id),
            "ships_killed": def_raw.get("ships_killed", 0),
            "isk_destroyed": df_isk,
            "efficiency": df_eff,
        },
        "allies": [{"id": i, "type": k, "name": nm(i)} for i, k in allies],
        "kills_total": len(refs),
        "kills_total_capped": len(refs) >= 2000 * max_killmail_pages,
        "recent_kills": kills,
        "top_systems": systems,
        "zkill_war_url": ZKILL_WAR.format(id=war_id),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
