"""Character notifications from ESI: asset safety and structure attacks (UC-13, NTF-01..NTF-12).

ESI caches ``/characters/{id}/notifications`` for 600 s per character. Each character is asked again the moment
its cached copy expires (a quarter of a second later; the ESI client revalidates with ETag), so a notification
waits for ESI's window and nothing else. Every registered character with a valid token is read, whoever linked
it, and the page lists them all (D17). Who may open the page is decided by the alliance gate (``access.py``,
D16). ``lag()`` explains where the time between a notification's timestamp and its delivery goes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from bisect import bisect_right
from datetime import UTC, datetime
from typing import Any

import yaml

from .activity import activity
from .characters import REAUTH, CharacterService
from .config import NOTIFICATION_GROUPS, NOTIFICATION_SCOPE, NotificationSettings
from .crypto import redact
from .db import Database
from .esi import EsiClient, EsiError
from .sso import SsoError
from .universe import GateGraph
from .zkb_check import ZkbApi, ZkbError, kill_url, listed, match_feed, parse_time
from .zkb_check import line as zkb_line

log = logging.getLogger("evedm.notifications")
# libyaml's safe loader parses a notification body several times faster than the pure Python one
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
PURGE_EVERY_S = 3600.0
MIN_SLEEP_S = 0.05  # shortest sleep of the loop (a character that is already due is read at once)
READ_MARGIN_S = 0.25  # a character is asked this long after ESI's copy expires (clock and header rounding)
MIN_REARM_S = 1.0  # the shortest honest re-arm, when ESI says the copy still lives for a moment
SHORT_REARMS = 5  # this many short re-arms in a row without anything new: fall back to ``min_interval_s``
LATE_S = 30.0  # a read later than this after its scheduled time is "late" in the lag report
HISTORY_S = 86400.0  # a notification stored more than this after its timestamp is history (a first read), not lag
HISTOGRAM_STEP_S = 120
HEADLINE_WINDOWS = 2  # the headline counts delays up to this many cache windows; older ones are backlog
MIN_SAMPLES = 20  # fewer notifications than this in the headline draw no conclusion
OLD_DATA_S = 300.0  # a read whose data was this old when ESI sent it
OLD_DATA_SHARE = 0.05
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
_MOON_KEYS = ("moonID", "moonId")
_STRUCTURE_TYPE_KEYS = ("structureTypeID", "structureTypeId", "structure_type_id")
_SHOWINFO = re.compile(r"showinfo:(\d+)//(\d+)")
TYPE_ID_GROUPS = ("tower", "other_attacks")  # ``typeID`` is the structure itself only in these (towers, orbitals...)
TYPE_NAME_RETRY_S = 3600.0  # a type ESI could not name is not asked again for this long
DISCORD_MARK = "\U0001f7e3"  # purple circle: Discord cannot colour embed text, so the key values get a marker and bold


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


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def structure_of(ntype: str, body: dict[str, Any]) -> tuple[int | None, int | None]:
    """(structure_id, structure type ID) of a notification body. The type is read where the game puts it:
    ``structureShowInfoData`` ([showinfo, typeID, structureID]), ``structureTypeID``, the ``showinfo:type//id`` of
    ``structureLink`` and, for towers and the other group of attacked orbitals, ``typeID``."""
    structure_id = _first(body, _STRUCTURE_KEYS)
    type_id: int | None = None
    info = body.get("structureShowInfoData")
    if isinstance(info, (list, tuple)) and len(info) >= 2:
        type_id = _int(info[1])
        if structure_id is None and len(info) >= 3:
            structure_id = _int(info[2])
    if type_id is None:
        type_id = _first(body, _STRUCTURE_TYPE_KEYS)
    if type_id is None or structure_id is None:
        link = _SHOWINFO.search(str(body.get("structureLink") or ""))
        if link:
            type_id = type_id if type_id is not None else int(link.group(1))
            structure_id = structure_id if structure_id is not None else int(link.group(2))
    if type_id is None and GROUP_OF.get(ntype) in TYPE_ID_GROUPS:
        type_id = _first(body, ("typeID",))
    return structure_id, type_id


def _f(row: Any, key: str) -> Any:
    """A field of a database row or a plain dict; None when the row has no such column."""
    try:
        return row[key]
    except (KeyError, IndexError):
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
        self._real_lifetime: dict[int, bool] = {}  # did ESI's answer carry a usable Expires?
        self._short_rearms: dict[int, int] = {}
        self._meta: dict[int, Any] = {}  # character_id -> ResponseMeta of the read in progress
        self._max_delay: dict[int, float | None] = {}
        self._post_lock = asyncio.Lock()
        self.zkb: ZkbApi | None = None  # zKillboard's API, attached by the app (the live feed comes from the database)
        self._zkb_task: asyncio.Task[None] | None = None
        self._zkb_wake = asyncio.Event()
        self._zkb_running = False
        self._zkb_unedited: set[str] = set()  # events whose Discord message still shows an old zKillboard line
        self._type_wanted: set[int] = set()  # structure types seen on a view whose name is not known yet
        self._type_failed: dict[int, float] = {}
        self._type_retry_at = 0.0
        self._body_types: dict[tuple[int, int], int | None] = {}
        self._post_retry_at = 0.0
        self._eligible_n: int | None = None
        self._lag_cache: tuple[float, dict[str, Any]] | None = None
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
            eligible = pollable
        else:
            counts = self.db.structure_counts()
            eligible = {cid: owner for cid, owner in pollable.items() if counts.get(cid, 0) >= need}
        if len(eligible) != self._eligible_n:
            log.info(
                "notifications: reading %d of %d registered characters (%d below min_structures=%d)",
                len(eligible), len(pollable), len(pollable) - len(eligible), need,
            )
            self._eligible_n = len(eligible)
        return eligible

    def excluded_by_gate(self) -> list[int]:
        """Registered characters that are not read because they have fewer than ``min_structures`` structures."""
        eligible = self.eligible()
        return sorted(c for c in self.pollable() if c not in eligible)

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
        items, meta = await self.esi.get_with_meta(path, token)
        self._meta[character_id] = meta
        self._expires[character_id] = self.esi.cached_expiry(path)
        if meta is not None:  # None: the cached copy was still fresh, nothing was asked
            left = meta.lifetime_s if meta.lifetime_s is not None else (meta.expires or 0.0) - meta.received_at
            self._real_lifetime[character_id] = meta.expires is not None and left >= 0
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
            ntype = str(item.get("type") or "Unknown")
            structure_id, structure_type_id = structure_of(ntype, body)
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
                    "structure_id": structure_id,
                    "structure_type_id": structure_type_id,
                }
            )
        new = self.db.save_notifications(rows) if rows else 0
        if new:
            self._start_zkb_checks(rows)
        known.update(r["notification_id"] for r in rows)
        self._max_delay[character_id] = max((self._age_s(r["timestamp"]) for r in rows), default=None)
        self.state["read"] += len(items or [])
        self.state["stored"] += new
        return new

    def next_check_at(self, character_id: int, now: float) -> float:
        """When to ask for this character again: the moment ESI's cached copy expires, plus a quarter of a second.

        A fixed 600 s after our own read drifts against ESI's cache window and a notification can then wait up to
        two windows. ESI's own lifetime is followed, also a short one (the answer was still stale: ask again when
        it says it expires). Only when ESI sends no usable Expires does ``min_interval_s`` apply, and
        ``character_interval_s`` (plus the margin) is the longest wait. Without any expiry (not known after a
        restart, or the copy was evicted) the schedule saved in the database is kept.
        """
        window = self.s.character_interval_s
        expires = self._expires.get(character_id)
        if expires is None:
            saved = self.db.notification_checks().get(character_id)
            if saved is not None and now < saved["next_check_at"] <= now + window:
                return float(saved["next_check_at"])
            return now + window
        at = expires + READ_MARGIN_S
        floor = MIN_REARM_S
        if not self._real_lifetime.get(character_id, True):
            floor = self.s.min_interval_s  # no Expires, or one that is nonsense: a prompt retry, not a hot loop
        elif at - now < self.s.min_interval_s:
            count = self._short_rearms[character_id] = self._short_rearms.get(character_id, 0) + 1
            if count > SHORT_REARMS:  # ESI keeps saying "a moment" without anything changing: slow down
                floor = self.s.min_interval_s
        else:
            self._short_rearms[character_id] = 0
        return min(max(at, now + floor), now + window + READ_MARGIN_S)

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
        """The Discord message of one event: title as in game, system, nearest hub, who received it.

        The system, structure type and kill link are marked and bold; a found kill is linked under the title."""
        d = self.describe(row)
        system = d["system"]
        fields: list[dict[str, Any]] = []
        if d["structure_type"]:  # unknown: no field (the page says "Unknown")
            fields.append(
                {"name": "Structure type", "value": f"{DISCORD_MARK} **{d['structure_type'][:900]}**", "inline": True}
            )
        if system:
            link = f"https://evemaps.dotlan.net/system/{system['name'].replace(' ', '_')}"
            fields.append(
                {
                    "name": "System",
                    "value": (
                        f"{DISCORD_MARK} **[{system['name']}]({link})** ({system.get('security')})"
                        f" · {system.get('region', '')}"
                    ),
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
        zkb = d["zkb"]
        if zkb:
            label = zkb["url"].removeprefix("https://").rstrip("/") if zkb["url"] else ""
            value = zkb["text"] + (f" — {DISCORD_MARK} **[{label}]({zkb['url']})**" if zkb["url"] else "")
            fields.append({"name": "Kill check", "value": value[:1000]})
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
        if zkb and zkb["url"]:
            embed["description"] = f"\U0001f4a5 **[Killmail on zKillboard]({zkb['url']})**"
        if row["timestamp"]:
            embed["timestamp"] = row["timestamp"]
        payload: dict[str, Any] = {"embeds": [embed]}
        role = self.s.discord.mention_role_id
        if role and d["group"] in ("structure_attack", "tower"):
            payload["content"] = f"<@&{role}>"
            payload["allowed_mentions"] = {"roles": [str(role)]}
        return payload

    async def post_discord(self, *, respect_backoff: bool = False) -> int:
        """Post the notifications not posted yet; a notification several characters received is one message that
        is edited to list them all. Returns how many messages were posted or updated. With ``respect_backoff``
        nothing is sent while the last failed post is less than ``retry_s`` ago."""
        if self.notifier is None:
            return 0
        async with self._post_lock:  # reads post as they finish: two posters must not send the same row twice
            if respect_backoff and time.time() < self._post_retry_at:
                return 0
            return await self._post_rows()

    async def _post_rows(self) -> int:
        from .wars.discord import NotifyError, WebhookInvalid

        d = self.s.discord
        self._post_pending = False  # anything stored while this runs sets it again
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
                log.warning("notification post failed (retried in %.0f s): %s", self.s.retry_s, redact(str(exc)))
                self._post_retry_at = time.time() + self.s.retry_s
                self._post_pending = True
                return done
            message_id = str(message.get("id") or (post["message_id"] if post is not None else "") or "")
            self.db.save_notification_post(key, message_id or None, characters)
            self.db.mark_notification_posted(cid, nid, 1)
            done += 1
        self._post_retry_at = 0.0
        self._post_pending = self._post_pending or len(rows) >= 100  # a full page: more may wait
        self.state["posted"] = self.state.get("posted", 0) + done
        return done

    @staticmethod
    def _transient(exc: Exception) -> bool:
        """A failure worth retrying soon: network trouble, ESI 5xx, the error limit or a rate limit."""
        return isinstance(exc, EsiError) and (exc.status in (0, 420, 429) or exc.status >= 500)

    def _retry_wait(self, exc: Exception) -> float:
        """How long a failed read waits: a refusal for a full window, a passing error ``retry_s`` and the error
        limit (420) exactly until ESI lets us in again."""
        if not self._transient(exc):
            return self.s.character_interval_s
        if isinstance(exc, EsiError) and exc.status == 420 and self.esi is not None:
            blocked = self.esi.blocked_for()
            return min(blocked + READ_MARGIN_S if blocked > 0 else MIN_REARM_S, self.s.character_interval_s)
        return self.s.retry_s

    def _record_read(self, cid: int, late_s: float, new: int, exc: Exception | None = None) -> None:
        """One row in ``notification_reads`` for the read that just ended (see ``lag``); never fails the round."""
        meta = self._meta.pop(cid, None)
        max_delay = self._max_delay.pop(cid, None)
        try:
            self.db.save_notification_read(
                cid,
                status=meta.status if meta is not None else 0,
                data_age_s=meta.data_age_s if meta is not None else None,
                lifetime_s=meta.lifetime_s if meta is not None else None,
                late_s=late_s,
                new=new,
                max_delay_s=max_delay,
                error=redact(repr(exc))[:200] if exc is not None else None,
            )
        except Exception:  # a statistics row is never worth losing a read for
            log.warning("notification read could not be recorded", exc_info=True)

    async def _post_after_read(self) -> None:
        """Discord gets a character's new notifications as soon as they are stored, not after the whole round."""
        if self.notifier is None or not self._post_pending or time.time() < self._post_retry_at:
            return
        try:
            await self.post_discord(respect_backoff=True)
        except Exception as exc:  # the next round tries again
            self.state["last_error"] = redact(repr(exc))
            log.exception("notification post failed")

    # -- structure type names ------------------------------------------------------------------------------
    async def resolve_type_names(self) -> int:
        """Name the structure types the views asked for and the database does not know: one batched ESI call for
        all of them (never one per notification), kept in ``type_names``. A failure is retried after ``retry_s``."""
        if not self._type_wanted or self.esi is None or self.demo or time.time() < self._type_retry_at:
            return 0
        ids = sorted(self._type_wanted)[:200]
        now = time.time()
        try:
            named = await self.esi.names(ids)
        except Exception as exc:
            if isinstance(exc, EsiError) and exc.status == 404:  # an ID ESI does not know: not asked for a while
                self._type_failed.update({t: now + TYPE_NAME_RETRY_S for t in ids})
                self._type_wanted.difference_update(ids)
            else:
                self._type_retry_at = now + self.s.retry_s
            log.warning("structure type names could not be read: %s", redact(repr(exc)))
            return 0
        found = {i: n for i, n in named.items() if n}
        if found:
            self.db.save_type_names(found)
            self._bump()  # the page shows the names at once
        self._type_wanted.difference_update(ids)
        self._type_failed.update({t: now + TYPE_NAME_RETRY_S for t in ids if t not in found})
        return len(found)

    # -- zKillboard check of asset safety notifications (NTF-27..NTF-33) ---------------------------------------
    def _start_zkb_checks(self, rows: list[dict[str, Any]]) -> None:
        """One check per event for the notifications just stored: the same event reaches several characters, and
        the first copy starts it. A notification that cannot be checked is recorded as skipped."""
        z = self.s.zkb_check
        if not z.enabled or not z.types:
            return
        started = False
        now = time.time()
        for r in rows:
            if r["type"] not in z.types:
                continue
            key = self.event_key(r)
            if self.db.zkb_check(key) is not None:
                continue
            type_id = self.view_context([r])["types"].get((r["character_id"], r["notification_id"]))
            event_at = parse_time(r["timestamp"])
            skip = None
            if not r.get("solar_system_id"):
                skip = "no solar system in the notification"
            elif not type_id:
                skip = "structure type unknown"
            elif event_at is None:
                skip = "no notification time"
            elif now - event_at > z.window_s + z.duration_s:
                skip = "history: older than the check window"  # the first read of a long-lived list
            self.db.create_zkb_check(
                event_key=key,
                notification_type=r["type"],
                solar_system_id=r.get("solar_system_id"),
                structure_type_id=type_id,
                event_at=event_at,
                received_at=now,
                status="skipped" if skip else "pending",
                planned=int(z.duration_s // z.interval_s) + 1,
                next_check_at=None if skip else now,
                deadline_at=now + z.duration_s,
                error=skip,
                character_id=r["character_id"],
                notification_id=r["notification_id"],
            )
            started = started or not skip
        if started:
            self._zkb_wake.set()
        self.zkb_refresh_counts()

    def zkb_report(self, since: float) -> dict[str, Any]:
        """The zKillboard checks of the period for ``lag`` and ``evedm notifications lag``: counts by outcome,
        the checks that met an API problem and the API's own counters (None when this process has no API)."""
        z = self.s.zkb_check
        counts: dict[str, Any] = self.db.zkb_counts(since)
        counts["enabled"] = z.enabled
        counts["types"] = list(z.types)
        counts["api"] = dict(self.zkb.stats) if self.zkb is not None else None
        return counts

    def zkb_refresh_counts(self) -> dict[str, Any]:
        """Counts of the last day's checks, kept in ``state`` for the page and the health output."""
        counts: dict[str, Any] = self.db.zkb_counts(time.time() - 86400)
        counts["api"] = dict(self.zkb.stats) if self.zkb is not None else None
        self.state["zkb_check"] = counts
        return counts

    def zkb_wait(self, now: float | None = None) -> float | None:
        """Seconds until the next pending check is due (0 or less: due now); None when nothing is pending."""
        now = time.time() if now is None else now
        due = [c["next_check_at"] for c in self.db.pending_zkb_checks() if c["next_check_at"] is not None]
        return min(due) - now if due else None

    async def zkb_step(self, now: float | None = None) -> int:
        """Make every check that is due; returns how many. Expired ones (the service was down past their
        deadline) close as not found."""
        z = self.s.zkb_check
        if not z.enabled:
            return 0
        done = 0
        for key in sorted(self._zkb_unedited):  # a Discord edit that failed is tried again
            await self._zkb_edit_discord(key)
        for c in self.db.pending_zkb_checks():
            at = time.time() if now is None else now
            if c["next_check_at"] is not None and c["next_check_at"] > at:
                continue
            await self._zkb_check_one(c, at)
            done += 1
        self.zkb_refresh_counts()
        return done

    async def _zkb_check_one(self, c: Any, now: float) -> None:
        z = self.s.zkb_check
        key = c["event_key"]
        if now > (c["deadline_at"] or 0) + z.interval_s:  # due long ago: the service was not running
            self.db.update_zkb_check(key, status="not_found", next_check_at=None, error=c["error"] or "expired")
            await self._zkb_closed(key)
            return
        hit: tuple[int, str, str] | None = None
        error: str | None = None
        rows = self.db.destroyed_structures(c["solar_system_id"], c["structure_type_id"])
        feed = match_feed(rows, c["event_at"], z.window_s)
        if feed is not None:
            hit = (feed["killmail_id"], feed["killmail_time"], "feed")
        elif self.zkb is not None and z.api_enabled and self.zkb.ready_in(now) <= 0:
            try:
                hit, error = await self._zkb_api(c, now)
            except ZkbError as exc:
                error = redact(str(exc))[:200]
                log.warning("zKillboard check %s: %s (the feed alone decides this round)", key, error)
        fields: dict[str, Any] = {"checks": c["checks"] + 1}
        if error:
            fields["error"] = error
        if hit is not None:
            kid, ktime, source = hit
            fields.update(status="found", next_check_at=None, killmail_id=kid, killmail_time=ktime,
                          zkb_url=kill_url(kid), source=source)
        elif now >= (c["deadline_at"] or 0):
            fields.update(status="not_found", next_check_at=None)
        else:
            fields["next_check_at"] = min(now + z.interval_s, c["deadline_at"])
        self.db.update_zkb_check(key, **fields)
        self._bump()  # the page shows the progress
        if "status" in fields:
            await self._zkb_closed(key)

    async def _zkb_api(self, c: Any, now: float) -> tuple[tuple[int, str, str] | None, str | None]:
        """Ask zKillboard's API for kills of the type in the system; a killmail whose list entry has no time is
        read from ESI (at most ``max_resolve`` per check). Returns (hit, soft error)."""
        z = self.s.zkb_check
        assert self.zkb is not None
        window_start = c["event_at"] - z.window_s
        kills = await self.zkb.kills(c["solar_system_id"], c["structure_type_id"], now - window_start + 1, now)
        best: tuple[float, tuple[int, str, str]] | None = None
        error: str | None = None
        resolved = 0
        for kill in kills[:50]:
            kid, khash, ktime, vtype, vsystem = listed(kill)
            if kid is None or (vtype is not None and vtype != c["structure_type_id"]):
                continue
            if vsystem is not None and vsystem != c["solar_system_id"]:
                continue
            if ktime is None:
                if not khash or resolved >= z.max_resolve or self.esi is None:
                    continue
                resolved += 1
                try:
                    km = await self.esi.get(f"/killmails/{kid}/{khash}")
                except EsiError as exc:
                    error = f"ESI killmail {kid}: HTTP {exc.status}"
                    continue
                ktime = parse_time((km or {}).get("killmail_time"))
                victim = (km or {}).get("victim") or {}
                if ktime is None or victim.get("ship_type_id") != c["structure_type_id"]:
                    continue
                if (km or {}).get("solar_system_id") != c["solar_system_id"]:
                    continue
            gap = abs(ktime - c["event_at"])
            if gap <= z.window_s and (best is None or gap < best[0]):
                stamp = datetime.fromtimestamp(ktime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                best = (gap, (kid, stamp, "api"))
        return (best[1] if best else None), error

    async def _zkb_closed(self, key: str) -> None:
        self._zkb_unedited.add(key)
        await self._zkb_edit_discord(key)

    async def _zkb_edit_discord(self, key: str) -> None:
        """The Discord message of the event shows the final zKillboard line: the existing message is edited, no
        message is sent and nobody is pinged again. Nothing posted (Discord off, or not yet): the post made later
        is built from the final state."""
        from .wars.discord import NotifyError, WebhookInvalid

        if self.notifier is None:
            self._zkb_unedited.discard(key)
            return
        async with self._post_lock:
            post = self.db.notification_post(key)
            check = self.db.zkb_check(key)
            if post is None or not post["message_id"] or check is None:
                self._zkb_unedited.discard(key)
                return
            row = self.db.notification_row(check["character_id"], check["notification_id"])
            if row is None:
                self._zkb_unedited.discard(key)
                return
            try:
                await self.notifier.edit(post["message_id"], self.embed_payload(row, json.loads(post["characters"])))
            except (NotifyError, WebhookInvalid) as exc:  # tried again with the next step
                log.warning("zKillboard line could not be edited into Discord: %s", redact(str(exc)))
                return
            self._zkb_unedited.discard(key)

    async def poll_once(self, post: bool = False) -> int:
        """One round: every character whose cached copy has expired (NTF-03). With ``post`` the new notifications
        of each character go to Discord the moment that character has been read."""
        pollable = self.eligible()
        due = self.db.due_notification_characters(list(pollable))
        if not due:
            return 0
        sem = asyncio.Semaphore(max(1, self.s.concurrency))
        new_total = 0
        checks = self.db.notification_checks()
        scheduled = {c: checks[c]["next_check_at"] for c in pollable if c in checks and c not in due}

        async def read_and_store(cid: int) -> int:
            async with sem:
                now = time.time()
                planned = checks[cid]["next_check_at"] if cid in checks else 0.0
                late = max(0.0, now - planned) if planned else 0.0
                try:
                    new = await self.read_character(cid, pollable[cid])
                except (EsiError, SsoError) as exc:
                    wait = self._retry_wait(exc)
                    self.db.set_notification_check(cid, now + wait, 0, redact(repr(exc))[:200])
                    self._record_read(cid, late, 0, exc)
                    return 0
                except Exception as exc:  # an odd answer must not leave the character due on every round
                    log.warning("notifications of %s could not be read: %s", cid, redact(repr(exc)))
                    self.db.set_notification_check(cid, now + self.s.retry_s, 0, redact(repr(exc))[:200])
                    self._record_read(cid, late, 0, exc)
                    return 0
                if new:
                    self._post_pending = True
                    self._bump()  # on the page at once, while the other characters are still being read
                done = time.time()
                nxt = self.next_check_at(cid, done)
                if nxt - done >= self.s.character_interval_s / 2:  # a full window: spread it; a short re-arm is exact
                    others = [v for c, v in scheduled.items() if c != cid]
                    nxt = self.staggered(cid, nxt, others)
                scheduled[cid] = nxt
                self.db.set_notification_check(cid, nxt, new, None)
                self._record_read(cid, late, new)
                return new

        async def one(cid: int) -> int:
            new = await read_and_store(cid)
            if new and post:
                await self._post_after_read()  # outside the semaphore: a slow Discord holds no read slot
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
        """How long the loop may sleep: until the next character is due (one that was never read is due now), at
        most ``poll_s`` so that new or changed characters are noticed."""
        eligible = self.eligible()
        now = time.time()
        zkb = self.zkb_wait(now) if self.s.zkb_check.enabled else None
        if zkb is not None and self._zkb_running:
            zkb = max(zkb, MIN_REARM_S)  # the check in flight is on its own task: no spinning until it is stored
        if not eligible:
            return min(self.s.poll_s, max(MIN_SLEEP_S, zkb)) if zkb is not None else self.s.poll_s
        checks = self.db.notification_checks()
        wait = min((checks[c]["next_check_at"] if c in checks else 0.0) for c in eligible) - now
        if zkb is not None:
            wait = min(wait, zkb)  # a pending zKillboard check is due then
        if self.notifier is not None and self._post_pending and self._post_retry_at > now:
            wait = min(wait, self._post_retry_at - now)  # a failed Discord post is tried again when it is due
        return min(self.s.poll_s, max(MIN_SLEEP_S, wait))

    # -- where the time goes ------------------------------------------------------------------------------
    @staticmethod
    def _stats(values: list[float]) -> dict[str, Any]:
        """n, p50, p90 and max of ``values`` (seconds, one decimal); None where there is nothing."""
        vals = sorted(v for v in values if v is not None)
        if not vals:
            return {"n": 0, "p50": None, "p90": None, "max": None}

        def pct(q: float) -> float:
            return round(vals[min(len(vals) - 1, int(q * (len(vals) - 1) + 0.5))], 1)

        return {"n": len(vals), "p50": pct(0.5), "p90": pct(0.9), "max": round(vals[-1], 1)}

    @staticmethod
    def _histogram(delays: list[float], cap_s: float) -> dict[str, int]:
        """Seconds in steps of 120 up to ``cap_s`` ("0-120" .. "1080-1200"); the headline never holds more."""
        buckets = {f"{lo}-{lo + HISTOGRAM_STEP_S}": 0 for lo in range(0, int(cap_s), HISTOGRAM_STEP_S)}
        keys = list(buckets)
        for d in delays:
            buckets[keys[min(int(d // HISTOGRAM_STEP_S), len(keys) - 1)]] += 1
        return buckets

    def lag(self, hours: float = 24.0, now: float | None = None) -> dict[str, Any]:
        """What the notifications of the last ``hours`` waited for, from ``notification_reads`` and the stored
        notifications. The delay of a notification is the time from its timestamp (EVE gives it to the minute, so
        up to a minute of it is rounding) to the moment it was stored. The headline counts only notifications matched
        to a read whose delay is at most two cache windows; older ones are ``backlog`` (a character that was not read
        for a while), ones with no read in the log ``unmatched`` and ones over a day old ``history_ignored``.
        ``reading`` names the biggest component."""
        now = time.time() if now is None else now
        since = now - hours * 3600
        reads = self.db.notification_reads(since)
        window = self.s.character_interval_s
        errors = [r for r in reads if r["error"]]
        by_status = {"200": 0, "304": 0, "no_request": 0}
        for r in reads:
            if not r["error"]:
                by_status[str(r["status"]) if r["status"] in (200, 304) else "no_request"] += 1
        data_age = self._stats([r["data_age_s"] for r in reads])
        lifetime = self._stats([r["lifetime_s"] for r in reads])
        late = self._stats([r["late_s"] for r in reads])
        late_share = sum(1 for r in reads if (r["late_s"] or 0.0) >= LATE_S) / len(reads) if reads else 0.0
        aged = [r["data_age_s"] for r in reads if r["data_age_s"] is not None]
        old = sum(1 for v in aged if v >= OLD_DATA_S)
        data_age = {**data_age, "ge_300": {"n": old, "share": round(old / len(aged), 4) if aged else None}}
        cap = HEADLINE_WINDOWS * window

        times: dict[int, list[float]] = {}
        late_of: dict[int, list[float | None]] = {}
        for r in reads:
            times.setdefault(r["character_id"], []).append(r["read_at"])
            late_of.setdefault(r["character_id"], []).append(r["late_s"])
        delays: list[float] = []
        on_time: list[float] = []
        late_reads: list[float] = []
        unmatched = history = backlog = 0
        for row in self.db.notifications_stored_since(since):
            try:
                stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            delay = max(0.0, row["created_at"] - stamp)
            if delay > HISTORY_S:
                history += 1
                continue
            ts = times.get(row["character_id"], [])
            i = bisect_right(ts, row["created_at"] + 1.0) - 1  # the read that stored it
            if i < 0:
                unmatched += 1
            elif delay > cap:
                backlog += 1
            else:
                delays.append(delay)
                (on_time if (late_of[row["character_id"]][i] or 0.0) < LATE_S else late_reads).append(delay)

        pollable = self.pollable()
        eligible = self.eligible()
        recent = {r["character_id"] for r in reads if r["read_at"] >= now - 3600 and not r["error"]}
        thresholds = {
            "errors_share": 0.1,
            "late_s": LATE_S,
            "data_age_s": 60.0,
            "data_age_300_share": OLD_DATA_SHARE,
            "window_s": window,
            "headline_max_delay_s": cap,
            "min_samples": MIN_SAMPLES,
        }
        delivery = self._stats(delays)
        if not reads:
            dominant, text = "no data", f"no reads recorded in the last {hours:g} h (is the poller running?)"
        elif len(errors) / len(reads) >= thresholds["errors_share"]:
            dominant = "errors"
            text = f"{len(errors)} of {len(reads)} reads failed: those characters were not read for a while"
        elif late_share >= 0.1 or (late["p90"] or 0.0) >= LATE_S:
            dominant = "late reads"
            text = (
                f"{late_share:.0%} of the reads started {LATE_S:.0f} s or more after their time "
                f"(p90 {late['p90']} s): blocked event loop, a slow ESI answer or the app was not running"
            )
        elif (data_age["p50"] or 0.0) >= thresholds["data_age_s"] or (
            aged and old / len(aged) >= OLD_DATA_SHARE
        ):
            dominant = "ESI data age"
            text = (
                f"ESI sends data that is already old: median {data_age['p50']} s, {old} of {len(aged)} reads "
                f"{OLD_DATA_S:.0f} s or more (its own delay before the cache)"
            )
        elif len(delays) < MIN_SAMPLES:
            dominant = "too few samples"
            text = f"too few samples: {len(delays)} notifications in the headline, {MIN_SAMPLES} needed"
        else:
            dominant = "within the cache window"
            text = (
                f"reads are on time and ESI's data is fresh: the delay is ESI's {window:.0f} s cache window "
                f"(median about {window / 2:.0f} s, worst case about {window:.0f} s)"
            )
        if reads and dominant not in ("too few samples", "no data") and len(delays) < MIN_SAMPLES:
            text += f" (delay figures: too few samples: {len(delays)})"
        return {
            "hours": hours,
            "reads": {
                "n": len(reads),
                "200": by_status["200"],
                "304": by_status["304"],
                "no_request": by_status["no_request"],
                "errors": len(errors),
            },
            "data_age_s": data_age,
            "lifetime_s": lifetime,
            "late_s": late,
            "delivery_delay_s": {
                **delivery,
                "max_delay_s": cap,
                "histogram": self._histogram(delays, cap),
                "read_on_time": {**self._stats(on_time), "histogram": self._histogram(on_time, cap)},
                "read_late": {**self._stats(late_reads), "histogram": self._histogram(late_reads, cap)},
                "backlog": backlog,
                "unmatched": unmatched,
                "history_ignored": history,
            },
            "characters": {
                "registered": len(pollable),
                "eligible": len(eligible),
                "excluded_by_gate": sorted(c for c in pollable if c not in eligible),
                "min_structures": self.s.min_structures,
                "not_read_last_hour": sorted(c for c in eligible if c not in recent),
            },
            "reading": {"dominant": dominant, "text": text, "thresholds": thresholds},
            "zkb_check": self.zkb_report(now - hours * 3600),
        }

    def lag_brief(self, hours: float = 24.0) -> dict[str, Any]:
        """The numbers of ``lag`` that belong in the health output and the page's state (cached for a minute)."""
        now = time.time()
        if self._lag_cache is not None and now - self._lag_cache[0] < 60:
            return self._lag_cache[1]
        full = self.lag(hours, now)
        brief = {
            "hours": hours,
            "reads": full["reads"]["n"],
            "errors": full["reads"]["errors"],
            "delay_p50_s": full["delivery_delay_s"]["p50"],
            "delay_p90_s": full["delivery_delay_s"]["p90"],
            "delay_samples": full["delivery_delay_s"]["n"],
            "backlog": full["delivery_delay_s"]["backlog"],
            "data_age_ge_300": full["data_age_s"]["ge_300"]["n"],
            "late_p90_s": full["late_s"]["p90"],
            "data_age_p50_s": full["data_age_s"]["p50"],
            "eligible": full["characters"]["eligible"],
            "registered": full["characters"]["registered"],
            "excluded_by_gate": len(full["characters"]["excluded_by_gate"]),
            "not_read_last_hour": len(full["characters"]["not_read_last_hour"]),
            "reading": full["reading"]["dominant"],
            "zkb_check": full["zkb_check"],
        }
        self._lag_cache = (now, brief)
        return brief

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

    def _body_type(self, row: Any) -> int | None:
        """The structure type in the stored body of a row that has none in its column (stored before the type was
        read from this body shape): derived when viewed, never written back."""
        key = (row["character_id"], row["notification_id"])
        if key not in self._body_types:
            if len(self._body_types) > 5000:
                self._body_types.clear()
            body = _f(row, "body")
            self._body_types[key] = structure_of(row["type"], parse_body(body))[1] if body else None
        return self._body_types[key]

    def view_context(self, rows: list[Any]) -> dict[str, Any]:
        """What the views of ``rows`` need beyond the rows: the structure type of each (body, then the scout's
        structure, then the public structure history), the type names the app already knows and the state of the
        zKillboard checks. Database only; a name not known yet is queued for ``resolve_type_names``."""
        types: dict[tuple[int, int], int | None] = {}
        for r in rows:
            types[(r["character_id"], r["notification_id"])] = _f(r, "structure_type_id") or self._body_type(r)
        lookup = {
            r["structure_id"]
            for r in rows
            if _f(r, "structure_id") and not types[(r["character_id"], r["notification_id"])]
        }
        by_structure = self.db.structure_types(sorted(lookup)) if lookup else {}
        for r in rows:
            key = (r["character_id"], r["notification_id"])
            if not types[key] and _f(r, "structure_id"):
                types[key] = by_structure.get(r["structure_id"])
        ids = sorted({t for t in types.values() if t})
        names = self.db.structure_type_names(ids) if ids else {}
        now = time.time()
        self._type_wanted |= {t for t in ids if t not in names and self._type_failed.get(t, 0.0) <= now}
        wanted = set(self.s.zkb_check.types) if self.s.zkb_check.enabled else set()
        keys = [self.event_key(r) for r in rows if r["type"] in wanted]
        return {"types": types, "names": names, "zkb": self.db.zkb_checks(keys) if keys else {}}

    def _zkb_view(
        self, row: Any, ctx: dict[str, Any], system: dict[str, Any] | None, type_name: str
    ) -> dict[str, Any] | None:
        check = ctx["zkb"].get(self.event_key(row)) if row["type"] in self.s.zkb_check.types else None
        if check is None:
            return None
        shown = zkb_line(check, system["name"] if system else "", type_name, self.s.zkb_check.window_s)
        if shown is None:
            return None
        shown.update(checks=check["checks"], planned=check["planned"], next_check_at=check["next_check_at"])
        return shown

    def describe(
        self, row: Any, names: dict[int, str] | None = None, ctx: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One notification as the page and Discord show it. ``ctx`` is ``view_context`` of the whole list (one
        lookup for all rows); without it the row is looked up on its own."""
        idx = self.g.index.get(row["solar_system_id"]) if row["solar_system_id"] else None
        system = self.g.describe(idx) if idx is not None else None
        ntype = row["type"]
        if ctx is None:
            ctx = self.view_context([row])
        type_id = ctx["types"].get((row["character_id"], row["notification_id"]))
        type_name = ctx["names"].get(type_id) if type_id else None
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
            "structure_type_id": type_id,
            "structure_type": type_name,
            "show_structure_type": bool(row["structure_id"] or type_id),
            "zkb": self._zkb_view(row, ctx, system, type_name or ""),
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
                    with activity("notifications.poll"):
                        await self.poll_once(post=True)
                    self.state["last_error"] = None
                    if self.notifier is not None and self._post_pending and time.time() >= self._post_retry_at:
                        await self.post_discord()
                except Exception as exc:  # one bad round must not stop the loop
                    self.state["last_error"] = redact(repr(exc))
                    log.exception("notification poll failed")
                self.state["polls"] += 1
                await self.resolve_type_names_soft()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.sleep_s())
                except TimeoutError:
                    pass
        finally:
            self.state["running"] = False

    async def resolve_type_names_soft(self) -> None:
        try:
            await self.resolve_type_names()
        except Exception:  # names are a nicety
            log.warning("structure type names could not be resolved", exc_info=True)

    async def _zkb_loop(self) -> None:
        """The zKillboard checks run on their own task, so a slow API never holds up a read of ESI."""
        self.zkb_refresh_counts()
        while not self._stop.is_set():
            self._zkb_running = True
            try:
                with activity("notifications.zkb_check"):
                    await self.zkb_step()
            except Exception as exc:  # one bad round must not stop the checks
                self.state["last_error"] = redact(repr(exc))
                log.exception("zKillboard check failed")
            finally:
                self._zkb_running = False
            wait = self.zkb_wait()
            self._zkb_wake.clear()
            timeout = self.s.poll_s if wait is None else min(self.s.poll_s, max(MIN_SLEEP_S, wait))
            try:
                await asyncio.wait_for(self._zkb_wake.wait(), timeout=timeout)
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._zkb_task is None and self.s.zkb_check.enabled:
            self._zkb_task = asyncio.ensure_future(self._zkb_loop())
        if self._task is None:
            log.info("notifications started (every %.0f s, ESI caches each character %.0f s)",
                     self.s.poll_s, self.s.character_interval_s)
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        self._zkb_wake.set()
        if self._zkb_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._zkb_task), timeout=10)
            except TimeoutError:
                self._zkb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._zkb_task
            self._zkb_task = None
        self._bump()  # let go of the pages that are waiting
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
