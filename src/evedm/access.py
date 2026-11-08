"""The alliance gate for the members-only tabs, Notifications and Structures (D16, NTF-01, STR-07).

A tab is shown only to a main whose alliance is one of ``access.alliance_ids`` (default 99010320, "BB"). The
alliance comes from ESI ``/characters/{id}`` (a public endpoint), is stored on the account and is read again
at sign-in and every ``affiliation_hours``. A main of another alliance, or one whose alliance is not known
yet, sees no link and gets 404 from the page and its API.
"""

from __future__ import annotations

import logging
import time

from .config import AccessSettings
from .db import Database
from .esi import EsiClient, EsiError

log = logging.getLogger("evedm.access")


class AllianceGate:
    def __init__(self, settings: AccessSettings, db: Database, esi: EsiClient | None, *, demo: bool = False):
        self.s = settings
        self.db = db
        self.esi = esi
        self.demo = demo

    def allowed(self, user_id: int) -> bool:
        """True when this main may see the members-only tabs (cached alliance, no ESI call)."""
        if not self.s.alliance_ids or self.demo:
            return True  # no alliance configured (or demo mode): the tabs are open
        row = self.db.get_user(user_id)
        alliance = row["alliance_id"] if row is not None and "alliance_id" in row.keys() else None
        return bool(alliance) and int(alliance) in self.s.alliance_ids

    async def refresh(self, user_id: int, force: bool = False) -> int | None:
        """Read the main's corporation and alliance from ESI, at most every ``affiliation_hours``."""
        row = self.db.get_user(user_id)
        if row is None:
            return None
        known = row["alliance_id"] if "alliance_id" in row.keys() else None
        seen_at = (row["affiliation_at"] if "affiliation_at" in row.keys() else 0) or 0
        if not force and time.time() - seen_at < self.s.affiliation_hours * 3600:
            return known
        if self.esi is None:
            return known
        try:
            info = await self.esi.get(f"/characters/{user_id}")
        except EsiError as exc:
            log.info("could not read the affiliation of main %s: %s", user_id, exc)
            return known
        alliance = info.get("alliance_id")
        self.db.set_affiliation(user_id, info.get("corporation_id"), alliance)
        return int(alliance) if alliance else None
