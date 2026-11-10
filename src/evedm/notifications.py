"""Character notifications from ESI: asset safety and structure attacks (UC-13, NTF-01..NTF-12).

ESI caches ``/characters/{id}/notifications`` for 600 s per character, so the loop runs every minute and
only asks for the characters whose cached copy has expired (the ESI client revalidates with ETag). Every
registered character with a valid token is read, whoever linked it, and the page lists them all (D17). Who
may open the page is decided by the alliance gate (``access.py``, D16).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime
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
# libyaml's safe loader parses a notification body several times faster than the pure Python one
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
PURGE_EVERY_S = 3600.0
MIN_SLEEP_S = 1.0  # the loop wakes at most this often; a character is read within a second or two of its expiry
MAX_WAITERS = 100  # open "anything new?" requests; more than this are answered at once

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
        body = yaml.load(text, Loader=_YAML_LOADER)  # noqa: S506 - a safe loader, the C one when built in
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
        notifier: Any = None,
        page_url: str | None = None,
        evescout: Any = None,
    ):
        self.notifier = notifier  # DiscordNotifier, or None when nothing is posted
        self.page_url = page_url
        self.evescout = evescout  # for the Thera / Turnur line of a Discord post
        self._post_pending = True  # look for unposted notifications on the first round
        self.s = settings
        self.db = db
        self.g = graph
        self.chars = chars
        self.esi = esi
        self.demo = demo
        self.state: dict[str, Any] = {"running": False, "polls": 0, "read": 0, "stored": 0, "last_error": None}
        self._moons: dict[int, int] = {}
        self._expires: dict[int, float | None] = {}  # character_id -> when ESI's cached copy of the list expires
        self._known: dict[int, set[int]] = {}  # character_id -> notification IDs already stored
        self._purged_at = 0.0
        # pushed to the page: the version grows whenever something new is stored, and waiters are woken at once
        self.version = 0
        self._changed = asyncio.Event()
        self._waiters = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- reading from ESI (NTF-02..NTF-05) ---------------------------------------------------------
    def pollable(self) -> dict[int, int]:
        """character_id -> owner_id of a link that may read notifications (one link per character).

        Every registered character qualifies that granted the notifications scope, whose refresh token is still
        valid (status is not Re-auth required) and that has the Notifications tick on in at least one link; the
        ranking on/off switch does not matter here (NTF-04). The link with the tick on is the one that reads.
        """
        rows = self.db.query(
            "SELECT character_id, owner_id, scopes, demo FROM characters WHERE status<>? AND notify=1 "
            "ORDER BY added_at",
            (REAUTH,),
        )
        out: dict[int, int] = {}
        for r in rows:
            if r["demo"]:
                continue
            if NOTIFICATION_SCOPE in (r["scopes"] or "") and r["character_id"] not in out:
                out[r["character_id"]] = r["owner_id"]
        return out

    def eligible(self) -> dict[int, int]:
        """The characters that are actually read: ``pollable`` with assets in at least ``min_structures`` player
        structures (NPC stations do not count). A character with only a few structures rarely gets a structure
        notification, and every one of them costs an ESI request each time ESI's cache expires."""
        pollable = self.pollable()
        need = self.s.min_structures
        if need <= 0 or not pollable:
            return pollable
        counts = self.db.structure_counts()
        return {cid: owner for cid, owner in pollable.items() if counts.get(cid, 0) >= need}

    def sources(self) -> dict[int, int]:
        """character_id -> polling owner of every character whose notifications the page lists (D17, NTF-13)."""
        out = self.pollable()
        if self.demo:  # demo characters are never polled, but their sample notifications are shown
            for r in self.db.query("SELECT character_id, owner_id FROM characters WHERE demo=1"):
                out.setdefault(r["character_id"], r["owner_id"])
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
        path = f"/characters/{character_id}/notifications"
        items = await self.esi.get(path, token)
        self._expires[character_id] = self.esi.cached_expiry(path)
        # ESI repeats the whole list (up to 500 entries) on every read: only entries not stored yet are parsed
        known = self._known.get(character_id)
        if known is None:
            known = self._known[character_id] = self.db.notification_ids(character_id)
        rows: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict) or "notification_id" not in item:
                continue
            if int(item["notification_id"]) in known:
                continue
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
        new = self.db.save_notifications(rows) if rows else 0
        known.update(r["notification_id"] for r in rows)
        self.state["read"] += len(items or [])
        self.state["stored"] += new
        return new

    def next_check_at(self, character_id: int, now: float) -> float:
        """When to ask for this character again: the moment ESI's cached copy expires (plus a second).

        A fixed 600 s after our own read drifts against ESI's cache window and a notification can then wait up to
        two windows. Bounded by ``min_interval_s`` below and ``character_interval_s`` above.
        """
        expires = self._expires.get(character_id)
        if expires is None:
            return now + self.s.character_interval_s
        return min(max(expires + 1.0, now + self.s.min_interval_s), now + self.s.character_interval_s)

    def staggered(self, character_id: int, base: float, others: list[float]) -> float:
        """``base`` moved later by up to ``stagger_s``, toward the middle between the reads before and after it.

        Positions are compared inside ESI's cache window (modulo ``character_interval_s``). Moving a read later
        moves that character's cache window with it, so round by round the reads drift apart until they are evenly
        spread; only the distances between them matter, so a common drift of all reads changes nothing.
        """
        window = self.s.character_interval_s
        if self.s.stagger_s <= 0 or not others or window <= 0:
            return base
        ahead = [(t - base) % window for t in others]  # how much later than this read each other read is
        next_gap = min((a for a in ahead if a > 0), default=window)
        prev_gap = min(window - a if a > 0 else 0.0 for a in ahead)
        shift = min((next_gap - prev_gap) / 2, self.s.stagger_s)
        return base + shift if shift >= 2.0 else base

    def _bump(self) -> None:
        """Something new was stored: wake every page that is waiting for it."""
        self.version += 1
        event, self._changed = self._changed, asyncio.Event()
        event.set()

    async def wait_changed(self, since: int, timeout: float | None = None) -> int:
        """The current version, held back until it differs from ``since`` or ``timeout`` seconds have passed."""
        if self.version != since or self._waiters >= MAX_WAITERS:
            return self.version
        event = self._changed
        self._waiters += 1
        try:
            await asyncio.wait_for(event.wait(), timeout=self.s.wait_s if timeout is None else timeout)
        except TimeoutError:
            pass
        finally:
            self._waiters -= 1
        return self.version

    # -- Discord ----------------------------------------------------------------------------------------
    @staticmethod
    def event_key(row: Any) -> str:
        """One key per event: the same notification reaches several characters with the same type, place and
        time. A notification without a place is its own event."""
        place = row["structure_id"] or row["solar_system_id"]
        if not place:
            return f"{row['type']}:c{row['character_id']}:{row['notification_id']}"
        return f"{row['type']}:{place}:{row['timestamp']}"

    @staticmethod
    def _age_s(timestamp: str) -> float:
        try:
            return time.time() - datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    def embed_payload(self, row: Any, characters: list[str]) -> dict[str, Any]:
        """The Discord message of one event: title as in game, system, nearest hub, who received it."""
        d = self.describe(row)
        system = d["system"]
        fields: list[dict[str, Any]] = []
        if system:
            link = f"https://evemaps.dotlan.net/system/{system['name'].replace(' ', '_')}"
            fields.append(
                {
                    "name": "System",
                    "value": f"[{system['name']}]({link}) ({system.get('security')}) · {system.get('region', '')}",
                    "inline": True,
                }
            )
            hub = None
            if self.evescout is not None and d["system_index"] is not None:
                with contextlib.suppress(Exception):  # the hub line is a nicety: never fail a post over it
                    hub = self.evescout.nearest(d["system_index"])
            if hub:
                jumps = hub["jumps"]
                fields.append(
                    {
                        "name": "Nearest hub",
                        "value": f"{hub['hub']} · {jumps} jump{'' if jumps == 1 else 's'} via {hub['entry']['name']}",
                        "inline": True,
                    }
                )
        who = ", ".join(characters)
        fields.append({"name": "Received by", "value": (who[:1000] + "…") if len(who) > 1000 else who or "?"})
        if self.page_url:
            fields.append({"name": "Closest characters", "value": f"[Open the Notifications page]({self.page_url})"})
        colors = {"structure_attack": 0xC0392B, "tower": 0xC0392B, "asset_safety": 0xF39C12}
        embed: dict[str, Any] = {
            "title": d["title"][:256],
            "color": colors.get(d["group"], 0x5B6B82),
            "fields": fields,
            "footer": {"text": f"{GROUP_LABELS.get(d['group'], d['group'])} · EVE Distance Meter"},
        }
        if self.page_url:
            embed["url"] = self.page_url
        if row["timestamp"]:
            embed["timestamp"] = row["timestamp"]
        payload: dict[str, Any] = {"embeds": [embed]}
        role = self.s.discord.mention_role_id
        if role and d["group"] in ("structure_attack", "tower"):
            payload["content"] = f"<@&{role}>"
            payload["allowed_mentions"] = {"roles": [str(role)]}
        return payload

    async def post_discord(self) -> int:
        """Post the notifications not posted yet; a notification several characters received is one message that
        is edited to list them all. Returns how many messages were posted or updated."""
        if self.notifier is None:
            return 0
        from .wars.discord import NotifyError, WebhookInvalid

        d = self.s.discord
        allowed = {t for g in (d.groups or self.s.groups) for t in NOTIFICATION_GROUPS.get(g, [])}
        rows = self.db.pending_notification_posts()
        names = self.db.character_names(sorted({r["character_id"] for r in rows}))
        if rows and self.evescout is not None:
            with contextlib.suppress(Exception):  # the Thera / Turnur line needs the list; a post never waits long
                await asyncio.wait_for(self.evescout.ensure_fresh_soon(), timeout=5)
        done = 0
        for r in rows:
            cid, nid = r["character_id"], r["notification_id"]
            if r["type"] not in allowed or self._age_s(r["timestamp"]) > d.max_age_min * 60:
                self.db.mark_notification_posted(cid, nid, 2)  # 2: not posted (filtered out, or history)
                continue
            key = self.event_key(r)
            name = names.get(cid, str(cid))
            post = self.db.notification_post(key)
            characters = json.loads(post["characters"]) if post is not None else []
            if name in characters:  # this character's copy is already in the message
                self.db.mark_notification_posted(cid, nid, 1)
                continue
            characters.append(name)
            payload = self.embed_payload(r, characters)
            try:
                message = None
                if post is not None and post["message_id"]:
                    message = await self.notifier.edit(post["message_id"], payload)
                if message is None:  # a new event, or the message was deleted in Discord
                    message = await self.notifier.send(payload)
            except WebhookInvalid as exc:
                self.state["last_error"] = redact(f"Discord webhook invalid: {exc}")
                log.error("notifications: %s", self.state["last_error"])
                self._post_pending = False  # nothing to gain from asking again until something new arrives
                return done
            except NotifyError as exc:
                log.warning("notification post failed (retried on the next round): %s", redact(str(exc)))
                return done
            message_id = str(message.get("id") or (post["message_id"] if post is not None else "") or "")
            self.db.save_notification_post(key, message_id or None, characters)
            self.db.mark_notification_posted(cid, nid, 1)
            done += 1
        self._post_pending = len(rows) >= 100  # a full page: more may wait
        self.state["posted"] = self.state.get("posted", 0) + done
        return done

    @staticmethod
    def _transient(exc: Exception) -> bool:
        """A failure worth retrying soon: network trouble, ESI 5xx, the error limit or a rate limit."""
        return isinstance(exc, EsiError) and (exc.status in (0, 420, 429) or exc.status >= 500)

    async def poll_once(self) -> int:
        """One round: every character whose cached copy has expired (NTF-03)."""
        pollable = self.eligible()
        due = self.db.due_notification_characters(list(pollable))
        if not due:
            return 0
        sem = asyncio.Semaphore(max(1, self.s.concurrency))
        new_total = 0
        checks = self.db.notification_checks()
        scheduled = {c: checks[c]["next_check_at"] for c in pollable if c in checks and c not in due}

        async def one(cid: int) -> int:
            async with sem:
                now = time.time()
                try:
                    new = await self.read_character(cid, pollable[cid])
                except (EsiError, SsoError) as exc:
                    wait = self.s.retry_s if self._transient(exc) else self.s.character_interval_s
                    self.db.set_notification_check(cid, now + wait, 0, repr(exc)[:200])
                    return 0
                except Exception as exc:  # an odd answer must not leave the character due on every round
                    log.warning("notifications of %s could not be read: %s", cid, redact(repr(exc)))
                    self.db.set_notification_check(cid, now + self.s.retry_s, 0, redact(repr(exc))[:200])
                    return 0
                if new:
                    self._post_pending = True
                    self._bump()  # on the page at once, while the other characters are still being read
                nxt = self.next_check_at(cid, time.time())
                others = [v for c, v in scheduled.items() if c != cid]
                scheduled[cid] = nxt = self.staggered(cid, nxt, others)
                self.db.set_notification_check(cid, nxt, new, None)
                return new

        for result in await asyncio.gather(*(one(cid) for cid in due), return_exceptions=True):
            if isinstance(result, BaseException):
                log.warning("a character's notifications could not be read: %r", result)
                continue
            new_total += result
        if time.time() - self._purged_at >= PURGE_EVERY_S:  # retention is in days: once an hour is plenty
            self._purged_at = time.time()
            if self.db.purge_notifications(self.s.retention_days):
                self._known.clear()
        return new_total

    def sleep_s(self) -> float:
        """How long the loop may sleep: until the next character is due, at most ``poll_s``, at least 1 s."""
        checks = self.db.notification_checks()
        upcoming = [checks[c]["next_check_at"] for c in self.eligible() if c in checks]
        if not upcoming:
            return self.s.poll_s
        return min(self.s.poll_s, max(MIN_SLEEP_S, min(upcoming) - time.time()))

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
                    if self.notifier is not None and self._post_pending:
                        await self.post_discord()
                except Exception as exc:  # one bad round must not stop the loop
                    self.state["last_error"] = redact(repr(exc))
                    log.exception("notification poll failed")
                self.state["polls"] += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.sleep_s())
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
        self._bump()  # let go of the pages that are waiting
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
