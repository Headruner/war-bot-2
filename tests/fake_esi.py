"""In-memory stand-in for EsiClient, using real ESI response shapes."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from app.esi import EsiError

NAMES = {
    99010015: ("BLACKFLAG.", "alliance"),
    98845884: ("islands in the stream", "corporation"),
    98000001: ("Flag Corp One", "corporation"),
    98000002: ("Flag Corp Two", "corporation"),
    587: ("Rifter", "inventory_type"),
    30002187: ("Amarr", "solar_system"),
    2112625428: ("Some Pilot", "character"),
}


def esi_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_war(war_id: int, declared: datetime, **extra) -> Dict:
    """A /wars/{id}/ body like ESI returns for a fresh alliance-vs-corp war."""
    war = {
        "id": war_id,
        "declared": esi_time(declared),
        "started": esi_time(declared + timedelta(hours=24)),
        "aggressor": {"alliance_id": 99010015, "isk_destroyed": 0, "ships_killed": 0},
        "defender": {"corporation_id": 98845884, "isk_destroyed": 0, "ships_killed": 0},
        "allies": [],
        "mutual": False,
        "open_for_allies": False,
    }
    war.update(extra)
    return war


class FakeEsi:
    def __init__(self, wars: Dict[int, Dict], killmails: Optional[Dict[int, List[Dict]]] = None):
        self.wars_db = wars
        self.killmails_db = killmails or {}
        self.war_calls: List[int] = []

    async def wars(self, max_war_id: Optional[int] = None) -> List[int]:
        return sorted(self.wars_db, reverse=True)

    async def war(self, war_id: int) -> Dict:
        self.war_calls.append(war_id)
        if war_id not in self.wars_db:
            raise EsiError(f"GET /wars/{war_id}/ -> HTTP 404", 404)
        return self.wars_db[war_id]

    async def names(self, ids: Iterable[Optional[int]]) -> Dict:
        return {int(i): NAMES[int(i)] for i in ids if i and int(i) in NAMES}

    async def alliance(self, alliance_id: int) -> Dict:
        return {"name": NAMES[alliance_id][0], "ticker": "BFLAG"}

    async def alliance_corporations(self, alliance_id: int) -> List[int]:
        return [98000001, 98000002]

    async def corporation(self, corp_id: int) -> Dict:
        return {"name": NAMES[corp_id][0], "ticker": "CORP", "member_count": 7}

    async def killmails(self, war_id: int, max_pages: int = 1) -> List[Dict]:
        return [{"killmail_id": k["killmail_id"], "killmail_hash": "h"} for k in self.killmails_db.get(war_id, [])]

    async def killmail(self, km_id: int, km_hash: str) -> Dict:
        for kms in self.killmails_db.values():
            for km in kms:
                if km["killmail_id"] == km_id:
                    return dict(km)
        raise EsiError("HTTP 404", 404)

    async def close(self) -> None:
        pass
