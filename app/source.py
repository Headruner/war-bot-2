"""War source: CCP ESI (replaces the DOTLAN page scraper of 1.x).

Fetches new war IDs and builds detailed War objects containing:
- Timeline (Declared, Started, Finished, Retracted)
- Score (Ships Killed, ISK Destroyed, Efficiency)
- Allies (Names, Tickers)
- Recent Kills (Top 5-25)
- Hot Systems (Top 3)
- Structures (Publicly known structures for both sides)
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import List, Dict, Optional
from .esi import EsiClient, EsiError
from .models import Party, War

log = logging.getLogger(__name__)


def _side(raw: Dict) -> tuple[Optional[int], str]:
    if raw.get("alliance_id"):
        return raw["alliance_id"], "alliance"
    return raw.get("corporation_id"), "corporation"


def dotlan_link(kind: str, name: str) -> str:
    seg = "alliance" if kind == "alliance" else "corp"
    return f"https://evemaps.dotlan.net/{seg}/{name.replace(' ', '_')}"


def parse_esi_time(value: Optional[str]) -> Optional[datetime]:
    """Parse an ESI timestamp such as 2026-10-28T21:28:00Z (Python 3.10 safe)."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class EsiWarSource:
    def __init__(self, esi: EsiClient, alliance_member_count_max_corps: int = 20):
        self.esi = esi
        self.max_corps = alliance_member_count_max_corps

    async def newest_war_ids(self) -> List[int]:
        ids = await self.esi.wars()
        if not isinstance(ids, list):
            log.warning("Unexpected /wars/ response format")
            return []
        return sorted(int(i) for i in ids)

    async def declared(self, war_id: int) -> Optional[datetime]:
        """Declaration time of a war, or None if ESI does not know it."""
        try:
            return parse_esi_time((await self.esi.war(war_id)).get("declared"))
        except EsiError as e:
            if e.not_found:
                return None
            raise

    async def _party(self, raw: Dict, names: Dict) -> Party:
        eid, kind = _side(raw)
        name = names.get(eid, (str(eid), ""))[0] if eid else "?"
        ticker = None
        members = None
        corps = None

        try:
            if kind == "corporation" and eid:
                data = await self.esi.corporation(eid)
                ticker, members = data.get("ticker"), data.get("member_count")
                name = data.get("name") or name
            elif eid:
                data = await self.esi.alliance(eid)
                ticker, name = data.get("ticker"), data.get("name") or name
                corp_ids = await self.esi.alliance_corporations(eid) or []
                corps = len(corp_ids)
                if 0 < corps <= self.max_corps:
                    total = 0
                    for corp_id in corp_ids:
                        total += int((await self.esi.corporation(corp_id)).get("member_count") or 0)
                    members = total
        except EsiError as e:
            log.debug("Optional detail fetch failed for %s %s: %s", kind, eid, e)

        return Party(
            name=name,
            type=kind,
            entity_id=eid,
            members=members,
            dotlan_url=dotlan_link(kind, name),
            ticker=ticker,
            corporations=corps,
        )

    async def war(self, war_id: int) -> Optional[War]:
        """Build a War from ESI.

        Returns None if ESI does not know the war (404). Transient errors raise
        EsiError so the caller can retry on the next cycle instead of losing it.
        """
        try:
            d = await self.esi.war(war_id)
        except EsiError as e:
            if e.not_found:
                log.warning("War %s not found on ESI", war_id)
                return None
            raise

        a_id, _ = _side(d.get("aggressor", {}))
        d_id, _ = _side(d.get("defender", {}))
        names = await self.esi.names([a_id, d_id])

        status = ("mutual" if d.get("mutual") else
                  "open_for_allies" if d.get("open_for_allies") else "none")

        return War(
            war_id=int(d.get("id", war_id)),
            declared_at=d.get("declared"),
            aggressor=await self._party(d.get("aggressor", {}), names),
            defender=await self._party(d.get("defender", {}), names),
            status=status,
            report_url=f"https://evemaps.dotlan.net/war/{war_id}",
        )
