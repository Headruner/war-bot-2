"""Structure alerts from zKillboard's live killmail feed (R2Z2).

There is no public "structure under attack" event: shield, armour and hull timers only reach the owner corporation's
directors. Public killmails do show three things, and each becomes an alert:

- destroyed: the victim is an Upwell structure. Citadels, engineering complexes and refineries drop their quantum
  core in the wreck.
- combat:    a ship was killed within ``grid_km`` of a known structure (positions from ESI and EVE Ref), the best
  public hint that the structure is being shot or defended.
- firing:    a structure is among the attackers: its guns or fighters are engaged.

R2Z2 (https://r2z2.zkillboard.com/ephemeral/): ``sequence.json`` holds a recent sequence number, ``{n}.json`` one
killmail; 404 means "nothing newer yet" (wait at least 6 s), files live at least 24 h, 15 requests/s per IP, a
non-empty User-Agent is required, 403/429 means slow down.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from . import __version__
from .activity import activity
from .config import AlertSettings
from .crypto import redact
from .db import Database
from .discord_rules import Target, alert_matches, of_source, targets
from .esi import EsiClient, EsiError
from .universe import GateGraph, space_of

log = logging.getLogger("evedm.alerts")

STRUCTURE_CATEGORY = 65  # inventory category "Structure" (Upwell)
CORE_GROUPS = {"Citadel", "Engineering Complex", "Refinery"}  # these drop a quantum core when destroyed
# Hull groups that count as "big" (T2 variants have their own groups). Freighters are not capital ships in EVE.
BATTLESHIP_GROUPS = {"Battleship", "Black Ops", "Marauder"}
CAPITAL_GROUPS = {
    "Carrier",
    "Dreadnought",
    "Lancer Dreadnought",
    "Force Auxiliary",
    "Supercarrier",
    "Titan",
    "Capital Industrial Ship",  # Rorqual
}
DEFAULT_MAX_AGE_S = 3600  # a kill older than this is obsolete: no catch-up, no post (alerts.max_age_min)
BLOCKED_BACKOFF_S = 600
ZKILL = "https://zkillboard.com"


class R2Z2Feed:
    """Follows the R2Z2 sequence; the position survives restarts (meta table)."""

    def __init__(
        self,
        db: Database,
        base_url: str,
        user_agent: str,
        *,
        max_per_poll: int = 300,
        delay_s: float = 0.1,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.db = db
        self.max_age_s = max_age_s
        self.base = base_url.rstrip("/")
        self.max_per_poll = max_per_poll
        self.delay_s = delay_s
        self.blocked_until = 0.0
        self.http = httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )
        self.stats = {"read": 0, "gaps": 0, "blocked": 0, "errors": 0, "obsolete": 0}

    async def close(self) -> None:
        await self.http.aclose()

    async def _pointer(self) -> int | None:
        r = await self.http.get(f"{self.base}/sequence.json")
        if r.status_code in (403, 429):
            self._block()
            return None
        if r.status_code != 200:
            return None
        return int(r.json().get("sequence"))

    async def sync_pointer(self) -> None:
        """Move the saved position to the newest published one without reading files (the websocket is delivering).

        When the websocket drops, the fallback then starts here: at most one poll interval behind.
        """
        if time.time() < self.blocked_until:
            return
        try:
            seq = await self._pointer()
        except (httpx.HTTPError, ValueError) as exc:
            self.stats["errors"] += 1
            log.warning("zKillboard feed: %s", exc)
            return
        if seq is not None:
            self.db.set_meta("alerts.sequence", str(seq))
            self.db.set_meta("alerts.sequence_at", str(time.time()))

    def _block(self) -> None:
        self.blocked_until = time.time() + BLOCKED_BACKOFF_S
        self.stats["blocked"] += 1
        log.warning("zKillboard R2Z2 refused the request (403/429): pausing %s s", BLOCKED_BACKOFF_S)

    async def poll(self) -> list[dict[str, Any]]:
        """Killmail files published since the last call (at most ``max_per_poll``)."""
        if time.time() < self.blocked_until:
            return []
        out: list[dict[str, Any]] = []
        try:
            seq_raw, at_raw = self.db.get_meta("alerts.sequence"), self.db.get_meta("alerts.sequence_at")
            seq = int(seq_raw) if seq_raw else None
            if seq is None or time.time() - float(at_raw or 0) > self.max_age_s:
                seq = await self._pointer()  # start, or restart after a stop longer than kills matter, at the newest
                if seq is None:
                    return []
            for _ in range(self.max_per_poll):
                r = await self.http.get(f"{self.base}/{seq}.json")
                if r.status_code == 200:
                    out.append(r.json())
                    self.stats["read"] += 1
                    seq += 1
                    if self.delay_s:
                        await asyncio.sleep(self.delay_s)  # stay well under 15 requests/s
                    continue
                if r.status_code in (403, 429):
                    self._block()
                    break
                if r.status_code == 404:
                    pointer = await self._pointer()
                    if pointer is not None and pointer > seq:  # a gap below the published position: skip it
                        self.stats["gaps"] += 1
                        seq += 1
                        continue
                    break  # caught up: nothing newer yet
                self.stats["errors"] += 1
                break
            self.db.set_meta("alerts.sequence", str(seq))
            self.db.set_meta("alerts.sequence_at", str(time.time()))
        except (httpx.HTTPError, ValueError) as exc:
            self.stats["errors"] += 1
            log.warning("zKillboard feed: %s", exc)
        return out


class KillSocket:
    """zKillboard's websocket ``killstream`` channel: the main source, kills arrive as they are processed.

    Keeps itself connected (capped exponential back-off). A connection that has been silent for ``silence_s`` counts as
    down, so the R2Z2 fallback takes over. Killmails wait in a queue until the alert service drains them.
    """

    SUBSCRIBE = {"action": "sub", "channel": "killstream"}

    def __init__(
        self,
        url: str,
        user_agent: str,
        *,
        silence_s: float = 120.0,
        backoff_max_s: float = 60.0,
        connect: Any = None,
    ):
        self.url = url
        self.user_agent = user_agent
        self.silence_s = silence_s
        self.backoff_max_s = backoff_max_s
        self._connect = connect or ws_connect
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)
        self.wake = asyncio.Event()  # set on every killmail: the service processes it at once
        self.connected = False
        self.last_message = 0.0
        self.last_error: str | None = None
        self.stats = {"connects": 0, "messages": 0, "errors": 0, "dropped": 0}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def healthy(self) -> bool:
        return self.connected and time.time() - self.last_message < self.silence_s

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def drain(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                return out

    def _accept(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        self.last_message = time.time()
        if not (msg.get("killmail_id") or "victim" in msg or msg.get("esi") or msg.get("killmail")):
            return  # an acknowledgement or a status line, not a killmail
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1  # the R2Z2 position still covers it if the socket then drops
            return
        self.stats["messages"] += 1
        self.wake.set()

    async def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                async with self._connect(
                    self.url,
                    additional_headers={"User-Agent": self.user_agent},
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    await ws.send(json.dumps(self.SUBSCRIBE))
                    self.connected = True
                    self.last_message = time.time()
                    self.last_error = None
                    self.stats["connects"] += 1
                    delay = 1.0
                    log.info("zKillboard websocket connected: %s", self.url)
                    async for raw in ws:
                        self._accept(raw)
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, WebSocketException) as exc:
                self.stats["errors"] += 1
                self.last_error = redact(f"{type(exc).__name__}: {exc}")
                log.warning("zKillboard websocket: %s (R2Z2 fallback, retry in %.0f s)", self.last_error, delay)
            finally:
                self.connected = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(delay * 2, self.backoff_max_s)


def _distance_m(a: tuple[float, float, float] | None, b: tuple[float, float, float] | None) -> float | None:
    if not a or not b:
        return None
    return math.dist(a, b)


def _pos(raw: Any) -> tuple[float, float, float] | None:
    if isinstance(raw, dict) and {"x", "y", "z"} <= raw.keys():
        return float(raw["x"]), float(raw["y"]), float(raw["z"])
    return None


class AlertService:
    def __init__(
        self,
        settings: AlertSettings,
        db: Database,
        graph: GateGraph,
        esi: EsiClient,
        *,
        scout: Any = None,
        structure_index: Any = None,
        notifier: Any = None,
        page_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        socket_connect: Any = None,
    ):
        self.s = settings
        self.db = db
        self.g = graph
        self.esi = esi
        self.scout = scout
        self.structure_index = structure_index
        self.notifier = notifier
        self.rules: Any = list  # () -> discord.rules as they are now (set by the web app)
        self.page_url = page_url
        contact = f"; {settings.contact}" if settings.contact else ""
        self.feed = R2Z2Feed(
            db,
            settings.feed_url,
            f"EVE-Distance-Meter/{__version__} (structure alerts{contact})",
            max_per_poll=settings.max_per_poll,
            max_age_s=settings.max_age_min * 60,
            transport=transport,
        )
        self.socket: KillSocket | None = None
        if settings.websocket:
            self.socket = KillSocket(
                settings.websocket_url,
                f"EVE-Distance-Meter/{__version__} (structure alerts{contact})",
                connect=ws_connect if socket_connect is None else socket_connect,
            )
        self._ws_was_healthy = False
        self.state: dict[str, Any] = {
            "running": False,
            "polls": 0,
            "alerts": 0,
            "last_error": None,
            "last_kill": None,
            "source": "r2z2",
        }
        self._types: dict[int, tuple[int | None, str | None, str | None]] = {}
        self._known: tuple[Any, dict[int, list[dict[str, Any]]]] | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.version = 0

    # -- type lookups ---------------------------------------------------------------------------
    async def type_info(self, type_id: int | None) -> tuple[int | None, str | None, str | None]:
        """(category_id, group_name, type_name) from ESI, cached in the database."""
        if not type_id:
            return None, None, None
        if type_id in self._types:
            return self._types[type_id]
        row = self.db.query("SELECT category_id, group_name, name FROM type_groups WHERE type_id=?", (type_id,))
        if row:
            self._types[type_id] = (row[0][0], row[0][1], row[0][2])
            return self._types[type_id]
        try:
            t = await self.esi.get(f"/universe/types/{type_id}")
            g = await self.esi.get(f"/universe/groups/{t['group_id']}")
        except (EsiError, KeyError):
            return None, None, None  # unknown for now: asked again next time
        info = (int(g.get("category_id") or 0), str(g.get("name") or ""), str(t.get("name") or ""))
        self.db.execute(
            "INSERT OR REPLACE INTO type_groups(type_id, name, group_id, group_name, category_id) VALUES(?,?,?,?,?)",
            (type_id, info[2], t.get("group_id"), info[1], info[0]),
        )
        self._types[type_id] = info
        return info

    async def group_id_of(self, type_id: int | None) -> int | None:
        """The ESI inventory group ID of a type (cached with ``type_info``); None while ESI cannot tell."""
        if not type_id:
            return None
        await self.type_info(type_id)  # fills type_groups
        row = self.db.query("SELECT group_id FROM type_groups WHERE type_id=?", (type_id,))
        return int(row[0][0]) if row and row[0][0] is not None else None

    async def is_structure(self, type_id: int | None) -> bool:
        return (await self.type_info(type_id))[0] == STRUCTURE_CATEGORY

    async def hull_class(self, type_id: int | None) -> str:
        """"capital", "battleship" or "" (any other hull, or a type ESI could not tell us about yet)."""
        group = (await self.type_info(type_id))[1]
        if group in CAPITAL_GROUPS:
            return "capital"
        return "battleship" if group in BATTLESHIP_GROUPS else ""

    async def weigh(self, victim: dict[str, Any], attackers: list[dict[str, Any]], isk: float) -> tuple[bool, bool]:
        """(big, capitals) of a killmail.

        big: a battleship or capital hull (T2 variants included) on either side, or a loss worth at least
        ``min_value_isk``. capitals: the victim is a capital and at least one attacker flew one too.
        """
        victim_class = await self.hull_class(victim.get("ship_type_id"))
        types = {a.get("ship_type_id") for a in attackers if a.get("ship_type_id")}
        classes = {await self.hull_class(t) for t in types}
        capitals = victim_class == "capital" and "capital" in classes
        big = bool(victim_class or classes - {""}) or (self.s.min_value_isk > 0 and isk >= self.s.min_value_isk)
        return big, capitals

    # -- known structures -------------------------------------------------------------------------
    def known_in(self, system_id: int) -> list[dict[str, Any]]:
        """Known structures of a system: the scout's finds and the public dataset (positions where known)."""
        key = (
            getattr(self.scout, "version", None),
            getattr(self.structure_index, "revision", None),
            id(getattr(self.structure_index, "index", None)),
        )
        if self._known is None or self._known[0] != key:
            by_system: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
            index = getattr(self.structure_index, "index", None) or {}
            for items in index.values():
                for st in items:
                    if st.system_id:
                        by_system[st.system_id][st.structure_id] = {
                            "structure_id": st.structure_id,
                            "name": st.name,
                            "type_id": st.type_id,
                            "owner_id": st.owner_id,
                            "position": st.position,
                        }
            if self.scout is not None:
                for r in self.scout.structures():
                    by_system[r["system"]["id"]][r["structure_id"]] = {
                        "structure_id": r["structure_id"],
                        "name": r["name"],
                        "type_id": r["type_id"],
                        "owner_id": r["owner_id"],
                        "position": r["position"],
                    }
            self._known = (key, {s: list(v.values()) for s, v in by_system.items()})
        return self._known[1].get(system_id, [])

    def _match(self, system_id: int, owner_id: int | None, type_id: int | None, position: Any) -> dict[str, Any] | None:
        """The known structure behind a structure on a killmail: same system, owner and type, nearest first."""
        cands = [
            s
            for s in self.known_in(system_id)
            if s["type_id"] == type_id and (not owner_id or s["owner_id"] == owner_id)
        ]
        if not cands:
            return None
        pos = _pos(position)
        cands.sort(key=lambda s: _distance_m(pos, s["position"]) if pos and s["position"] else 1e30)
        return cands[0]

    # -- classification -------------------------------------------------------------------------------
    async def handle(self, pkg: dict[str, Any]) -> list[dict[str, Any]]:
        """Turn one R2Z2 file into alerts (stored, deduplicated). Returns the new alerts."""
        km = pkg.get("esi") or pkg.get("killmail") or (pkg if "victim" in pkg else None)
        zkb = pkg.get("zkb") or {}
        kid = pkg.get("killmail_id") or (km or {}).get("killmail_id")
        khash = pkg.get("hash") or zkb.get("hash")
        if km is None and kid and khash:
            try:
                km = await self.esi.get(f"/killmails/{kid}/{khash}")
            except EsiError as exc:
                log.debug("killmail %s not readable: %s", kid, exc)
                return []
        if not km or not km.get("solar_system_id") or self.g.index.get(km["solar_system_id"]) is None:
            return []
        kid = kid or km.get("killmail_id")
        system = int(km["solar_system_id"])
        victim = km.get("victim") or {}
        attackers = km.get("attackers") or []
        base = {
            "killmail_id": int(kid),
            "killmail_hash": khash,
            "killmail_time": km.get("killmail_time"),
            "solar_system_id": system,
            "victim_type_id": victim.get("ship_type_id"),
            "victim_corp_id": victim.get("corporation_id"),
            "attackers": len(attackers),
            "isk": float(zkb.get("totalValue") or 0),
            "capitals": 0,
        }
        found: list[dict[str, Any]] = []
        if self.s.ignore_group_ids and await self.group_id_of(victim.get("ship_type_id")) in self.s.ignore_group_ids:
            return []  # e.g. a stronghold (group 1924): no alert, no zKillboard message
        if await self.is_structure(victim.get("ship_type_id")):
            _cat, group, _name = await self.type_info(victim.get("ship_type_id"))
            st = self._match(system, victim.get("corporation_id"), victim.get("ship_type_id"), victim.get("position"))
            found.append(
                base
                | {
                    "kind": "destroyed",
                    "structure_id": st["structure_id"] if st else None,
                    "structure_name": st["name"] if st else None,
                    "structure_type_id": victim.get("ship_type_id"),
                    "owner_id": victim.get("corporation_id"),
                    "distance_km": None,
                    "core_drop": int(group in CORE_GROUPS),
                }
            )
            if st:
                self.db.execute(
                    "UPDATE known_structures SET destroyed_at=? WHERE structure_id=?", (time.time(), st["structure_id"])
                )
        else:
            pos = _pos(victim.get("position"))
            for st in self.known_in(system):
                d = _distance_m(pos, st["position"])
                if d is not None and d <= self.s.grid_km * 1000:
                    found.append(
                        base
                        | {
                            "kind": "combat",
                            "structure_id": st["structure_id"],
                            "structure_name": st["name"],
                            "structure_type_id": st["type_id"],
                            "owner_id": st["owner_id"],
                            "distance_km": round(d / 1000, 1),
                            "core_drop": 0,
                        }
                    )
        for att in attackers:
            if not await self.is_structure(att.get("ship_type_id")):
                continue
            st = self._match(system, att.get("corporation_id"), att.get("ship_type_id"), victim.get("position"))
            found.append(
                base
                | {
                    "kind": "firing",
                    "structure_id": st["structure_id"] if st else None,
                    "structure_name": st["name"] if st else None,
                    "structure_type_id": att.get("ship_type_id"),
                    "owner_id": att.get("corporation_id"),
                    "distance_km": None,
                    "core_drop": 0,
                }
            )
        if any(a["kind"] != "destroyed" for a in found):
            big, capitals = await self.weigh(victim, attackers, base["isk"])
            if self.s.big_kills_only and not big:  # a destroyed structure is always kept, whatever the kill is worth
                found = [a for a in found if a["kind"] == "destroyed"]
            found = [a | {"capitals": int(capitals)} if a["kind"] != "destroyed" else a for a in found]
        new = []
        now = time.time()
        with self.db.tx() as c:
            for a in found:
                cur = c.execute(
                    "INSERT OR IGNORE INTO structure_alerts(kind, killmail_id, killmail_hash, killmail_time,"
                    " solar_system_id, structure_id, structure_type_id, structure_name, owner_id, distance_km,"
                    " victim_type_id, victim_corp_id, attackers, isk, core_drop, created_at, capitals)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        a["kind"],
                        a["killmail_id"],
                        a["killmail_hash"],
                        a["killmail_time"],
                        a["solar_system_id"],
                        a["structure_id"],
                        a["structure_type_id"],
                        a["structure_name"],
                        a["owner_id"],
                        a["distance_km"],
                        a["victim_type_id"],
                        a["victim_corp_id"],
                        a["attackers"],
                        a["isk"],
                        a["core_drop"],
                        now,
                        a["capitals"],
                    ),
                )
                if cur.rowcount:  # 0 when the same killmail already raised this alert
                    new.append(a)
        if new:
            self.version += 1
            self.state["alerts"] += len(new)
        return new

    # -- Discord -------------------------------------------------------------------------------------
    async def post(self) -> int:
        """Post unposted alerts of the configured kinds; combat/firing at most once per structure per cooldown."""
        if self.notifier is None:
            return 0
        from .wars.discord import DiscordRejected, NotifyError

        d = self.s.discord
        rules = of_source(self.rules(), "alerts")
        rows = self.db.query("SELECT * FROM structure_alerts WHERE posted=0 ORDER BY alert_id LIMIT 50")
        alliances = self._alliances() if any(r["owners"] or r["exclude_owners"] for r in rules) else {}
        posted = 0
        cooldown = time.time() - d.combat_cooldown_min * 60
        too_old = time.time() - self.s.max_age_min * 60
        for r in rows:
            if (r["created_at"] or 0) < too_old:  # waited too long (Discord down): obsolete, kept on the page only
                self.db.execute("UPDATE structure_alerts SET posted=2 WHERE alert_id=?", (r["alert_id"],))
                continue
            before = self.db.query("SELECT channel_id FROM alert_posts WHERE alert_id=?", (r["alert_id"],))
            done = {x[0] for x in before}
            sent = bool(done)
            for target in self._targets(r, rules, alliances):
                channel = target.channel or ""
                if channel in done:  # sent before a failure further on: not again
                    continue
                if r["kind"] != "destroyed" and r["structure_id"]:  # one combat/firing post per structure and channel
                    recent = self.db.query(
                        "SELECT 1 FROM alert_posts p JOIN structure_alerts a ON a.alert_id=p.alert_id "
                        "WHERE p.channel_id=? AND a.kind IN ('combat','firing') AND a.structure_id=? "
                        "AND a.created_at > ? LIMIT 1",
                        (channel, r["structure_id"], cooldown),
                    )
                    if recent:
                        continue
                payload = self.embed_payload(r, target.role)
                try:
                    if target.channel is None:
                        message = await self.notifier.send(payload)
                    else:
                        message = await self.notifier.send(payload, channel_id=target.channel)
                except DiscordRejected as exc:
                    self.state["last_error"] = redact(f"Discord rejected the bot: {exc}")
                    if target.channel is None or exc.status not in (403, 404):
                        return posted  # the bot itself: everything waits until it is fixed
                    log.error("alert %s is not posted to channel %s: %s", r["alert_id"], channel, redact(str(exc)))
                    continue  # that channel only (no access, deleted): the other channels still get it
                except NotifyError as exc:
                    log.warning("alert post failed (retried next poll): %s", exc)
                    return posted
                self.db.execute(
                    "INSERT OR REPLACE INTO alert_posts(alert_id, channel_id, message_id, created_at) VALUES(?,?,?,?)",
                    (r["alert_id"], channel, str((message or {}).get("id") or ""), time.time()),
                )
                sent = True
            # 1: posted somewhere; 2: not posted (no rule or kind wanted it, or the cooldown)
            self.db.execute("UPDATE structure_alerts SET posted=? WHERE alert_id=?", (1 if sent else 2, r["alert_id"]))
            posted += int(sent)
        return posted

    def _alliances(self) -> dict[int, set[int]]:
        """corporation -> the alliances it is in, from the member lists the war census keeps."""
        out: dict[int, set[int]] = {}
        for alliance_id, corps in self.db.query("SELECT alliance_id, corps FROM alliance_corps"):
            for corp in json.loads(corps):
                out.setdefault(int(corp), set()).add(int(alliance_id))
        return out

    def _targets(self, r: Any, rules: list[dict[str, Any]], alliances: dict[int, set[int]]) -> list[Target]:
        """The channels of one alert: every matching rule's, or the single channel while there are no rules."""
        if not rules:
            return [Target(None, None)] if r["kind"] in self.s.discord.kinds else []
        idx = self.g.index.get(r["solar_system_id"])
        place = None if idx is None else self.g.describe(idx) | {"space": space_of(self.g, idx)}
        owner = int(r["owner_id"] or 0)
        owners = ({owner} | alliances.get(owner, set())) if owner else set()
        alert = {"kind": r["kind"], "isk": r["isk"], "core_drop": r["core_drop"], "capitals": r["capitals"]}
        return targets([rule for rule in rules if alert_matches(rule, alert, place, owners)])

    def describe(self, r: Any) -> dict[str, Any]:
        """Human view of a stored alert (names from the shared name table)."""
        ids = [i for i in (r["owner_id"], r["structure_type_id"], r["victim_type_id"], r["victim_corp_id"]) if i]
        names: dict[int, str] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            q_names = f"SELECT entity_id, name FROM entity_names WHERE entity_id IN ({marks})"  # noqa: S608
            q_types = f"SELECT type_id, name FROM type_groups WHERE type_id IN ({marks})"  # noqa: S608
            names = {x[0]: x[1] for x in self.db.query(q_names, tuple(ids))}
            for x in self.db.query(q_types, tuple(ids)):
                names.setdefault(x[0], x[1])
        idx = self.g.index.get(r["solar_system_id"])
        system = (
            self.g.describe(idx) if idx is not None else {"id": r["solar_system_id"], "name": str(r["solar_system_id"])}
        )
        stype = names.get(r["structure_type_id"], f"Type {r['structure_type_id']}")
        return {
            "alert_id": r["alert_id"],
            "kind": r["kind"],
            "time": r["killmail_time"],
            "created_at": r["created_at"],
            "system": system,
            "structure": {
                "id": r["structure_id"],
                "name": r["structure_name"] or f"unknown {stype}",
                "type": stype,
                "known": r["structure_id"] is not None,
            },
            "owner": {
                "id": r["owner_id"],
                "name": names.get(r["owner_id"], f"Corporation {r['owner_id']}"),
                "zkill": f"{ZKILL}/corporation/{r['owner_id']}/" if r["owner_id"] else None,
            },
            "victim": {
                "type": names.get(r["victim_type_id"], f"Type {r['victim_type_id']}"),
                "corp": names.get(r["victim_corp_id"], f"Corporation {r['victim_corp_id']}"),
            },
            "distance_km": r["distance_km"],
            "attackers": r["attackers"],
            "isk": r["isk"],
            "core_drop": bool(r["core_drop"]),
            "capitals": bool(r["capitals"]),
            "zkill": f"{ZKILL}/kill/{r['killmail_id']}/",
        }

    def embed_payload(self, r: Any, role: str | None = None) -> dict[str, Any]:
        """``role``: the role a rule pings with every message; None = the single channel's role, on "destroyed"."""
        a = self.describe(r)
        if a["kind"] == "destroyed":
            title = f"💥 {a['structure']['type']} destroyed" + (" - quantum core drop" if a["core_drop"] else "")
        elif a["kind"] == "combat":
            title = f"⚔ Combat {a['distance_km'] or 0:g} km from {a['structure']['name']}"
        else:
            title = f"🔫 {a['structure']['name']} is firing"
        sec = a["system"].get("security")
        fields = [
            {
                "name": "System",
                "value": f"{a['system']['name']} ({sec}) · {a['system'].get('region', '')}",
                "inline": True,
            },
            {
                "name": "Owner",
                "value": f"[{a['owner']['name']}]({a['owner']['zkill']})"
                if a["owner"]["zkill"]
                else a["owner"]["name"],
                "inline": True,
            },
            {"name": "Killmail", "value": f"[{a['victim']['type']} of {a['victim']['corp']}]({a['zkill']})"},
        ]
        if self.page_url:
            fields.append(
                {
                    "name": "Closest characters",
                    "value": f"[Open the Alerts page]({self.page_url}#alert-{a['alert_id']})",
                }
            )
        embed = {
            "title": title[:256],
            "url": a["zkill"],
            "color": 0xC0392B if a["kind"] == "destroyed" else 0xF39C12,
            "fields": fields,
            "footer": {"text": "zKillboard live feed · EVE Distance Meter"},
            "timestamp": a["time"],
        }
        payload: dict[str, Any] = {"embeds": [embed]}
        if role is None:
            role = self.s.discord.mention_role_id if a["kind"] == "destroyed" else ""
        if role:
            payload["content"] = f"<@&{role}>"
            payload["allowed_mentions"] = {"roles": [str(role)]}
        return payload

    # -- reading ------------------------------------------------------------------------------------------
    def recent(
        self, hours: float = 24, kinds: list[str] | None = None, limit: int = 200, capitals_only: bool = False
    ) -> list[Any]:
        sql = "SELECT * FROM structure_alerts WHERE created_at > ?"
        params: list[Any] = [time.time() - hours * 3600]
        if capitals_only:
            sql += " AND capitals=1"
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        sql += " ORDER BY created_at DESC, alert_id DESC LIMIT ?"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def purge(self) -> int:
        cutoff = time.time() - self.s.retention_days * 86400
        self.db.execute("DELETE FROM alert_posts WHERE created_at < ?", (cutoff,))
        return self.db.execute("DELETE FROM structure_alerts WHERE created_at < ?", (cutoff,))

    # -- background ---------------------------------------------------------------------------------------
    async def poll_once(self) -> int:
        new = 0
        for pkg in await self._packages():
            km = pkg.get("esi") or pkg.get("killmail") or pkg
            when = km.get("killmail_time") if isinstance(km, dict) else None
            if self._obsolete(when):  # still catching up after a pause: the kill no longer matters
                self.feed.stats["obsolete"] += 1
                continue
            try:
                found = await self.handle(pkg)
            except Exception:  # one odd killmail must not stop the feed
                log.exception("could not classify a killmail")
                continue
            new += len(found)
            self.state["last_kill"] = when or self.state["last_kill"]
        await self.resolve_names()
        await self.post()
        self.purge()
        return new

    async def _packages(self) -> list[dict[str, Any]]:
        """Killmails to look at now: the websocket's; R2Z2 only while the websocket is down.

        While the websocket delivers, R2Z2 is only asked for its newest position (one small request per poll), so the
        fallback can start right where the live feed stopped. The first poll after a reconnect still reads R2Z2 once,
        which covers the seconds before the subscription went live (a killmail seen twice is stored once).
        """
        sock = self.socket
        if sock is None:
            return await self.feed.poll()
        live = sock.drain()
        healthy = sock.healthy
        if healthy and self._ws_was_healthy:
            await self.feed.sync_pointer()
        else:
            live += await self.feed.poll()
        self._ws_was_healthy = healthy
        self.state["source"] = "websocket" if healthy else "r2z2"
        return live

    def _obsolete(self, killmail_time: str | None) -> bool:
        """True when the killmail happened longer ago than ``alerts.max_age_min`` (unknown time: kept)."""
        if not killmail_time:
            return False
        try:
            when = datetime.fromisoformat(killmail_time).timestamp()
        except ValueError:
            return False
        return time.time() - when > self.s.max_age_min * 60

    async def resolve_names(self) -> None:
        """Owner and victim corporation names of recent alerts (shared name table)."""
        rows = self.db.query(
            "SELECT DISTINCT owner_id FROM structure_alerts WHERE owner_id IS NOT NULL AND owner_id NOT IN "
            "(SELECT entity_id FROM entity_names) UNION SELECT DISTINCT victim_corp_id FROM structure_alerts "
            "WHERE victim_corp_id IS NOT NULL AND victim_corp_id NOT IN (SELECT entity_id FROM entity_names) LIMIT 1000"
        )
        missing = [r[0] for r in rows]
        if not missing:
            return
        try:
            found = await self.esi.names_categories(missing)
        except EsiError as exc:
            log.debug("alert names: %s", exc)
            return
        now = time.time()
        with self.db.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO entity_names(entity_id, name, category, updated_at) VALUES(?,?,?,?)",
                [(i, n, cat, now) for i, (n, cat) in found.items()],
            )

    async def _wait(self) -> None:
        """Until the next poll: ``poll_s``, or a second after a killmail came in over the websocket."""
        sock = self.socket
        if sock is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.s.poll_s)
            return
        if sock.wake.is_set():  # a killmail is already waiting: at most one pass a second under load
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
        else:
            tasks = {asyncio.ensure_future(sock.wake.wait()), asyncio.ensure_future(self._stop.wait())}
            await asyncio.wait(tasks, timeout=self.s.poll_s, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
        sock.wake.clear()

    async def _loop(self) -> None:
        self.state["running"] = True
        try:
            while not self._stop.is_set():
                try:
                    with activity("alerts.poll"):
                        await self.poll_once()
                    self.state["last_error"] = None
                except Exception as exc:
                    self.state["last_error"] = redact(repr(exc))
                    log.exception("alert poll failed")
                self.state["polls"] += 1
                await self._wait()
        finally:
            self.state["running"] = False

    def start(self) -> None:
        if self._task is None:
            log.info(
                "structure alerts started (websocket %s, R2Z2 %s)",
                self.s.websocket_url if self.socket else "off",
                self.s.feed_url,
            )
            if self.socket is not None:
                self.socket.start()
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
        if self.socket is not None:
            await self.socket.stop()
        await self.feed.close()


# --------------------------------------------------------------------------- demo
DEMO_ALERTS: list[tuple[str, str, str, str, int, int, float | None, int]] = [
    # (kind, system, structure name or "", structure type, victim type, minutes ago, distance km, core drop)
    ("destroyed", "Hek", "", "Astrahus", 35832, 12, None, 1),
    ("combat", "Perimeter", "Perimeter - Tollgate Market", "Tatara", 29990, 4, 14.2, 0),
    ("combat", "Perimeter", "Perimeter - Tollgate Market", "Tatara", 587, 9, 31.0, 0),
    ("firing", "Amamake", "Amamake - Ember Post", "Astrahus", 24702, 25, None, 0),
]
DEMO_TYPES = {
    35832: ("Astrahus", "Citadel"),
    35836: ("Tatara", "Refinery"),
    29990: ("Loki", "Strategic Cruiser"),
    587: ("Rifter", "Frigate"),
    24702: ("Hurricane", "Combat Battlecruiser"),
}


def seed_demo_alerts(db: Database, graph: GateGraph) -> int:
    """Sample alerts for demo mode (structures from the demo structure list). Returns the number inserted."""
    if db.query("SELECT 1 FROM structure_alerts LIMIT 1"):
        return 0
    now = time.time()
    with db.tx() as c:
        c.executemany(
            "INSERT OR IGNORE INTO type_groups(type_id, name, group_id, group_name, category_id) VALUES(?,?,?,?,?)",
            [(t, n, 0, g, STRUCTURE_CATEGORY if g in CORE_GROUPS else 6) for t, (n, g) in DEMO_TYPES.items()],
        )
        n = 0
        for k, (kind, system, sname, stype, victim, minutes, dist, core) in enumerate(DEMO_ALERTS):
            idx = graph.find(system)
            if idx is None:
                continue
            known = (
                c.execute(
                    "SELECT structure_id, owner_id, type_id FROM known_structures WHERE name=?", (sname,)
                ).fetchone()
                if sname
                else None
            )
            stype_id = {v[0]: t for t, v in DEMO_TYPES.items()}[stype]
            owner = known["owner_id"] if known else 98300001
            t = now - minutes * 60
            c.execute(
                "INSERT OR IGNORE INTO structure_alerts(kind, killmail_id, killmail_hash, killmail_time,"
                " solar_system_id, structure_id, structure_type_id, structure_name, owner_id, distance_km,"
                " victim_type_id, victim_corp_id, attackers, isk, core_drop, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    129000000 + k,
                    "demo",
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                    graph.ids[idx],
                    known["structure_id"] if known else None,
                    stype_id,
                    sname or None,
                    owner,
                    dist,
                    stype_id if kind == "destroyed" else victim,
                    owner if kind == "destroyed" else 98100001,
                    7 if kind == "destroyed" else 3,
                    1.6e9 if kind == "destroyed" else 2.5e8,
                    core,
                    t,
                ),
            )
            n += 1
    return n
