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
import logging
import math
import time
from collections import defaultdict
from typing import Any

import httpx

from . import __version__
from .config import AlertSettings
from .crypto import redact
from .db import Database
from .esi import EsiClient, EsiError
from .universe import GateGraph

log = logging.getLogger("evedm.alerts")

STRUCTURE_CATEGORY = 65  # inventory category "Structure" (Upwell)
CORE_GROUPS = {"Citadel", "Engineering Complex", "Refinery"}  # these drop a quantum core when destroyed
RESUME_MAX_AGE_S = 20 * 3600  # R2Z2 keeps files at least 24 h: older positions restart at the pointer
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
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.db = db
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
        self.stats = {"read": 0, "gaps": 0, "blocked": 0, "errors": 0}

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
            if seq is None or time.time() - float(at_raw or 0) > RESUME_MAX_AGE_S:
                seq = await self._pointer()  # start (or restart) at the newest published position
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
    ):
        self.s = settings
        self.db = db
        self.g = graph
        self.esi = esi
        self.scout = scout
        self.structure_index = structure_index
        self.notifier = notifier
        self.page_url = page_url
        contact = f"; {settings.contact}" if settings.contact else ""
        self.feed = R2Z2Feed(
            db,
            settings.feed_url,
            f"EVE-Distance-Meter/{__version__} (structure alerts{contact})",
            max_per_poll=settings.max_per_poll,
            transport=transport,
        )
        self.state: dict[str, Any] = {"running": False, "polls": 0, "alerts": 0, "last_error": None, "last_kill": None}
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

    async def is_structure(self, type_id: int | None) -> bool:
        return (await self.type_info(type_id))[0] == STRUCTURE_CATEGORY

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
        }
        found: list[dict[str, Any]] = []
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
        new = []
        now = time.time()
        with self.db.tx() as c:
            for a in found:
                cur = c.execute(
                    "INSERT OR IGNORE INTO structure_alerts(kind, killmail_id, killmail_hash, killmail_time,"
                    " solar_system_id, structure_id, structure_type_id, structure_name, owner_id, distance_km,"
                    " victim_type_id, victim_corp_id, attackers, isk, core_drop, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        from .wars.discord import NotifyError, WebhookInvalid

        d = self.s.discord
        rows = self.db.query("SELECT * FROM structure_alerts WHERE posted=0 ORDER BY alert_id LIMIT 50")
        posted = 0
        cooldown = time.time() - d.combat_cooldown_min * 60
        for r in rows:
            skip = r["kind"] not in d.kinds
            if not skip and r["kind"] != "destroyed" and r["structure_id"]:
                recent = self.db.query(
                    "SELECT 1 FROM structure_alerts WHERE posted=1 AND kind IN ('combat','firing') AND structure_id=? "
                    "AND created_at > ? LIMIT 1",
                    (r["structure_id"], cooldown),
                )
                skip = bool(recent)
            if skip:
                self.db.execute(
                    "UPDATE structure_alerts SET posted=2 WHERE alert_id=?", (r["alert_id"],)
                )  # 2: not posted
                continue
            try:
                await self.notifier.send(self.embed_payload(r))
            except WebhookInvalid as exc:
                self.state["last_error"] = redact(f"Discord webhook invalid: {exc}")
                return posted
            except NotifyError as exc:
                log.warning("alert post failed (retried next poll): %s", exc)
                return posted
            self.db.execute("UPDATE structure_alerts SET posted=1 WHERE alert_id=?", (r["alert_id"],))
            posted += 1
        return posted

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
            "zkill": f"{ZKILL}/kill/{r['killmail_id']}/",
        }

    def embed_payload(self, r: Any) -> dict[str, Any]:
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
        if self.s.discord.mention_role_id and a["kind"] == "destroyed":
            payload["content"] = f"<@&{self.s.discord.mention_role_id}>"
            payload["allowed_mentions"] = {"roles": [str(self.s.discord.mention_role_id)]}
        return payload

    # -- reading ------------------------------------------------------------------------------------------
    def recent(self, hours: float = 24, kinds: list[str] | None = None, limit: int = 200) -> list[Any]:
        sql = "SELECT * FROM structure_alerts WHERE created_at > ?"
        params: list[Any] = [time.time() - hours * 3600]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        sql += " ORDER BY created_at DESC, alert_id DESC LIMIT ?"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def purge(self) -> int:
        return self.db.execute(
            "DELETE FROM structure_alerts WHERE created_at < ?", (time.time() - self.s.retention_days * 86400,)
        )

    # -- background ---------------------------------------------------------------------------------------
    async def poll_once(self) -> int:
        new = 0
        for pkg in await self.feed.poll():
            try:
                found = await self.handle(pkg)
            except Exception:  # one odd killmail must not stop the feed
                log.exception("could not classify a killmail")
                continue
            new += len(found)
            km = pkg.get("esi") or pkg.get("killmail") or {}
            self.state["last_kill"] = km.get("killmail_time") or self.state["last_kill"]
        await self.resolve_names()
        await self.post()
        self.purge()
        return new

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

    async def _loop(self) -> None:
        self.state["running"] = True
        try:
            while not self._stop.is_set():
                try:
                    await self.poll_once()
                    self.state["last_error"] = None
                except Exception as exc:
                    self.state["last_error"] = redact(repr(exc))
                    log.exception("alert poll failed")
                self.state["polls"] += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.s.poll_s)
                except TimeoutError:
                    pass
        finally:
            self.state["running"] = False

    def start(self) -> None:
        if self._task is None:
            log.info("structure alerts started (zKillboard R2Z2 %s)", self.s.feed_url)
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
