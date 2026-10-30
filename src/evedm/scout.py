"""Structure scout: player-owned structures found through the assets of every registered character.

For each character that granted ``esi-assets.read_assets.v1`` the scout reads the asset list (locations only are
kept) and takes every location that is not one of the character's own items as a structure candidate. Candidates are
read from ``/universe/structures/{id}`` with ``esi-universe.read_structures.v1``, using the token of a character
that has assets there (403 for one character: the next one is tried). NPC stations never appear: only Upwell
structures answer that endpoint. The result is shared by every main on the site (the Structures page) and also feeds
the structure lists of the Wars page.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import Counter
from typing import Any

from .characters import REAUTH, CharacterService
from .config import STRUCTURE_SCOPES, Settings
from .crypto import redact
from .db import Database
from .esi import EsiClient, EsiError
from .sso import SsoError
from .universe import GateGraph, security_class

log = logging.getLogger("evedm.scout")

READ_STRUCTURES, READ_ASSETS = STRUCTURE_SCOPES
WSPACE_MIN_ID = 31000000
ZKILL = "https://zkillboard.com"


def space_of(graph: GateGraph, idx: int) -> str:
    """high | low | null | wormhole (J-space and other systems without stargates)."""
    if graph.ids[idx] >= WSPACE_MIN_ID:
        return "wormhole"
    return security_class(graph.sec[idx])


class StructureScout:
    def __init__(self, settings: Settings, db: Database, graph: GateGraph, chars: CharacterService, esi: EsiClient):
        self.settings = settings
        self.s = settings.structures
        self.db = db
        self.g = graph
        self.chars = chars
        self.esi = esi
        self.version = 0  # bumped when known structures change (caches, the Wars page index)
        self.on_change: list[Any] = []  # callbacks(index) when the structure set changes
        self.state: dict[str, Any] = {"running": False, "cycles": 0, "last_run": None, "last_error": None}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._rows: tuple[int, list[dict[str, Any]]] | None = None

    # -- which characters can contribute ---------------------------------------------------------
    def contributors(self) -> list[tuple[int, int, list[str]]]:
        """(character_id, owner_id, scopes) per EVE character with the asset scope, one link per character."""
        best: dict[int, tuple[int, int, list[str]]] = {}
        rows = self.db.query(
            "SELECT c.owner_id, c.character_id, c.scopes, c.status, c.demo, t.updated_at FROM characters c "
            "JOIN tokens t ON t.owner_id=c.owner_id AND t.character_id=c.character_id ORDER BY t.updated_at"
        )
        for r in rows:
            scopes = json.loads(r["scopes"])
            if r["status"] == REAUTH or r["demo"] or READ_ASSETS not in scopes:
                continue
            best[r["character_id"]] = (r["character_id"], r["owner_id"], scopes)  # newest token wins
        return list(best.values())

    # -- one scout step -----------------------------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        t0 = time.time()
        stats = {"characters_read": 0, "candidates": 0, "resolved": 0, "no_access": 0, "gone": 0, "errors": 0}
        contributors = self.contributors()
        linked = {c for c, _, _ in contributors}
        checked = {r[0]: r[1] for r in self.db.query("SELECT character_id, checked_at FROM scout_characters")}
        due = [c for c in contributors if time.time() - checked.get(c[0], 0) > self.s.assets_refresh_hours * 3600]
        sem = asyncio.Semaphore(self.s.concurrency)

        async def read_assets(item: tuple[int, int, list[str]]) -> None:
            cid, owner, _ = item
            async with sem:
                try:
                    token = await self.chars.access_token(cid, owner)
                    assets, truncated = await self.esi.get_paged(
                        f"/characters/{cid}/assets", token, max_pages=self.s.max_asset_pages
                    )
                except (EsiError, SsoError) as exc:
                    stats["errors"] += 1
                    self.db.execute(
                        "INSERT INTO scout_characters(character_id, checked_at, error) VALUES(?,?,?) "
                        "ON CONFLICT(character_id) DO UPDATE SET checked_at=excluded.checked_at, error=excluded.error",
                        (cid, time.time(), redact(str(exc))[:200]),
                    )
                    return
            own = {int(a["item_id"]) for a in assets if a.get("item_id")}
            found = {
                int(a["location_id"])
                for a in assets
                if a.get("location_type") in ("item", "other")
                and int(a.get("location_id") or 0) not in own
                and int(a.get("location_id") or 0) >= 1_000_000_000_000  # structure IDs; NPC stations are 60 million
            }
            now = time.time()
            with self.db.tx() as c:
                c.execute("DELETE FROM structure_sightings WHERE character_id=?", (cid,))
                c.executemany(
                    "INSERT INTO structure_sightings(structure_id, character_id, seen_at) VALUES(?,?,?)",
                    [(sid, cid, now) for sid in found],
                )
                c.executemany(
                    "INSERT INTO known_structures(structure_id, first_seen, last_seen) VALUES(?,?,?) "
                    "ON CONFLICT(structure_id) DO UPDATE SET last_seen=excluded.last_seen",
                    [(sid, now, now) for sid in found],
                )
                c.execute(
                    "INSERT INTO scout_characters(character_id, checked_at, structures, error) VALUES(?,?,?,?) "
                    "ON CONFLICT(character_id) DO UPDATE SET checked_at=excluded.checked_at, "
                    "structures=excluded.structures, error=excluded.error",
                    (cid, now, len(found), "assets truncated" if truncated else None),
                )
            stats["characters_read"] += 1
            stats["candidates"] += len(found)

        await asyncio.gather(*(read_assets(c) for c in due))
        # characters no longer linked by anyone: their sightings no longer count
        if linked:
            marks = ",".join("?" * len(linked))
            self.db.execute(
                f"DELETE FROM structure_sightings WHERE character_id NOT IN ({marks})",  # noqa: S608
                tuple(linked),
            )
        else:
            self.db.execute("DELETE FROM structure_sightings")
        await self._resolve(contributors, stats)
        await self._names()
        self.version += 1
        self._notify()
        self.state["last_run"] = stats | {"at": time.time(), "duration_s": round(time.time() - t0, 1)}
        log.info("structure scout: %s", self.state["last_run"])
        return self.state["last_run"]

    async def _resolve(self, contributors: list[tuple[int, int, list[str]]], stats: dict[str, int]) -> None:
        readers = {c: (c, o) for c, o, scopes in contributors if READ_STRUCTURES in scopes}
        stale = time.time() - self.s.structure_refresh_hours * 3600
        todo = self.db.query(
            "SELECT structure_id FROM known_structures WHERE status IN ('new','ok','no_access') AND checked_at < ? "
            "ORDER BY checked_at LIMIT 2000",
            (stale,),
        )
        sem = asyncio.Semaphore(self.s.concurrency)

        async def resolve(sid: int) -> None:
            seen_by = [
                r[0] for r in self.db.query("SELECT character_id FROM structure_sightings WHERE structure_id=?", (sid,))
            ]
            candidates = [readers[c] for c in seen_by if c in readers]
            if not candidates:
                return  # nobody who may read it right now: keep what we know, try again later
            outcome = "no_access"
            body: dict[str, Any] | None = None
            async with sem:
                for cid, owner in candidates:
                    try:
                        token = await self.chars.access_token(cid, owner)
                        body = await self.esi.get(f"/universe/structures/{sid}", token)
                        outcome = "ok"
                        break
                    except SsoError:
                        continue
                    except EsiError as exc:
                        if exc.status == 404:
                            outcome = "gone"
                            break
                        if exc.status in (401, 403):
                            continue
                        stats["errors"] += 1
                        return  # ESI trouble: retry next run
            now = time.time()
            if outcome == "ok" and body:
                self.db.execute(
                    "UPDATE known_structures SET name=?, owner_id=?, solar_system_id=?, type_id=?, status='ok', "
                    "checked_at=? WHERE structure_id=?",
                    (
                        body.get("name"),
                        body.get("owner_id"),
                        body.get("solar_system_id"),
                        body.get("type_id"),
                        now,
                        sid,
                    ),
                )
                stats["resolved"] += 1
            else:
                self.db.execute(
                    "UPDATE known_structures SET status=?, checked_at=? WHERE structure_id=?", (outcome, now, sid)
                )
                stats["no_access" if outcome == "no_access" else "gone"] += 1

        await asyncio.gather(*(resolve(r[0]) for r in todo))

    async def _names(self) -> None:
        ids = {r[0] for r in self.db.query("SELECT DISTINCT owner_id FROM known_structures WHERE owner_id IS NOT NULL")}
        ids |= {r[0] for r in self.db.query("SELECT DISTINCT type_id FROM known_structures WHERE type_id IS NOT NULL")}
        if not ids:
            return
        known: set[int] = set()
        ordered = sorted(ids)
        for start in range(0, len(ordered), 900):
            chunk = ordered[start : start + 900]
            sql = f"SELECT entity_id FROM entity_names WHERE entity_id IN ({','.join('?' * len(chunk))})"  # noqa: S608
            known.update(r[0] for r in self.db.query(sql, tuple(chunk)))
        missing = [i for i in ids if i not in known]
        if not missing:
            return
        try:
            found = await self.esi.names_categories(missing)
        except EsiError as exc:
            log.warning("structure scout: name lookup failed (retried next run): %s", exc)
            return
        now = time.time()
        with self.db.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO entity_names(entity_id, name, category, updated_at) VALUES(?,?,?,?)",
                [(i, n, cat, now) for i, (n, cat) in found.items()],
            )

    # -- reading ------------------------------------------------------------------------------------
    def structures(self) -> list[dict[str, Any]]:
        """Every resolved structure with its system (cached until the next scout step)."""
        if self._rows is not None and self._rows[0] == self.version:
            return self._rows[1]
        rows = self.db.query(
            "SELECT k.*, (SELECT COUNT(*) FROM structure_sightings s WHERE s.structure_id=k.structure_id) AS seen_by, "
            "o.name AS owner_name, t.name AS type_name FROM known_structures k "
            "LEFT JOIN entity_names o ON o.entity_id=k.owner_id LEFT JOIN entity_names t ON t.entity_id=k.type_id "
            "WHERE k.solar_system_id IS NOT NULL AND k.status IN ('ok','no_access')"
        )
        out = []
        for r in rows:
            idx = self.g.index.get(r["solar_system_id"])
            if idx is None:
                continue
            out.append(
                {
                    "structure_id": r["structure_id"],
                    "name": r["name"] or f"Structure {r['structure_id']}",
                    "type_id": r["type_id"],
                    "type": r["type_name"] or f"Type {r['type_id']}",
                    "owner_id": r["owner_id"],
                    "owner": r["owner_name"] or f"Corporation {r['owner_id']}",
                    "owner_zkill": f"{ZKILL}/corporation/{r['owner_id']}/" if r["owner_id"] else None,
                    "system": self.g.describe(idx) | {"space": space_of(self.g, idx)},
                    "system_index": idx,
                    "seen_by": r["seen_by"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "checked_at": r["checked_at"],
                    "access": r["status"] == "ok",
                }
            )
        self._rows = (self.version, out)
        return out

    def index(self) -> dict[int, list[Any]]:
        """owner corporation -> Structure, for the war structure lists."""
        from .wars.structures import Structure

        idx: dict[int, list[Any]] = {}
        for r in self.structures():
            if r["owner_id"]:
                idx.setdefault(int(r["owner_id"]), []).append(
                    Structure(
                        r["structure_id"], r["name"], int(r["type_id"] or 0), r["system"]["id"], int(r["owner_id"])
                    )
                )
        return idx

    def _notify(self) -> None:
        if not self.on_change:
            return
        idx = self.index()
        for callback in self.on_change:
            callback(idx)

    def listing(
        self,
        sources: dict[int, list[str]],
        avoid: list[int],
        at_war: dict[int, list[int]] | None = None,
        *,
        q: str = "",
        region: str = "",
        type_name: str = "",
        space: str = "",
        war_only: bool = False,
        sort: str = "jumps",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        base = self.structures()
        dist, origin = self.g.nearest(sources.keys(), avoid)
        at_war = at_war or {}
        rows = []
        for r in base:
            i, o = r["system_index"], origin[r["system_index"]]
            item = {k: v for k, v in r.items() if k != "system_index"}
            item["jumps"] = dist[i] if dist[i] >= 0 else None
            item["nearest"] = None if o < 0 else {"system": self.g.names[o], "characters": sources.get(o, [])}
            item["wars"] = at_war.get(int(r["owner_id"] or 0), [])
            rows.append(item)
        facets = {
            "types": Counter(r["type"] for r in rows).most_common(),
            "regions": Counter(r["system"]["region"] for r in rows).most_common(),
            "spaces": Counter(r["system"]["space"] for r in rows).most_common(),
            "total": len(rows),
        }
        needle = q.strip().lower()
        if needle:
            rows = [r for r in rows if needle in f"{r['name']} {r['system']['name']} {r['owner']}".lower()]
        if region:
            rows = [r for r in rows if r["system"]["region"] == region]
        if type_name:
            rows = [r for r in rows if r["type"] == type_name]
        if space:
            rows = [r for r in rows if r["system"]["space"] == space]
        if war_only:
            rows = [r for r in rows if r["wars"]]
        far = 1 << 30
        keys = {
            "jumps": lambda r: (r["jumps"] if r["jumps"] is not None else far, r["system"]["name"], r["name"]),
            "system": lambda r: (r["system"]["name"], r["name"]),
            "name": lambda r: (r["name"].lower(),),
            "type": lambda r: (r["type"], r["system"]["name"]),
            "owner": lambda r: (r["owner"].lower(), r["system"]["name"]),
            "security": lambda r: (-r["system"]["security"], r["system"]["name"]),
            "seen": lambda r: (-r["last_seen"],),
        }
        rows.sort(key=keys.get(sort, keys["jumps"]))
        contributors = self.db.query("SELECT COUNT(*), COALESCE(SUM(structures),0) FROM scout_characters")[0]
        return {
            "rows": rows[offset : offset + limit],
            "matched": len(rows),
            "facets": facets,
            "characters": sum(len(v) for v in sources.values()),
            "scout": {
                "contributing_characters": contributors[0],
                "last_run": self.state["last_run"],
                "running": self.state["running"],
            },
        }

    # -- background ---------------------------------------------------------------------------------
    async def _loop(self) -> None:
        self.state["running"] = True
        try:
            while not self._stop.is_set():
                try:
                    await self.run()
                    self.state["last_error"] = None
                except Exception as exc:
                    self.state["last_error"] = redact(repr(exc))
                    log.exception("structure scout step failed")
                self.state["cycles"] += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.s.interval_min * 60)
                except TimeoutError:
                    pass
        finally:
            self.state["running"] = False

    def start(self) -> None:
        if self._task is None:
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


# --------------------------------------------------------------------------- demo
DEMO_KNOWN: list[tuple[str, str, str, str]] = [  # (system, name, type, owner): not in the public demo dataset
    ("Jita", "Ledger Vault", "Astrahus", "Crimson Ledger Syndicate"),
    ("Perimeter", "Tollgate Market", "Tatara", "Grey Tollkeepers"),
    ("Rens", "Brutor Hall", "Raitaru", "Frostline Compact"),
    ("Old Man Star", "Star Forge", "Raitaru", "Frostline Compact"),
    ("Amamake", "Ember Post", "Astrahus", "Ashen Veil"),
    ("1DQ1-A", "Northern Bastion", "Keepstar", "Frostline Compact"),
    ("Thera", "Drifter Watch", "Astrahus", "Frostline Compact"),
]
DEMO_TYPES = {
    "Astrahus": 35832,
    "Fortizar": 35833,
    "Keepstar": 35834,
    "Raitaru": 35825,
    "Azbel": 35826,
    "Athanor": 35835,
    "Tatara": 35836,
}


def seed_demo_structures(db: Database, graph: GateGraph, corp_ids: dict[str, int]) -> int:
    """Structures 'found' by the demo characters, in every kind of space. Returns the number inserted."""
    if db.query("SELECT 1 FROM known_structures LIMIT 1"):
        return 0
    now = time.time()
    rows, names = [], []
    for n, (system, name, stype, owner) in enumerate(DEMO_KNOWN):
        idx = graph.find(system)
        if idx is None:
            continue
        oid = corp_ids.get(owner) or 98400000 + n
        corp_ids.setdefault(owner, oid)
        rows.append(
            (
                1_200_000_000_000 + n,
                f"{system} - {name}",
                oid,
                graph.ids[idx],
                DEMO_TYPES[stype],
                "ok",
                now - 86400 * (n + 1),
                now - 3600 * n,
                now,
            )
        )
        names.append((oid, owner, "corporation", now))
    names += [(tid, t, "inventory_type", now) for t, tid in DEMO_TYPES.items()]
    with db.tx() as c:
        c.executemany(
            "INSERT OR IGNORE INTO known_structures(structure_id,name,owner_id,solar_system_id,type_id,status,"
            "first_seen,last_seen,checked_at) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
        c.executemany("INSERT OR IGNORE INTO entity_names VALUES(?,?,?,?)", names)
        c.executemany(
            "INSERT OR IGNORE INTO structure_sightings(structure_id, character_id, seen_at) VALUES(?,?,?)",
            [(r[0], 3900000001 + k % 3, now) for k, r in enumerate(rows)],
        )
    return len(rows)
