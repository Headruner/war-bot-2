"""War source: CCP ESI. Cheap headers decide what to keep; full War objects carry both parties."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .esi import WarEsi, WarEsiError

log = logging.getLogger("evedm.wars.source")


@dataclass
class Party:
    name: str
    type: str  # alliance | corporation
    entity_id: int | None
    members: int | None
    dotlan_url: str
    ticker: str | None
    corporations: int | None

    @property
    def logo(self) -> str | None:
        if not self.entity_id:
            return None
        kind = "alliances" if self.type == "alliance" else "corporations"
        return f"https://images.evetech.net/{kind}/{self.entity_id}/logo?size=64"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"logo": self.logo}


@dataclass
class War:
    war_id: int
    declared_at: str | None
    aggressor: Party
    defender: Party
    status: str  # none | mutual | open_for_allies
    report_url: str


@dataclass
class WarHeader:
    """The cheap part of a war (one ESI call), enough to decide whether to keep it."""

    war_id: int
    declared: datetime | None
    status: str
    ended: bool
    declared_raw: str | None = None


def _side(raw: dict) -> tuple[int | None, str]:
    if raw.get("alliance_id"):
        return raw["alliance_id"], "alliance"
    return raw.get("corporation_id"), "corporation"


def dotlan_link(kind: str, name: str) -> str:
    seg = "alliance" if kind == "alliance" else "corp"
    return f"https://evemaps.dotlan.net/{seg}/{name.replace(' ', '_')}"


def war_status(d: dict) -> str:
    return "mutual" if d.get("mutual") else "open_for_allies" if d.get("open_for_allies") else "none"


def parse_esi_time(value: str | None) -> datetime | None:
    """Parse an ESI timestamp such as 2026-10-28T21:28:00Z."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class EsiWarSource:
    def __init__(self, esi: WarEsi, alliance_member_count_max_corps: int = 20):
        self.esi = esi
        self.max_corps = alliance_member_count_max_corps

    async def newest_war_ids(self) -> list[int]:
        ids = await self.esi.wars()
        if not isinstance(ids, list):
            log.warning("unexpected /wars/ response format")
            return []
        return sorted(int(i) for i in ids)

    async def header(self, war_id: int) -> WarHeader | None:
        """Declaration time and type of a war, or None if ESI does not know it."""
        try:
            d = await self.esi.war(war_id)
        except WarEsiError as e:
            if e.not_found:
                return None
            raise
        return WarHeader(
            war_id=int(d.get("id", war_id)),
            declared=parse_esi_time(d.get("declared")),
            status=war_status(d),
            ended=bool(d.get("finished") or d.get("retracted")),
            declared_raw=d.get("declared"),
        )

    async def _party(self, raw: dict, names: dict) -> Party:
        eid, kind = _side(raw)
        name = names.get(eid, (str(eid), ""))[0] if eid else "?"
        ticker = members = corps = None
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
        except WarEsiError as e:
            log.debug("optional detail fetch failed for %s %s: %s", kind, eid, e)
        return Party(name, kind, eid, members, dotlan_link(kind, name), ticker, corps)

    async def war(self, war_id: int) -> War | None:
        """Build a War from ESI; None if ESI does not know it. Transient errors raise WarEsiError (retry later)."""
        try:
            d = await self.esi.war(war_id)
        except WarEsiError as e:
            if e.not_found:
                log.warning("war %s not found on ESI", war_id)
                return None
            raise
        a_id, _ = _side(d.get("aggressor", {}))
        d_id, _ = _side(d.get("defender", {}))
        names = await self.esi.names([a_id, d_id])
        return War(
            war_id=int(d.get("id", war_id)),
            declared_at=d.get("declared"),
            aggressor=await self._party(d.get("aggressor", {}), names),
            defender=await self._party(d.get("defender", {}), names),
            status=war_status(d),
            report_url=f"https://evemaps.dotlan.net/war/{war_id}",
        )
