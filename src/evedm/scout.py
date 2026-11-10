"""Structure scout: player-owned structures found through the assets of every registered character.

For each character that granted ``esi-assets.read_assets.v1`` the scout reads the asset list (locations only are
kept) and takes every location that is not one of the character's own items as a structure candidate. Candidates are
read from ``/universe/structures/{id}`` with ``esi-universe.read_structures.v1``, using the token of a character
with assets there first, then other scoped characters for unidentified structures. Only Upwell
structures answer that endpoint. The result is shared by every main on the site (the Structures page) and also feeds
the structure lists of the Wars page.
"""

from __future__ import annotations

import asyncio
import bz2
import contextlib
import csv
import io
import json
import logging
import time
from collections import Counter
from typing import Any

import httpx

from .characters import REAUTH, CharacterService
from .config import STRUCTURE_SCOPES, Settings
from .crypto import redact
from .db import Database
from .esi import EsiClient, EsiError
from .sso import SsoError
from .universe import GateGraph, security_class

SCOUT_ERROR_RESERVE = 60  # stop resolving structures when ESI has this few errors left in its 100-per-minute window

log = logging.getLogger("evedm.scout")

READ_STRUCTURES, READ_ASSETS = STRUCTURE_SCOPES


class ResolveError(Exception):
    """The one-by-one resolver cannot start (``code``: busy, no_character, no_scope, reauth, demo)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

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
        self._directory_due = 0.0  # Adam4EVE
        self._everef_due = 0.0  # EVE Ref
        self._history_forced = -1e9  # last forced dataset download (monotonic)
        self.version = 0  # bumped when known structures change (caches, the Wars page index)
        self.on_change: list[Any] = []  # callbacks(index) when the structure set changes
        self.state: dict[str, Any] = {"running": False, "cycles": 0, "last_run": None, "last_error": None}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._unresolved: tuple[int, list[dict[str, Any]]] | None = None
        self._rows: tuple[int, list[dict[str, Any]]] | None = None
        self._main_lock = asyncio.Lock()  # one-by-one resolution with a main's token: one run at a time

    # -- which characters can contribute ---------------------------------------------------------
    def contributors(self, scope: str = READ_ASSETS) -> list[tuple[int, int, list[str]]]:
        """(character_id, owner_id, scopes) per EVE character with the requested scope, one link per character."""
        best: dict[int, tuple[int, int, list[str]]] = {}
        rows = self.db.query(
            "SELECT c.owner_id, c.character_id, c.scopes, c.status, c.demo, t.updated_at FROM characters c "
            "JOIN tokens t ON t.owner_id=c.owner_id AND t.character_id=c.character_id ORDER BY t.updated_at"
        )
        for r in rows:
            scopes = json.loads(r["scopes"])
            if r["status"] == REAUTH or r["demo"] or scope not in scopes:
                continue
            best[r["character_id"]] = (r["character_id"], r["owner_id"], scopes)  # newest token wins
        return list(best.values())

    def _save_sightings(self, character_id: int, structure_ids: set[int]) -> None:
        """Record that a character has items in these structures (one transaction). Known ones are only touched."""
        now = time.time()
        with self.db.tx() as c:
            c.executemany(
                "INSERT INTO structure_sightings(structure_id, character_id, seen_at) VALUES(?,?,?) "
                "ON CONFLICT(structure_id, character_id) DO UPDATE SET seen_at=excluded.seen_at",
                [(sid, character_id, now) for sid in structure_ids],
            )
            c.executemany(
                "INSERT INTO known_structures(structure_id, first_seen, last_seen) VALUES(?,?,?) "
                "ON CONFLICT(structure_id) DO UPDATE SET last_seen=excluded.last_seen",
                [(sid, now, now) for sid in structure_ids],
            )

    # -- one scout step -----------------------------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        t0 = time.time()
        stats = {
            "characters_read": 0,
            "candidates": 0,
            "resolved": 0,
            "no_access": 0,
            "gone": 0,
            "errors": 0,
            "truncated": 0,
        }
        contributors = self.contributors()
        linked = {c for c, _, _ in contributors}
        checked = {r[0]: r[1] for r in self.db.query("SELECT character_id, checked_at FROM scout_characters")}
        due = [c for c in contributors if time.time() - checked.get(c[0], 0) > self.s.assets_refresh_hours * 3600]
        sem = asyncio.Semaphore(self.s.concurrency)

        async def read_assets(item: tuple[int, int, list[str]]) -> None:
            """One character's assets, page by page: each page's structure IDs go to the database at once."""
            cid, owner, _ = item
            started = time.time()
            found: set[int] = set()
            total_pages = 1
            async with sem:
                try:
                    token = await self.chars.access_token(cid, owner)
                    async for page, pages_reported in self.esi.iter_pages(
                        f"/characters/{cid}/assets", token, max_pages=self.s.max_asset_pages
                    ):
                        total_pages = pages_reported
                        # ESI types the location of an item inside a structure "other"; "item" is a container or
                        # ship, "station" an NPC station. Only the IDs are kept: one record per structure.
                        fresh = {
                            int(a["location_id"])
                            for a in page
                            if a.get("location_type") == "other"
                            and int(a.get("location_id") or 0) >= 1_000_000_000_000  # NPC stations are 60 million
                        } - found
                        if fresh:
                            found |= fresh
                            self._save_sightings(cid, fresh)
                except (EsiError, SsoError) as exc:
                    stats["errors"] += 1
                    # what the earlier pages found is kept. A passing ESI trouble (error limit, 5xx, network) does not
                    # use up the refresh interval: the character is read again on the next run
                    passing = isinstance(exc, EsiError) and (exc.status in (0, 420, 429) or exc.status >= 500)
                    self.db.execute(
                        "INSERT INTO scout_characters(character_id, checked_at, error) VALUES(?,?,?) "
                        "ON CONFLICT(character_id) DO UPDATE SET "
                        "checked_at=CASE WHEN ? THEN checked_at ELSE excluded.checked_at END, error=excluded.error",
                        (cid, 0.0 if passing else time.time(), redact(str(exc))[:200], int(passing)),
                    )
                    return
            truncated = total_pages > self.s.max_asset_pages
            now = time.time()
            with self.db.tx() as c:
                if not truncated:  # a complete read: structures this character no longer has items in are dropped
                    c.execute("DELETE FROM structure_sightings WHERE character_id=? AND seen_at < ?", (cid, started))
                c.execute(
                    "INSERT INTO scout_characters(character_id, checked_at, structures, error) VALUES(?,?,?,?) "
                    "ON CONFLICT(character_id) DO UPDATE SET checked_at=excluded.checked_at, "
                    "structures=excluded.structures, error=excluded.error",
                    (cid, now, len(found), None),  # a cut-off list is not an error: one item per structure is enough
                )
            if truncated:
                stats["truncated"] += 1
                log.info(
                    "structure scout: %s has more than %s asset pages; structures on the rest are not seen",
                    cid,
                    self.s.max_asset_pages,
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
        # known_structures is never pruned: a structure stays until ESI answers 404 (destroyed or unanchored), then it
        # is kept as status 'gone' (not listed, not asked again), whoever sights it and whether or not anyone may dock
        await self._resolve(stats)
        await self._refresh_directory()
        await self._names()
        self.version += 1
        self._notify()
        self.state["last_run"] = stats | {"at": time.time(), "duration_s": round(time.time() - t0, 1)}
        log.info("structure scout: %s", self.state["last_run"])
        return self.state["last_run"]

    def _save_read(self, sid: int, body: dict[str, Any], now: float) -> None:
        """Store what ``/universe/structures/{id}`` returned: name, owner, system, type and position."""
        pos = body.get("position") or {}
        self.db.execute(
            "UPDATE known_structures SET name=?, owner_id=?, solar_system_id=?, type_id=?, status='ok', "
            "pos_x=?, pos_y=?, pos_z=?, checked_at=? WHERE structure_id=?",
            (
                body.get("name"),
                body.get("owner_id"),
                body.get("solar_system_id"),
                body.get("type_id"),
                pos.get("x"),
                pos.get("y"),
                pos.get("z"),
                now,
                sid,
            ),
        )

    async def resolve_with_main(self, main_id: int, *, limit: int = 200) -> dict[str, Any]:
        """Read the unresolved structures one by one with the token of ONE character: the signed-in main.

        1. ``GET /universe/structures/`` (no token) lists the structures with an open access list; any of them that
           is unresolved is asked first because the main's token is certain to be accepted there.
        2. ``GET /universe/structures/{id}`` (``esi-universe.read_structures.v1``, docking access needed) then reads
           each remaining ID sequentially: 200 stores name, owner, system and type; 403 keeps the row unresolved
           (``no_access``); 404 marks it gone (destroyed or unanchored). Rows never tried, or tried longest ago,
           go first, so repeated runs work through the whole list instead of repeating the same refusals.
        3. The run stops early, and says so, when ESI's error window is nearly used up (a 403 counts as an error),
           on a passing ESI failure, or when the main's token is no longer accepted.

        ESI cannot search a structure by ID (character search needs a name fragment), so there is no search step.
        """
        if self.settings.demo:
            raise ResolveError("demo", "resolving structures is not available in demo mode")
        row = self.db.get_character(main_id, main_id)
        if row is None or row.demo:
            raise ResolveError("no_character", "the signed-in main character is not registered on this site")
        if row.status == REAUTH:
            raise ResolveError("reauth", "the main character must sign in again (Add character) to read structures")
        if READ_STRUCTURES not in row.scopes:
            raise ResolveError("no_scope", f"the main character has not granted {READ_STRUCTURES}: Add character again")
        if self._main_lock.locked():
            raise ResolveError("busy", "a resolution run is already in progress")
        async with self._main_lock:
            return await self._resolve_with_main(main_id, max(1, limit))

    async def _resolve_with_main(self, main_id: int, limit: int) -> dict[str, Any]:
        out: dict[str, Any] = {
            "character_id": main_id,
            "targets": 0,
            "public_first": 0,
            "resolved": 0,
            "no_access": 0,
            "gone": 0,
            "errors": 0,
            "remaining": 0,
            "stopped": None,
        }
        try:
            listed = await self.esi.get("/universe/structures/")
            public = {int(i) for i in listed} if isinstance(listed, list) else set()
        except EsiError as exc:
            log.warning("structure resolver: public structure list unavailable (%s); going one by one", exc)
            public = set()
        rows = self.db.query(
            "SELECT structure_id FROM known_structures WHERE status IN ('new','no_access','ok') "
            "AND destroyed_at IS NULL AND (name IS NULL OR owner_id IS NULL OR solar_system_id IS NULL) "
            "ORDER BY checked_at, structure_id"
        )
        ids = [r[0] for r in rows]
        ids.sort(key=lambda sid: sid not in public)  # stable: public first, otherwise least recently tried first
        out["targets"] = len(ids)
        out["public_first"] = sum(1 for sid in ids if sid in public)
        batch, rest = ids[:limit], ids[limit:]
        for n, sid in enumerate(batch):
            wait = self.esi.blocked_for(reserve=SCOUT_ERROR_RESERVE)
            if wait > 0:
                out["stopped"] = "error_limit"
                out["retry_after_s"] = round(wait)
                rest = batch[n:] + rest
                break
            try:
                token = await self.chars.access_token(main_id, main_id)
                body = await self.esi.get(f"/universe/structures/{sid}", token)
            except SsoError:
                out["stopped"] = "reauth"
                rest = batch[n:] + rest
                break
            except EsiError as exc:
                now = time.time()
                if exc.status == 404:
                    self.db.execute(
                        "UPDATE known_structures SET status='gone', checked_at=? WHERE structure_id=?", (now, sid)
                    )
                    out["gone"] += 1
                elif exc.status == 403:
                    self.db.execute(
                        "UPDATE known_structures SET status='no_access', checked_at=? WHERE structure_id=?", (now, sid)
                    )
                    out["no_access"] += 1
                elif exc.status == 401:
                    out["stopped"] = "reauth"
                    rest = batch[n:] + rest
                    break
                else:  # 0, 420, 429, 5xx: a passing ESI failure; nothing is marked checked
                    out["errors"] += 1
                    out["stopped"] = "esi_error"
                    rest = batch[n:] + rest
                    break
                continue
            if isinstance(body, dict) and body:
                self._save_read(sid, body, time.time())
                out["resolved"] += 1
        out["remaining"] = len(rest)
        # structures ESI refuses to the main are matched against the public datasets (EVE Ref, Adam4EVE); the
        # download is forced at most every 10 minutes so repeated clicks do not hammer the dataset hosts
        force = time.monotonic() - self._history_forced > 600
        if force:
            self._history_forced = time.monotonic()
        out["history"] = await self._refresh_directory(force=force)
        if out["resolved"] or out["gone"] or any(out["history"].values()):
            await self._names()
            self.version += 1
            self._notify()
        log.info("structure resolver (main %s): %s", main_id, out)
        return out

    async def _resolve(self, stats: dict[str, int]) -> None:
        readers = {c: (c, o) for c, o, _ in self.contributors(READ_STRUCTURES)}
        stale = time.time() - self.s.structure_refresh_hours * 3600
        stale_denied = time.time() - self.s.no_access_refresh_hours * 3600  # a refused read is an ESI error: ask rarely
        # a structure that was read (status ok) is known: skipped from then on, unless a refresh interval is set
        refresh_known = self.s.structure_refresh_hours > 0
        todo = self.db.query(
            "SELECT structure_id, name, owner_id FROM known_structures WHERE status = 'new' "
            "OR (status = 'ok' AND ? AND checked_at < ?) OR (status = 'no_access' AND checked_at < ?) "
            "ORDER BY checked_at LIMIT 2000",
            (int(refresh_known), stale, stale_denied),
        )
        sem = asyncio.Semaphore(self.s.concurrency)

        async def resolve(sid: int, unidentified: bool) -> None:
            seen_by = {
                r[0] for r in self.db.query("SELECT character_id FROM structure_sightings WHERE structure_id=?", (sid,))
            }
            candidates = [readers[c] for c in sorted(seen_by) if c in readers]
            if unidentified:
                candidates.extend(reader for cid, reader in readers.items() if cid not in seen_by)
            if not candidates:
                # nobody who may read it right now: keep what we know and try again later, behind the rows not yet asked
                self.db.execute("UPDATE known_structures SET checked_at=? WHERE structure_id=?", (time.time(), sid))
                return
            outcome = "no_access"
            body: dict[str, Any] | None = None
            async with sem:
                for cid, owner in candidates:
                    if self.esi.blocked_for(reserve=SCOUT_ERROR_RESERVE) > 0:
                        return  # leave the rest of ESI's error budget to the character refresh; retry next run
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
                self._save_read(sid, body, now)
                stats["resolved"] += 1
            else:
                self.db.execute(
                    "UPDATE known_structures SET status=?, checked_at=? WHERE structure_id=?", (outcome, now, sid)
                )
                stats["no_access" if outcome == "no_access" else "gone"] += 1

        await asyncio.gather(*(resolve(r["structure_id"], not r["name"] or not r["owner_id"]) for r in todo))

    # historical details: public datasets, kept apart from live ESI records (EVE Ref outranks Adam4EVE)
    HISTORY_PRIORITY = {"EVE Ref": 2, "Adam4EVE": 1}

    def _history_wanted(self) -> set[int]:
        return {
            r[0]
            for r in self.db.query(
                "SELECT structure_id FROM known_structures WHERE status != 'gone' AND destroyed_at IS NULL "
                "AND (name IS NULL OR owner_id IS NULL OR solar_system_id IS NULL)"
            )
        }

    def _store_history(self, rows: list[tuple[Any, ...]], source: str) -> None:
        """rows: (structure_id, name, owner_id, owner_name, system_id, type_id, type_name, observed).

        A source never overwrites an entry of a higher-priority source; fields the new record lacks are kept.
        """
        if not rows:
            return
        with self.db.tx() as conn:
            conn.executemany(
                "INSERT INTO public_structure_history "
                "(structure_id,name,owner_id,owner_name,system_id,type_id,type_name,observed,source) "
                "VALUES(?,?,?,?,?,?,?,?,'" + source.replace("'", "") + "') "
                "ON CONFLICT(structure_id) DO UPDATE SET name=COALESCE(excluded.name,name), "
                "owner_id=COALESCE(excluded.owner_id,owner_id), owner_name=COALESCE(excluded.owner_name,owner_name), "
                "system_id=COALESCE(excluded.system_id,system_id), type_id=COALESCE(excluded.type_id,type_id), "
                "type_name=COALESCE(excluded.type_name,type_name), observed=excluded.observed, source=excluded.source "
                "WHERE ? >= CASE COALESCE(source,'Adam4EVE') WHEN 'EVE Ref' THEN 2 ELSE 1 END",
                [(*r, self.HISTORY_PRIORITY.get(source, 1)) for r in rows],
            )

    async def _refresh_directory(self, force: bool = False) -> dict[str, int]:
        """Match the public datasets locally (EVE Ref, then Adam4EVE); returns the matches stored per source."""
        found = {"EVE Ref": 0, "Adam4EVE": 0}
        if self.settings.demo:
            return found
        wanted = self._history_wanted()
        if not wanted:
            return found
        if self.s.everef_enabled and (force or time.monotonic() >= self._everef_due):
            try:
                rows = await self._fetch_everef(wanted)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                self._everef_due = time.monotonic() + 3600
                log.warning("EVE Ref structure dataset unavailable: %s", exc)
            else:
                self._store_history(rows, "EVE Ref")
                found["EVE Ref"] = len(rows)
                self._everef_due = time.monotonic() + 86400
        if force or time.monotonic() >= self._directory_due:
            try:
                rows = await self._fetch_adam4eve(wanted)
            except (httpx.HTTPError, csv.Error, ValueError) as exc:
                self._directory_due = time.monotonic() + 3600
                log.warning("Adam4EVE structure export unavailable: %s", exc)
            else:
                self._store_history(rows, "Adam4EVE")
                found["Adam4EVE"] = len(rows)
                self._directory_due = time.monotonic() + 86400
        return found

    async def _fetch_adam4eve(self, wanted: set[int]) -> list[tuple[Any, ...]]:
        url = "https://static.adam4eve.eu/IDs/playerStructure_extended.csv"
        response = await self.esi.http.get(url, timeout=20)
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";")
        if not {"structureID", "name", "ownerID", "lastSeen"} <= set(reader.fieldnames or ()):
            raise ValueError("unrecognized CSV header")
        wanted_text = {str(i) for i in wanted}
        matches: list[tuple[Any, ...]] = []
        for row in reader:
            sid = row["structureID"]
            if sid not in wanted_text:
                continue
            name = (row["name"] or "").strip()
            owner = (row["ownerID"] or "").strip()
            if not name and not owner.isdecimal():
                continue
            matches.append(
                (
                    int(sid),
                    name or None,
                    int(owner) if owner.isdecimal() else None,
                    row.get("ownerName"),
                    row.get("solarSystemID") or None,
                    row.get("typeID") or None,
                    row.get("typeName"),
                    row["lastSeen"] or None,
                )
            )
        return matches

    async def _fetch_everef(self, wanted: set[int]) -> list[tuple[Any, ...]]:
        """The EVE Ref scrape: an object keyed by structure ID whose values are ESI's structure answer plus
        ``is_gettable_structure``, ``last_structure_get``, ``is_public_structure`` and ``first_seen``."""
        cap = max(1, self.s.everef_max_mb) * 1024 * 1024
        buf = bytearray()
        async with self.esi.http.stream("GET", self.s.everef_url, timeout=120) as response:
            response.raise_for_status()
            if int(response.headers.get("content-length") or 0) > cap:
                raise ValueError(f"download larger than {self.s.everef_max_mb} MB")
            async for chunk in response.aiter_bytes():
                buf += chunk
                if len(buf) > cap:
                    raise ValueError(f"download larger than {self.s.everef_max_mb} MB")
        payload = bytes(buf)
        if payload[:3] == b"BZh":  # a .bz2 scrape, if the URL points at one
            unpacked = bz2.BZ2Decompressor().decompress(payload, max_length=cap + 1)
            if len(unpacked) > cap:
                raise ValueError(f"unpacked data larger than {self.s.everef_max_mb} MB")
            payload = unpacked
        data = json.loads(payload)
        del payload, buf
        if not isinstance(data, dict):
            raise ValueError("unrecognized EVE Ref structure file")
        matches: list[tuple[Any, ...]] = []
        for key, rec in data.items():
            try:
                sid = int(key)
            except ValueError:
                continue
            if sid not in wanted or not isinstance(rec, dict):
                continue
            name = (rec.get("name") or "").strip() or None
            owner = rec.get("owner_id")
            if not name and not owner:  # an ID the scrape never got details for tells nothing
                continue
            stamps = [rec.get(k) for k in ("last_structure_get", "last_seen_public_structure", "first_seen")]
            observed = next((t for t in stamps if t and not str(t).startswith("1970-01-01")), None)
            matches.append(
                (sid, name, owner, None, rec.get("solar_system_id"), rec.get("type_id"), None, observed)
            )
        return matches

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
                    "name_unresolved": not r["name"],
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
                    "status": r["status"],
                    "position": (r["pos_x"], r["pos_y"], r["pos_z"]) if r["pos_x"] is not None else None,
                    "destroyed_at": r["destroyed_at"],
                }
            )
        self._rows = (self.version, out)
        return out

    def unresolved(self) -> list[dict[str, Any]]:
        """Structures stored without a system: ESI gave no name, owner or system (nobody may dock there, or not read
        yet). They are listed with what is known: the ID, the status and when they were seen."""
        if self._unresolved is not None and self._unresolved[0] == self.version:
            return self._unresolved[1]
        rows = self.db.query(
            "SELECT k.*, (SELECT COUNT(*) FROM structure_sightings s WHERE s.structure_id=k.structure_id) AS seen_by "
            "FROM known_structures k WHERE k.solar_system_id IS NULL AND k.status IN ('new','ok','no_access')"
        )
        unknown = {
            "id": None,
            "name": "Unknown",
            "region": "Unknown",
            "security": -9.9,
            "class": "unknown",
            "space": "unknown",
        }
        out = [
            {
                "structure_id": r["structure_id"],
                "name_unresolved": not r["name"],
                "name": r["name"] or f"Unknown structure {r['structure_id']}",
                "type_id": r["type_id"],
                "type": "Unknown",
                "owner_id": r["owner_id"],
                "owner": "Unknown",
                "owner_zkill": None,
                "system": unknown,
                "system_index": None,
                "seen_by": r["seen_by"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "checked_at": r["checked_at"],
                "access": False,
                "status": "unread" if r["status"] == "new" else r["status"],
                "position": None,
                "destroyed_at": r["destroyed_at"],
            }
            for r in rows
        ]
        self._unresolved = (self.version, out)
        return out

    def index(self) -> dict[int, list[Any]]:
        """owner corporation -> Structure, for the war structure lists."""
        from .wars.structures import Structure

        idx: dict[int, list[Any]] = {}
        for r in self.structures():
            if r["owner_id"]:
                idx.setdefault(int(r["owner_id"]), []).append(
                    Structure(
                        r["structure_id"],
                        r["name"],
                        int(r["type_id"] or 0),
                        r["system"]["id"],
                        int(r["owner_id"]),
                        r["position"],
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
        base = self.structures() + self.unresolved()
        history = {r["structure_id"]: r for r in self.db.query("SELECT * FROM public_structure_history")}
        dist, origin = self.g.nearest(sources.keys(), avoid)
        at_war = at_war or {}
        rows = []
        for r in base:
            i = r["system_index"]
            item = {k: v for k, v in r.items() if k not in ("system_index", "position", "name_unresolved")}
            record = history.get(r["structure_id"])
            if record and r["destroyed_at"] is None:
                used = False
                if record["name"] and r["name_unresolved"]:
                    item["name"] = record["name"]
                    used = True
                if not r["owner_id"] and record["owner_id"]:
                    item["owner_id"] = record["owner_id"]
                    item["owner"] = record["owner_name"] or f"Corporation {record['owner_id']}"
                    item["owner_zkill"] = f"{ZKILL}/corporation/{record['owner_id']}/"
                    used = True
                if i is None and record["system_id"] in self.g.index:
                    i = self.g.index[record["system_id"]]
                    item["system"] = self.g.describe(i) | {"space": space_of(self.g, i)}
                    used = True
                if not r["type_id"] and record["type_id"]:
                    item["type_id"] = record["type_id"]
                    item["type"] = record["type_name"] or f"Type {record['type_id']}"
                    used = True
                if used:
                    item["historical_source"] = record["source"] or "Adam4EVE"
                    item["historical_date"] = record["observed"]
            o = origin[i] if i is not None else -1
            item["jumps"] = dist[i] if i is not None and dist[i] >= 0 else None
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
