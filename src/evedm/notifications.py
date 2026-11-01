"""Character notifications from ESI: asset safety and structure attacks (UC-13, NTF-01..NTF-12).

ESI caches ``/characters/{id}/notifications`` for 600 s per character, so the loop runs every minute and
only asks for the characters whose cached copy has expired (the ESI client revalidates with ETag). The
page is shown only to a main whose alliance is one of ``notifications.alliance_ids`` (D16).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any

import yaml

from .characters import REAUTH, CharacterService
from .config import NOTIFICATION_GROUPS, NOTIFICATION_SCOPE, NotificationSettings
from .crypto import redact
from .db import Database
from .esi import EsiClient, EsiError
from .sso import SsoError
from .universe import GateGraph

log = logging.getLogger("evedm.notifications")

GROUP_OF = {t: group for group, types in NOTIFICATION_GROUPS.items() for t in types}
GROUP_LABELS = {
    "asset_safety": "Asset safety",
    "structure_attack": "Structure attacked",
    "structure_state": "Structure state and fuel",
    "tower": "Starbases (POS)",
    "other_attacks": "Customs offices, skyhooks, dens",
    "sovereignty": "Sovereignty",
    "other": "Other notifications",
}
# One line per type, as the in-game notification reads. {system} is filled in; unknown types fall back
# to the type name split into words.
TITLES = {
    "StructureItemsMovedToSafety": "Your assets in {system} have been moved to Asset Safety!",
    "StructureItemsDelivered": "Your items in {system} have been delivered",
    "StructureImpendingAbandonmentAssetsAtRisk": (
        "Your assets in {system} are at risk: the structure is going abandoned"
    ),
    "StructureUnderAttack": "A structure in {system} is under attack!",
    "StructureLostShields": "A structure in {system} has lost its shields",
    "StructureLostArmor": "A structure in {system} has lost its armor",
    "StructureDestroyed": "A structure in {system} has been destroyed",
    "StructuresReinforcementChanged": "The reinforcement window of structures in {system} changed",
    "OwnershipTransferred": "A structure in {system} changed owner",
    "StructureFuelAlert": "A structure in {system} is running out of fuel",
    "StructureLowReagentsAlert": "A structure in {system} is running out of reagents",
    "StructureNoReagentsAlert": "A structure in {system} has run out of reagents",
    "StructureWentLowPower": "A structure in {system} went to low power",
    "StructureWentHighPower": "A structure in {system} went to high power",
    "StructureServicesOffline": "Services of a structure in {system} went offline",
    "StructureAnchoring": "A structure is anchoring in {system}",
    "StructureUnanchoring": "A structure in {system} is unanchoring",
    "StructureOnline": "A structure in {system} came online",
    "TowerAlertMsg": "A starbase in {system} is under attack!",
    "TowerResourceAlertMsg": "A starbase in {system} is running out of fuel",
    "OrbitalAttacked": "A customs office in {system} is under attack!",
    "OrbitalReinforced": "A customs office in {system} has been reinforced",
    "SkyhookUnderAttack": "A skyhook in {system} is under attack!",
    "SkyhookLostShields": "A skyhook in {system} has lost its shields",
    "SkyhookDestroyed": "A skyhook in {system} has been destroyed",
    "MercenaryDenAttacked": "A mercenary den in {system} is under attack!",
    "MercenaryDenReinforced": "A mercenary den in {system} has been reinforced",
}
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")
_SYSTEM_KEYS = ("solarsystemID", "solarSystemID", "solarsystemid", "systemID", "solar_system_id")
_STRUCTURE_KEYS = ("structureID", "structureId", "structure_id")
_TYPE_KEYS = ("structureTypeID", "structureTypeId", "typeID", "structure_type_id")
_MOON_KEYS = ("moonID", "moonId")


def _first(body: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = body.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
    return None


def parse_body(text: str) -> dict[str, Any]:
    """The YAML text ESI sends with a notification; an unreadable body is simply empty."""
    if not text:
        return {}
    try:
        body = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return body if isinstance(body, dict) else {}


def title_of(ntype: str, system_name: str) -> str:
    template = TITLES.get(ntype)
    if template:
        return template.format(system=system_name or "an unknown system")
    words = _CAMEL.sub(" ", ntype).replace("Msg", "").strip()
    return f"{words} in {system_name}" if system_name else words


class NotificationService:
    """Reads, stores and serves the notifications of the registered characters."""

    def __init__(
        self,
        settings: NotificationSettings,
        db: Database,
        graph: GateGraph,
        chars: CharacterService,
        esi: EsiClient | None,
        *,
        demo: bool = False,
    ):
        self.s = settings
        self.db = db
        self.g = graph
        self.chars = chars
        self.esi = esi
        self.demo = demo
        self.state: dict[str, Any] = {"running": False, "polls": 0, "read": 0, "stored": 0, "last_error": None}
        self._moons: dict[int, int] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- the alliance gate (D16, NTF-01) ----------------------------------------------------------
    def allowed(self, user_id: int) -> bool:
        """True when this main may see the section: its alliance is one of the configured ones."""
        if not self.s.enabled:
            return False
        if not self.s.alliance_ids or self.demo:
            return True  # no alliance configured (or demo mode): the section is open
        row = self.db.get_user(user_id)
        alliance = row["alliance_id"] if row is not None and "alliance_id" in row.keys() else None
        return bool(alliance) and int(alliance) in self.s.alliance_ids

    async def refresh_affiliation(self, user_id: int, force: bool = False) -> int | None:
        """Read the main's corporation and alliance from ESI (public endpoint), at most every few hours."""
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

    # -- reading from ESI (NTF-02..NTF-05) ---------------------------------------------------------
    def pollable(self) -> dict[int, int]:
        """character_id -> owner_id of a link that may read notifications (one link per character)."""
        rows = self.db.query(
            "SELECT character_id, owner_id, scopes, demo FROM characters WHERE enabled=1 AND status<>?", (REAUTH,)
        )
        out: dict[int, int] = {}
        for r in rows:
            if r["demo"]:
                continue
            if NOTIFICATION_SCOPE in (r["scopes"] or "") and r["character_id"] not in out:
                out[r["character_id"]] = r["owner_id"]
        return out

    async def _resolve_moon(self, moon_id: int) -> int | None:
        if moon_id in self._moons:
            return self._moons[moon_id]
        if self.esi is None:
            return None
        try:
            moon = await self.esi.get(f"/universe/moons/{moon_id}")
        except EsiError:
            return None
        system = moon.get("system_id")
        if system:
            self._moons[moon_id] = int(system)
        return int(system) if system else None

    async def read_character(self, character_id: int, owner_id: int) -> int:
        """Read one character's notifications and store the new ones. Returns how many were new."""
        if self.esi is None:
            return 0
        token = await self.chars.access_token(character_id, owner_id)
        items = await self.esi.get(f"/characters/{character_id}/notifications", token)
        rows: list[dict[str, Any]] = []
        for item in items or []:
            body = parse_body(str(item.get("text") or ""))
            system_id = _first(body, _SYSTEM_KEYS)
            if system_id is None:
                moon_id = _first(body, _MOON_KEYS)
                if moon_id:
                    system_id = await self._resolve_moon(moon_id)
            rows.append(
                {
                    "character_id": character_id,
                    "notification_id": int(item["notification_id"]),
                    "type": str(item.get("type") or "Unknown"),
                    "sender_id": item.get("sender_id"),
                    "sender_type": item.get("sender_type"),
                    "timestamp": str(item.get("timestamp") or ""),
                    "body": str(item.get("text") or "")[:4000],
                    "solar_system_id": system_id,
                    "structure_id": _first(body, _STRUCTURE_KEYS),
                    "structure_type_id": _first(body, _TYPE_KEYS),
                }
            )
        new = self.db.save_notifications(rows)
        self.state["read"] += len(rows)
        self.state["stored"] += new
        return new

    async def poll_once(self) -> int:
        """One round: every character whose cached copy has expired (NTF-03)."""
        pollable = self.pollable()
        due = self.db.due_notification_characters(list(pollable))
        if not due:
            return 0
        sem = asyncio.Semaphore(max(1, self.s.concurrency))
        new_total = 0

        async def one(cid: int) -> int:
            async with sem:
                try:
                    new = await self.read_character(cid, pollable[cid])
                except (EsiError, SsoError) as exc:
                    self.db.set_notification_check(cid, time.time() + self.s.character_interval_s, 0, repr(exc)[:200])
                    return 0
                self.db.set_notification_check(cid, time.time() + self.s.character_interval_s, new, None)
                return new

        for result in await asyncio.gather(*(one(cid) for cid in due), return_exceptions=True):
            if isinstance(result, BaseException):
                log.warning("a character's notifications could not be read: %r", result)
                continue
            new_total += result
        self.db.purge_notifications(self.s.retention_days)
        return new_total

    # -- views (NTF-06..NTF-09) ----------------------------------------------------------------------
    def default_types(self) -> list[str]:
        return self.s.default_types()

    def catalogue(self, seen: dict[str, int]) -> list[dict[str, Any]]:
        """The filter list: the known groups plus every type ESI has actually sent (NTF-08)."""
        defaults = set(self.default_types())
        groups: list[dict[str, Any]] = []
        for name, types in NOTIFICATION_GROUPS.items():
            groups.append(
                {
                    "group": name,
                    "label": GROUP_LABELS.get(name, name),
                    "default": name in self.s.groups,
                    "types": [
                        {"type": t, "seen": seen.get(t, 0), "default": t in defaults}
                        for t in types
                    ],
                }
            )
        extra = sorted(t for t in seen if t not in GROUP_OF)
        if extra:
            groups.append(
                {
                    "group": "other",
                    "label": GROUP_LABELS["other"],
                    "default": False,
                    "types": [{"type": t, "seen": seen[t], "default": False} for t in extra],
                }
            )
        return groups

    def describe(self, row: Any, names: dict[int, str] | None = None) -> dict[str, Any]:
        idx = self.g.index.get(row["solar_system_id"]) if row["solar_system_id"] else None
        system = self.g.describe(idx) if idx is not None else None
        ntype = row["type"]
        return {
            "notification_id": row["notification_id"],
            "character_id": row["character_id"],
            "character": (names or {}).get(row["character_id"], str(row["character_id"])),
            "type": ntype,
            "group": GROUP_OF.get(ntype, "other"),
            "title": title_of(ntype, system["name"] if system else ""),
            "timestamp": row["timestamp"],
            "system": system,
            "system_index": idx,
            "structure_id": row["structure_id"],
            "structure_type_id": row["structure_type_id"],
            "body": row["body"],
        }

    def recent(
        self, character_ids: list[int], types: list[str] | None, hours: float = 720, limit: int = 200
    ) -> list[Any]:
        return self.db.notifications(character_ids, types, hours, limit)

    # -- background ------------------------------------------------------------------------------------
    async def _loop(self) -> None:
        self.state["running"] = True
        try:
            while not self._stop.is_set():
                try:
                    await self.poll_once()
                    self.state["last_error"] = None
                except Exception as exc:  # one bad round must not stop the loop
                    self.state["last_error"] = redact(repr(exc))
                    log.exception("notification poll failed")
                self.state["polls"] += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.s.poll_s)
                except TimeoutError:
                    pass
        finally:
            self.state["running"] = False

    def start(self) -> None:
        if self._task is None:
            log.info("notifications started (every %.0f s, ESI caches each character %.0f s)",
                     self.s.poll_s, self.s.character_interval_s)
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
