"""Structure scout: player-owned structures found through the assets of every registered character.

For each character that granted ``esi-assets.read_assets.v1`` the scout reads the whole asset list (only the item IDs
and the locations are kept). A location is a structure candidate when its ID is at least 1e12, its ``location_flag``
is Hangar, Deliveries or AssetSafety and it is not one of the character's own items (a ship or container in a
hangar). ESI's ``location_type`` is not used: items in a structure are typed "item" as well. Candidates are
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import httpx

from .characters import REAUTH, CharacterService
from .config import ANSIBLEX_SCOPE, STRUCTURE_SCOPES, Settings
from .crypto import redact
from .db import Database
from .esi import EsiClient, EsiError, search_structure_ids
from .sso import SsoError
from .structure_index import index_path
from .structure_index import lookup as lookup_index
from .universe import GateGraph, security_class

SCOUT_ERROR_RESERVE = 60  # stop resolving structures when ESI has this few errors left in its 100-per-minute window
STRUCTURE_MIN_ID = 1_000_000_000_000  # structures, customs offices, corporation offices and all items live above this
# Only items in these flags sit in a structure hangar (a corporation-less character's hangar, its deliveries or its
# asset safety). Fittings, cargo, drone bays and the like point at the character's own ships and containers.
STRUCTURE_FLAGS = frozenset({"Hangar", "Deliveries", "AssetSafety"})
ASSET_FILTER_VERSION = 2  # a character read with an older rule is read again at once, and the old finds are purged
ACCESS_KEEP_DAYS = 30  # a dock-search row nobody refreshed for this long is dropped
JOB_MAX_FAILURES = 3  # the Resolve unknown job stops after this many passing ESI failures in a row
JOB_PUBLISH_EVERY = 50  # ... and shows what it found (names, caches, Wars page) after this many resolved structures
# the structures the resolver still wants: no name, owner or system yet, not gone, not destroyed
UNKNOWN = (
    "status IN ('new','no_access','ok') AND destroyed_at IS NULL "
    "AND (name IS NULL OR owner_id IS NULL OR solar_system_id IS NULL)"
)

log = logging.getLogger("evedm.scout")

READ_STRUCTURES, READ_ASSETS = STRUCTURE_SCOPES
SEARCH_STRUCTURES = ANSIBLEX_SCOPE  # esi-search.search_structures.v1


class ResolveError(Exception):
    """The Resolve unknown job cannot start (``code``: busy, no_character, no_scope, reauth, demo)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AssetScan:
    """What one character's asset pages add up to: candidate locations and the character's own item IDs.

    Only two sets are kept, not the rows. The candidates are known only when the whole list was read, because a
    location that is the character's own item may be on a later page.
    """

    def __init__(self) -> None:
        self.located: set[int] = set()  # locations at 1e12 or more in a structure flag
        self.items: set[int] = set()  # the character's own item IDs at 1e12 or more

    def add(self, page: list[dict[str, Any]]) -> None:
        for a in page:
            if a.get("item_id") is not None and int(a["item_id"]) >= STRUCTURE_MIN_ID:
                self.items.add(int(a["item_id"]))
            loc = int(a.get("location_id") or 0)
            if loc >= STRUCTURE_MIN_ID and a.get("location_flag") in STRUCTURE_FLAGS:
                self.located.add(loc)

    @property
    def candidates(self) -> set[int]:
        return self.located - self.items


def is_structure_candidate(location_id: int, flag: str | None, own_items: Any) -> bool:
    """The ingestion rule for one asset row (``own_items``: anything supporting ``in``); the probe reports with it."""
    return location_id >= STRUCTURE_MIN_ID and flag in STRUCTURE_FLAGS and location_id not in own_items


@dataclass
class ResolveJob:
    """One run of the Resolve unknown button; the last one is kept in memory for the page to poll."""

    started_by: int
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    running: bool = True
    targets: int = 0
    done: int = 0  # structures dealt with: resolved + no_access + gone + skipped (a failed read is not done)
    resolved: int = 0
    no_access: int = 0  # every character asked in this pass was refused
    gone: int = 0
    skipped: int = 0  # nobody left to ask: not sent to ESI
    errors: int = 0
    public_first: int = 0
    paused_until: float | None = None  # epoch seconds, while waiting for ESI's error window
    stopped: str | None = None  # done | cancelled | esi_error | reauth | failed (None while running)
    failure: str | None = None  # what went wrong when stopped is "failed"
    history: dict[str, int] = field(default_factory=dict)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    published: int = 0  # value of ``resolved`` at the last publish

    def as_dict(self) -> dict[str, Any]:
        out = {
            k: getattr(self, k)
            for k in (
                "running started_at finished_at started_by targets done resolved no_access gone skipped errors "
                "public_first paused_until stopped failure history"
            ).split()
        }
        out["remaining"] = max(0, self.targets - self.done)
        return out


@dataclass
class _Pool:
    """What the choice of a character needs, read once per pass."""

    readers: dict[int, int]  # character_id -> owner_id of the link whose token is used (esi-universe.read_structures)
    sightings: dict[int, list[int]]  # structure_id -> characters with assets there (ordered by character ID)
    refused: dict[int, set[int]]  # structure_id -> characters refused less than no_access_refresh_hours ago
    asked: dict[int, set[int]]  # structure_id -> characters ever refused (the pair was asked before)
    dead: set[int] = field(default_factory=set)  # characters whose token was not accepted during this pass
    access: dict[int, list[int]] = field(default_factory=dict)  # structure_id -> characters that may dock there


class _Read(NamedTuple):
    """Outcome of asking ESI about one structure: ok | gone | no_access | skipped | blocked | error."""

    kind: str
    body: dict[str, Any] | None = None


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
        self._main_lock = asyncio.Lock()  # ESI reads of unresolved structures: the job or the hourly step, not both
        self._history_lock = asyncio.Lock()  # one public dataset download at a time
        self._job: ResolveJob | None = None
        self._own_hits: set[int] = set()  # stored IDs that turned out to be a character's own items: purged next
        self._job_task: asyncio.Task[None] | None = None
        self._by_region: dict[int, list[int]] | None = None  # region id -> system indexes (fallback terms)
        self._sleep: Callable[[float, asyncio.Event], Awaitable[None]] = self._cancellable_sleep  # tests replace it

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
            "purged_own_items": 0,
            "purged_orphans": 0,
            "dock_searched": 0,
            "dock_capped": 0,
            "dock_terms_capped": 0,
            "dock_errors": 0,
            "dock_found": 0,
        }
        contributors = self.contributors()
        linked = {c for c, _, _ in contributors}
        scouted = {r[0]: r for r in self.db.query("SELECT character_id, checked_at, filter_v FROM scout_characters")}
        now = time.time()
        due = [
            c
            for c in contributors
            if c[0] not in scouted
            or now - scouted[c[0]]["checked_at"] > self.s.assets_refresh_hours * 3600
            or scouted[c[0]]["filter_v"] < ASSET_FILTER_VERSION  # read with an older rule: again, at once
        ]
        sem = asyncio.Semaphore(self.s.concurrency)

        async def read_assets(item: tuple[int, int, list[str]]) -> None:
            """One character's whole asset list; its structure candidates are saved only after a complete read."""
            cid, owner, _ = item
            started = time.time()
            scan = AssetScan()
            total_pages = 1
            async with sem:
                try:
                    token = await self.chars.access_token(cid, owner)
                    async for page, pages_reported in self.esi.iter_pages(
                        f"/characters/{cid}/assets", token, max_pages=self.s.max_asset_pages
                    ):
                        total_pages = pages_reported
                        scan.add(page)
                except (EsiError, SsoError) as exc:
                    stats["errors"] += 1
                    # nothing is saved and the old sightings stay: a list read only in part cannot tell which
                    # locations are the character's own items. A passing ESI trouble (error limit, 5xx, network) does
                    # not use up the refresh interval: the character is read again on the next run
                    passing = isinstance(exc, EsiError) and (exc.status in (0, 420, 429) or exc.status >= 500)
                    self.db.execute(
                        "INSERT INTO scout_characters(character_id, checked_at, error) VALUES(?,?,?) "
                        "ON CONFLICT(character_id) DO UPDATE SET "
                        "checked_at=CASE WHEN ? THEN checked_at ELSE excluded.checked_at END, error=excluded.error",
                        (cid, 0.0 if passing else time.time(), redact(str(exc))[:200], int(passing)),
                    )
                    return
            if total_pages > self.s.max_asset_pages:  # cut short: same as a failed read, but not retried every run
                stats["truncated"] += 1
                log.info(
                    "structure scout: %s has more than %s asset pages; nothing is saved for this character",
                    cid,
                    self.s.max_asset_pages,
                )
                self.db.execute(
                    "INSERT INTO scout_characters(character_id, checked_at, error) VALUES(?,?,?) "
                    "ON CONFLICT(character_id) DO UPDATE SET checked_at=excluded.checked_at, error=excluded.error",
                    (cid, time.time(), f"truncated: more than {self.s.max_asset_pages} asset pages, nothing saved"),
                )
                return
            found = scan.candidates
            if found:
                self._save_sightings(cid, found)
            self._own_hits |= self._stored_among(scan.items)
            with self.db.tx() as c:  # a complete read: structures this character no longer has items in are dropped
                c.execute("DELETE FROM structure_sightings WHERE character_id=? AND seen_at < ?", (cid, started))
                c.execute(
                    "INSERT INTO scout_characters(character_id, checked_at, structures, error, filter_v) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET checked_at=excluded.checked_at, "
                    "structures=excluded.structures, error=excluded.error, filter_v=excluded.filter_v",
                    (cid, time.time(), len(found), None, ASSET_FILTER_VERSION),
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
        # refusals go with characters that no longer exist: every linked character counts here, whatever its scopes
        # (the main that only granted esi-universe.read_structures.v1 has no assets and is not in ``linked``)
        self.db.execute(
            "DELETE FROM structure_refusals WHERE character_id NOT IN (SELECT character_id FROM characters)"
        )
        await self._dock_search(stats)
        self._prune_access()
        # a structure stays until ESI answers 404 (destroyed or unanchored), then it is kept as status 'gone' (not
        # listed, not asked again). Only what is no structure at all is purged: a character's own items, and finds that
        # nobody sights any more once every character has been read with the current filter
        self._purge(stats, contributors)
        await self._resolve(stats)
        await self._refresh_directory()
        await self._names()
        self.version += 1
        self._notify()
        self.state["last_run"] = stats | {"at": time.time(), "duration_s": round(time.time() - t0, 1)}
        log.info("structure scout: %s", self.state["last_run"])
        return self.state["last_run"]

    # -- structures a character may dock at: ESI's character search -----------------------------------
    def dock_searchers(self) -> list[tuple[int, int, list[str]]]:
        """Characters holding both the search scope and the structures scope (one link per character)."""
        return [c for c in self.contributors(SEARCH_STRUCTURES) if READ_STRUCTURES in c[2]]

    def _region_systems(self) -> dict[int, list[int]]:
        if self._by_region is None:
            by: dict[int, list[int]] = {}
            for i, region in enumerate(self.g.region_of):
                by.setdefault(region, []).append(i)
            self._by_region = by
        return self._by_region

    def dock_terms(self, character_id: int) -> list[str]:
        """The fallback terms "<System> - " in the order they are tried for one character.

        The character's last known system and the systems of its region first, then the systems of the structures it
        sights or may dock at, then the regions of those. Each system once.
        """
        by_region = self._region_systems()
        order: list[int] = []
        snap = self.db.query("SELECT solar_system_id FROM snapshots WHERE character_id=?", (character_id,))
        home = self.g.index.get(snap[0][0]) if snap and snap[0][0] else None
        if home is not None:
            order.append(home)
            order.extend(by_region.get(self.g.region_of[home], ()))
        rows = self.db.query(
            "SELECT DISTINCT solar_system_id FROM known_structures WHERE solar_system_id IS NOT NULL AND "
            "(structure_id IN (SELECT structure_id FROM structure_sightings WHERE character_id=?1) "
            "OR structure_id IN (SELECT structure_id FROM structure_access WHERE character_id=?1)) "
            "ORDER BY solar_system_id",
            (character_id,),
        )
        systems = [self.g.index[r[0]] for r in rows if r[0] in self.g.index]
        order.extend(systems)
        for region in dict.fromkeys(self.g.region_of[i] for i in systems):
            order.extend(by_region.get(region, ()))
        return [f"{self.g.names[i]} - " for i in dict.fromkeys(order)]

    async def _dock_search(self, stats: dict[str, int]) -> None:
        """One search per due character (see ``_dock_one``); runs inside the scout step, before the purge."""
        if not self.s.dock_search_enabled:
            return
        searchers = self.dock_searchers()
        state = {r["character_id"]: r for r in self.db.query("SELECT * FROM scout_dock_search")}
        now = time.time()
        wait = self.s.dock_search_hours * 3600
        due = [c for c in searchers if c[0] not in state or now - state[c[0]]["checked_at"] > wait]
        sem = asyncio.Semaphore(self.s.concurrency)

        async def one(item: tuple[int, int, list[str]]) -> None:
            async with sem:
                await self._dock_one(item, state.get(item[0]), stats)

        await asyncio.gather(*(one(c) for c in due))

    async def _dock_one(self, item: tuple[int, int, list[str]], prev: Any, stats: dict[str, int]) -> None:
        """Search one character's dockable structures with the configured term.

        A result shorter than the cap is complete: it replaces the character's set. A result of cap or more may be cut
        off: it only adds, and ``dock_search_max_terms`` more searches with "<System> - " continue at the persisted
        cursor. A passing ESI trouble (420, 429, 5xx, network, error window low) keeps the interval unused; 401/403
        or a token that is not accepted records the error and waits the full interval.
        """
        cid, owner, _ = item
        started = time.time()
        cap = self.s.dock_search_cap
        found: set[int] = set()
        searched = False  # the search with the main term answered
        count = 0
        capped = False
        cursor = int(prev["cursor"]) if prev else 0
        error: str | None = None
        passing = False

        def blocked() -> bool:
            return self.esi.blocked_for(reserve=SCOUT_ERROR_RESERVE) > 0

        try:
            if blocked():
                return  # nothing recorded: the character is searched again next run
            token = await self.chars.access_token(cid, owner)
            raw = await search_structure_ids(self.esi, cid, token, self.s.dock_search_term)
            searched = True
            count = len(raw)
            capped = count >= cap
            found |= {i for i in raw if i >= STRUCTURE_MIN_ID}
            if capped:
                terms = self.dock_terms(cid)
                n = min(self.s.dock_search_max_terms, len(terms))
                start = cursor % len(terms) if terms else 0
                done = 0
                try:
                    for k in range(n):
                        if blocked():
                            passing = True
                            error = "paused: ESI error window low, continues next run"
                            break
                        raw = await search_structure_ids(self.esi, cid, token, terms[(start + k) % len(terms)])
                        done += 1
                        if len(raw) >= cap:
                            stats["dock_terms_capped"] += 1
                        found |= {i for i in raw if i >= STRUCTURE_MIN_ID}
                finally:
                    cursor = (start + done) % len(terms) if terms else 0
        except (EsiError, SsoError) as exc:
            passing = isinstance(exc, EsiError) and (exc.status in (0, 420, 429) or exc.status >= 500)
            error = redact(str(exc))[:200]
        now = time.time()
        complete = searched and not capped and error is None
        with self.db.tx() as c:
            if found:
                c.executemany(
                    "INSERT INTO structure_access(structure_id, character_id, seen_at) VALUES(?,?,?) "
                    "ON CONFLICT(structure_id, character_id) DO UPDATE SET seen_at=excluded.seen_at",
                    [(sid, cid, now) for sid in found],
                )
                c.executemany(
                    "INSERT INTO known_structures(structure_id, first_seen, last_seen) VALUES(?,?,?) "
                    "ON CONFLICT(structure_id) DO UPDATE SET last_seen=excluded.last_seen",
                    [(sid, now, now) for sid in found],
                )
            if complete:  # the whole list came back: structures no longer in it are dropped
                c.execute("DELETE FROM structure_access WHERE character_id=? AND seen_at < ?", (cid, started))
            if searched:
                c.execute(
                    "INSERT INTO scout_dock_search(character_id, checked_at, found, capped, cursor, error) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET "
                    "checked_at=CASE WHEN ? THEN checked_at ELSE excluded.checked_at END, found=excluded.found, "
                    "capped=excluded.capped, cursor=excluded.cursor, error=excluded.error",
                    (cid, 0.0 if passing else now, count, int(capped), cursor, error, int(passing)),
                )
            else:  # the search itself failed: what was stored before stays
                c.execute(
                    "INSERT INTO scout_dock_search(character_id, checked_at, error) VALUES(?,?,?) "
                    "ON CONFLICT(character_id) DO UPDATE SET "
                    "checked_at=CASE WHEN ? THEN checked_at ELSE excluded.checked_at END, error=excluded.error",
                    (cid, 0.0 if passing else now, error, int(passing)),
                )
        if searched:
            stats["dock_searched"] += 1
            stats["dock_found"] += len(found)
            stats["dock_capped"] += int(capped)
        if error:
            stats["dock_errors"] += 1
            log.warning("structure scout: dock search for character %s: %s", cid, error)
        log.info(
            "structure scout: character %s may dock at %s structures (result %s, cap %s%s)",
            cid, len(found), count, cap, ", capped" if capped else "",
        )

    def _prune_access(self) -> None:
        """Dock-search rows of characters that no longer exist, and rows nobody refreshed for 30 days."""
        gone = "character_id NOT IN (SELECT character_id FROM characters)"
        self.db.execute(f"DELETE FROM structure_access WHERE {gone}")  # noqa: S608
        self.db.execute(f"DELETE FROM scout_dock_search WHERE {gone}")  # noqa: S608
        keep = max(ACCESS_KEEP_DAYS * 86400.0, 2 * self.s.dock_search_hours * 3600)
        self.db.execute("DELETE FROM structure_access WHERE seen_at < ?", (time.time() - keep,))

    def _stored_among(self, item_ids: set[int]) -> set[int]:
        """The IDs among ``item_ids`` that known_structures holds (a character's own items stored as structures)."""
        hits: set[int] = set()
        ordered = sorted(item_ids)
        for start in range(0, len(ordered), 900):
            chunk = ordered[start : start + 900]
            sql = f"SELECT structure_id FROM known_structures WHERE structure_id IN ({','.join('?' * len(chunk))})"  # noqa: S608
            hits.update(r[0] for r in self.db.query(sql, tuple(chunk)))
        return hits

    def _bump_meta(self, key: str, n: int) -> None:
        self.db.set_meta(key, str(int(self.db.get_meta(key, "0") or 0) + n))

    def _filters_current(self, contributors: list[tuple[int, int, list[str]]]) -> bool:
        """True when there is a contributor and every one of them was last read completely with the current rule."""
        have = {r[0]: r[1] for r in self.db.query("SELECT character_id, filter_v FROM scout_characters")}
        return bool(contributors) and all(have.get(cid, 1) >= ASSET_FILTER_VERSION for cid, _, _ in contributors)

    def _purge(self, stats: dict[str, int], contributors: list[tuple[int, int, list[str]]]) -> None:
        """Delete what is no structure: own items found in asset lists, and finds nobody sights (orphans).

        Runs under ``_main_lock``, the lock a Resolve unknown job holds for its whole run: with a job running nothing is
        deleted (the own-item IDs are kept for the next run), so a row is never deleted while it is being asked.
        Rows read as structures (status ok) are never purged; one that is a character's own item is warned about.
        """
        if self._main_lock.locked():
            log.info("structure scout: a resolve job is running; the purge waits for the next run")
            return
        orphans_wanted = self._filters_current(contributors)
        if not self._own_hits and not orphans_wanted:
            return
        with self.db.tx() as c:  # one transaction; the lock is free and nothing awaits in between
            own = 0
            hits = sorted(self._own_hits)
            for start in range(0, len(hits), 900):
                chunk = hits[start : start + 900]
                marks = ",".join("?" * len(chunk))
                rows = c.execute(
                    f"SELECT structure_id, status FROM known_structures WHERE structure_id IN ({marks})",  # noqa: S608
                    tuple(chunk),
                ).fetchall()
                for r in rows:
                    if r[1] == "ok":
                        log.warning("structure %s was read from ESI but is one of a character's own items: kept", r[0])
                drop = [r[0] for r in rows if r[1] != "ok"]
                marks = ",".join("?" * len(drop))
                if drop:
                    for table in ("structure_sightings", "structure_refusals"):
                        c.execute(f"DELETE FROM {table} WHERE structure_id IN ({marks})", tuple(drop))  # noqa: S608
                    known = f"DELETE FROM known_structures WHERE structure_id IN ({marks})"  # noqa: S608
                    own += c.execute(known, tuple(drop)).rowcount
            orphans = 0
            if orphans_wanted:
                orphan = (
                    "SELECT structure_id FROM known_structures k WHERE status IN ('new','no_access') "
                    "AND destroyed_at IS NULL "
                    "AND NOT EXISTS (SELECT 1 FROM structure_sightings s WHERE s.structure_id=k.structure_id) "
                    "AND NOT EXISTS (SELECT 1 FROM structure_access a WHERE a.structure_id=k.structure_id) "
                    "AND NOT EXISTS (SELECT 1 FROM public_structure_history h WHERE h.structure_id=k.structure_id)"
                )
                c.execute(f"DELETE FROM structure_refusals WHERE structure_id IN ({orphan})")  # noqa: S608
                orphans = c.execute(f"DELETE FROM known_structures WHERE structure_id IN ({orphan})").rowcount  # noqa: S608
        self._own_hits.clear()
        stats["purged_own_items"] += own
        stats["purged_orphans"] += orphans
        if own or orphans:
            self._bump_meta("scout_purged_own_items", own)
            self._bump_meta("scout_purged_orphans", orphans)
            log.info("structure scout: purged %s own items and %s orphans that are no structures", own, orphans)
            self.version += 1  # the page and the caches drop them
            self._notify()

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

    # -- which character to ask, and what ESI said -------------------------------------------------
    def _load_pool(self) -> _Pool:
        readers = {c: o for c, o, _ in self.contributors(READ_STRUCTURES)}
        sightings: dict[int, list[int]] = {}
        for r in self.db.query("SELECT structure_id, character_id FROM structure_sightings ORDER BY character_id"):
            sightings.setdefault(r[0], []).append(r[1])
        since = time.time() - self.s.no_access_refresh_hours * 3600
        asked: dict[int, set[int]] = {}
        refused: dict[int, set[int]] = {}
        for r in self.db.query("SELECT structure_id, character_id, refused_at FROM structure_refusals"):
            asked.setdefault(r[0], set()).add(r[1])
            if r[2] >= since:
                refused.setdefault(r[0], set()).add(r[1])
        access: dict[int, list[int]] = {}
        for r in self.db.query("SELECT structure_id, character_id FROM structure_access ORDER BY character_id"):
            access.setdefault(r[0], []).append(r[1])
        return _Pool(readers, sightings, refused, asked, access=access)

    @staticmethod
    def _candidates(
        sid: int, pool: _Pool, *, main_id: int | None = None, unidentified: bool = False, public: bool = False
    ) -> list[int]:
        """The characters to ask about one structure, best first; refused pairs and dead tokens left out.

        Characters that may dock there (a dock-search hit, known to be allowed) come first, then sighters (characters
        with assets there); all of them must hold the structures scope. With ``main_id`` (the button) those linked to
        that main go before the others within each group and the main itself comes last. Without it (the hourly step)
        an ``unidentified`` structure is also offered to every other reader. A ``public`` structure (ESI lists it as
        open to everyone) ignores earlier refusals and falls back to any reader: its answer does not depend on who asks.
        """
        docking = [c for c in pool.access.get(sid, ()) if c in pool.readers]
        seen = docking + [c for c in pool.sightings.get(sid, ()) if c in pool.readers and c not in docking]
        if main_id is not None:

            def mine_first(group: list[int]) -> list[int]:
                mine = [c for c in group if pool.readers[c] == main_id]
                return mine + [c for c in group if c not in mine]

            order = mine_first(docking) + mine_first(seen[len(docking) :])
            if main_id in pool.readers:
                order.append(main_id)
        else:
            order = list(seen)
            if unidentified:
                order += [c for c in pool.readers if c not in seen]
        if public:
            order += list(pool.readers)
        skip = pool.dead | (set() if public else pool.refused.get(sid, set()))
        return [c for c in dict.fromkeys(order) if c not in skip]

    def _record_refusal(self, sid: int, cid: int, pool: _Pool) -> None:
        now = time.time()
        self.db.execute(
            "INSERT INTO structure_refusals(structure_id, character_id, refused_at) VALUES(?,?,?) "
            "ON CONFLICT(structure_id, character_id) DO UPDATE SET refused_at=excluded.refused_at",
            (sid, cid, now),
        )
        pool.refused.setdefault(sid, set()).add(cid)
        pool.asked.setdefault(sid, set()).add(cid)

    def _forget_refusals(self, sid: int, pool: _Pool) -> None:
        """A 200 or a 404 settles the structure: what refused it before no longer matters."""
        self.db.execute("DELETE FROM structure_refusals WHERE structure_id=?", (sid,))
        pool.refused.pop(sid, None)
        pool.asked.pop(sid, None)

    async def _ask(self, sid: int, order: list[int], pool: _Pool) -> _Read:
        """Ask ESI about one structure with each character of ``order`` in turn until one answers.

        403 records the pair as refused and tries the next character; 404 means the structure is gone; a token that is
        not accepted (SsoError, 401) skips that character for the rest of the pass. Nothing is sent while the error
        window is low (``blocked``). Any other failure is passing (``error``): nothing is marked checked for it.
        """
        refused = 0
        for cid in order:
            if self.esi.blocked_for(reserve=SCOUT_ERROR_RESERVE) > 0:
                return _Read("blocked")
            try:
                token = await self.chars.access_token(cid, pool.readers[cid])
                body = await self.esi.get(f"/universe/structures/{sid}", token)
            except SsoError:
                pool.dead.add(cid)
                continue
            except EsiError as exc:
                if exc.status == 404:
                    self._forget_refusals(sid, pool)
                    return _Read("gone")
                if exc.status == 403:
                    self._record_refusal(sid, cid, pool)
                    refused += 1
                    continue
                if exc.status == 401:
                    pool.dead.add(cid)
                    continue
                return _Read("error")
            self._forget_refusals(sid, pool)
            return _Read("ok", body) if isinstance(body, dict) and body else _Read("skipped")
        return _Read("no_access" if refused else "skipped")

    def _mark(self, sid: int, status: str) -> None:
        self.db.execute(
            "UPDATE known_structures SET status=?, checked_at=? WHERE structure_id=?", (status, time.time(), sid)
        )

    # -- Resolve unknown: a background job with the signed-in main ----------------------------------
    def _check_resolver(self, main_id: int) -> None:
        if self.settings.demo:
            raise ResolveError("demo", "resolving structures is not available in demo mode")
        row = self.db.get_character(main_id, main_id)
        if row is None or row.demo:
            raise ResolveError("no_character", "the signed-in main character is not registered on this site")
        if row.status == REAUTH:
            raise ResolveError("reauth", "the main character must sign in again (Add character) to read structures")
        if READ_STRUCTURES not in row.scopes:
            raise ResolveError("no_scope", f"the main character has not granted {READ_STRUCTURES}: Add character again")
        if self._job is not None and self._job.running:
            raise ResolveError("busy", "a resolution run is already in progress")

    def start_resolve(self, main_id: int) -> dict[str, Any]:
        """Start the job in the background (one at a time) and return its state; raises ResolveError."""
        self._check_resolver(main_id)
        job = self._job = ResolveJob(started_by=main_id)
        self._job_task = asyncio.get_running_loop().create_task(self._run_job(job, main_id))
        return job.as_dict()

    async def run_resolve(self, main_id: int) -> dict[str, Any]:
        """The same job, run to completion in the caller (tests, scripts); returns its final state."""
        self._check_resolver(main_id)
        job = self._job = ResolveJob(started_by=main_id)
        await self._run_job(job, main_id)
        return job.as_dict()

    def resolve_state(self) -> dict[str, Any]:
        if self._job is not None:
            return self._job.as_dict()
        idle = ResolveJob(started_by=0, started_at=0.0, running=False).as_dict()
        return idle | {"started_at": None, "started_by": None}

    def stop_resolve(self) -> dict[str, Any]:
        """Cancel the running job (it ends at its next step, a pause is cut short); returns its state."""
        if self._job is not None and self._job.running:
            self._job.cancel.set()
        return self.resolve_state()

    @staticmethod
    async def _cancellable_sleep(seconds: float, cancel: asyncio.Event) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(cancel.wait(), timeout=seconds)

    async def _run_job(self, job: ResolveJob, main_id: int) -> None:
        try:
            async with self._main_lock:
                await self._work(job, main_id)
        except asyncio.CancelledError:  # the task was cancelled hard (shutdown did not wait for the job)
            job.stopped = "cancelled"
            raise
        except Exception as exc:
            log.exception("structure resolver failed")
            job.stopped, job.failure = "failed", redact(repr(exc))[:200]
        finally:
            job.running = False
            job.finished_at = time.time()
            job.paused_until = None
            self.version += 1
            log.info("structure resolver (main %s): %s", main_id, job.as_dict())

    async def _publish(self, job: ResolveJob) -> None:
        """Let the page and the Wars page see what the job found so far."""
        job.published = job.resolved
        await self._names()
        self.version += 1
        self._notify()

    async def _wait_window(self, job: ResolveJob) -> bool:
        """Sleep until ESI's error window has room again; False when the job was cancelled meanwhile."""
        shown = False
        while True:
            wait = self.esi.blocked_for(reserve=SCOUT_ERROR_RESERVE)
            if wait <= 0:
                return not job.cancel.is_set()
            if job.cancel.is_set():
                return False
            job.paused_until = time.time() + wait
            if not shown and job.resolved > job.published:  # a pause is a good moment to show the progress
                shown = True
                await self._publish(job)
            await self._sleep(wait + 1.0, job.cancel)
            job.paused_until = None

    async def _work(self, job: ResolveJob, main_id: int) -> None:
        # public datasets first, so the page shows the historical matches at once; forced at most every 10 minutes
        force = time.monotonic() - self._history_forced > 600
        if force:
            self._history_forced = time.monotonic()
        job.history = await self._refresh_directory(force=force)
        if any(job.history.values()):
            await self._publish(job)
        try:
            listed = await self.esi.get("/universe/structures/")
            public = {int(i) for i in listed} if isinstance(listed, list) else set()
        except EsiError as exc:
            log.warning("structure resolver: public structure list unavailable (%s); going one by one", exc)
            public = set()
        pool = self._load_pool()
        for row in self.db.list_characters(main_id):  # the main's own links: their tokens are used as this main
            if not row.demo and row.status != REAUTH and READ_STRUCTURES in row.scopes:
                pool.readers[row.character_id] = main_id
        rows = self.db.query(
            f"SELECT structure_id FROM known_structures WHERE {UNKNOWN} ORDER BY checked_at, structure_id"  # noqa: S608
        )

        def tier(sid: int) -> int:  # public first, then a sighter never refused for it, then the rest
            if sid in public:
                return 0
            asked = pool.asked.get(sid, ())
            return 1 if any(c in pool.readers and c not in asked for c in pool.sightings.get(sid, ())) else 2

        targets = sorted((r[0] for r in rows), key=tier)  # stable: least recently checked first within a tier
        job.targets = len(targets)
        job.public_first = sum(1 for sid in targets if sid in public)
        failures = 0
        for sid in targets:
            result = _Read("skipped")
            while True:
                if not await self._wait_window(job):
                    break
                order = self._candidates(sid, pool, main_id=main_id, public=sid in public)
                if not order:
                    break  # nobody left to ask: not sent to ESI
                result = await self._ask(sid, order, pool)
                if result.kind != "blocked":
                    break  # blocked in the middle of the list: wait for the window, then ask the rest
            if job.cancel.is_set():
                job.stopped = "cancelled"
                break
            if result.kind == "error":
                job.errors += 1
                failures += 1
                if failures >= JOB_MAX_FAILURES:
                    job.stopped = "esi_error"
                    break
                continue
            failures = 0
            if result.kind == "ok" and result.body:
                self._save_read(sid, result.body, time.time())
                job.resolved += 1
            elif result.kind == "gone":
                self._mark(sid, "gone")
                job.gone += 1
            elif result.kind == "no_access":
                self._mark(sid, "no_access")
                job.no_access += 1
            else:
                job.skipped += 1
            job.done += 1
            if job.resolved - job.published >= JOB_PUBLISH_EVERY:
                await self._publish(job)
            if pool.readers and pool.dead >= set(pool.readers):
                job.stopped = "reauth"  # no character with a token ESI accepts is left
                break
        else:
            job.stopped = "done"
        if job.stopped != "cancelled":  # structures ESI refused are matched again against the public datasets
            again = await self._refresh_directory()
            job.history = {k: max(v, again.get(k, 0)) for k, v in job.history.items()} or again
        await self._publish(job)

    # -- the hourly step ------------------------------------------------------------------------------
    async def _resolve(self, stats: dict[str, int]) -> None:
        if self._main_lock.locked():  # a Resolve unknown job is reading: no second reader, no shared budget fights
            log.info("structure scout: a resolve job is running; unresolved structures wait for the next run")
            return
        async with self._main_lock:
            await self._resolve_locked(stats)

    async def _resolve_locked(self, stats: dict[str, int]) -> None:
        pool = self._load_pool()
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
            order = self._candidates(sid, pool, unidentified=unidentified)
            if not order:
                # nobody who may read it right now: keep what we know and try again later, behind the rows not yet asked
                self.db.execute("UPDATE known_structures SET checked_at=? WHERE structure_id=?", (time.time(), sid))
                return
            async with sem:
                result = await self._ask(sid, order, pool)
            if result.kind == "blocked":
                return  # leave the rest of ESI's error budget to the character refresh; retry next run
            if result.kind == "error":
                stats["errors"] += 1
                return  # ESI trouble: retry next run
            now = time.time()
            if result.kind == "ok" and result.body:
                self._save_read(sid, result.body, now)
                stats["resolved"] += 1
            elif result.kind == "skipped":  # every token was refused by SSO: as if nobody could be asked
                self.db.execute("UPDATE known_structures SET checked_at=? WHERE structure_id=?", (now, sid))
            else:
                self._mark(sid, result.kind)
                stats["no_access" if result.kind == "no_access" else "gone"] += 1

        await asyncio.gather(*(resolve(r["structure_id"], not r["name"] or not r["owner_id"]) for r in todo))

    # -- numbers for the operator ---------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """What the resolver has to work with, from the database alone (no ESI call, no token)."""
        since = time.time() - self.s.no_access_refresh_hours * 3600
        readers = tuple(c for c, _, _ in self.contributors(READ_STRUCTURES))  # holders of the structures scope
        marks = ",".join("?" * len(readers))
        refusals = "structure_refusals"
        if not self.db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='structure_refusals'"):
            # a database that was not migrated yet (the read-only CLI): no refusals recorded
            refusals = "(SELECT 0 AS structure_id, 0 AS character_id, 0.0 AS refused_at WHERE 0)"
        q = self.db.query
        unknown = f"SELECT structure_id FROM known_structures WHERE {UNKNOWN}"  # noqa: S608
        sighters = (  # sighters holding the scope, per structure
            f"(SELECT COUNT(*) FROM structure_sightings s WHERE s.structure_id=k.structure_id "  # noqa: S608
            f"AND s.character_id IN ({marks}))"
        )
        split = {"0": 0, "1": 0, "2+": 0}
        for n, count in q(  # noqa: S608
            f"SELECT MIN(c, 2), COUNT(*) FROM (SELECT {sighters} AS c FROM known_structures k WHERE {UNKNOWN}) "  # noqa: S608
            "GROUP BY MIN(c, 2)",
            readers,
        ):
            split["2+" if n >= 2 else str(n)] = count
        askable = q(  # a reader without a recent refusal for the structure exists
            f"SELECT COUNT(*) FROM known_structures k WHERE {UNKNOWN} AND ? > (SELECT COUNT(*) FROM {refusals} r "  # noqa: S608
            f"WHERE r.structure_id=k.structure_id AND r.refused_at >= ? AND r.character_id IN ({marks}))",
            (len(readers), since, *readers),
        )[0][0]
        askable_sighters = q(  # a sighter holding the scope without a recent refusal for the structure exists
            f"SELECT COUNT(*) FROM known_structures k WHERE {UNKNOWN} AND EXISTS (SELECT 1 FROM structure_sightings s "  # noqa: S608
            f"WHERE s.structure_id=k.structure_id AND s.character_id IN ({marks}) AND NOT EXISTS "
            f"(SELECT 1 FROM {refusals} r WHERE r.structure_id=s.structure_id AND r.character_id=s.character_id "
            "AND r.refused_at >= ?))",
            (*readers, since),
        )[0][0]
        total = q(f"SELECT COUNT(*) FROM ({unknown})")[0][0]  # noqa: S608
        columns = {r[1] for r in q("PRAGMA table_info(scout_characters)")}
        if "filter_v" in columns:  # a database not migrated yet (the read-only CLI) has none
            have = {r[0]: r[1] for r in q("SELECT character_id, filter_v FROM scout_characters")}
        else:
            have = {}
        pending = sum(1 for cid, _, _ in self.contributors() if have.get(cid, 1) < ASSET_FILTER_VERSION)
        matches = {"EVE Ref": 0, "Local index": 0, "Adam4EVE": 0}
        for source, count in q(
            "SELECT COALESCE(source,'Adam4EVE'), COUNT(*) FROM public_structure_history "  # noqa: S608
            f"WHERE structure_id IN ({unknown}) GROUP BY COALESCE(source,'Adam4EVE')"
        ):
            matches[source] = count
        return {
            "dock_search": self._dock_stats(unknown),
            "by_status": {r[0]: r[1] for r in q("SELECT status, COUNT(*) FROM known_structures GROUP BY status")},
            "readers": len(readers),
            "unknown": total,
            "unknown_by_sighters": split,
            "askable": askable,  # unknown structures some reader may still be asked about (the hourly rule)
            "not_askable": total - askable,
            "askable_via_sighters": askable_sighters,  # ... of which a sighter can be asked (the button's first choice)
            "refusal_pairs": q(f"SELECT COUNT(*) FROM {refusals}")[0][0],  # noqa: S608
            "refusal_pairs_recent": q(f"SELECT COUNT(*) FROM {refusals} WHERE refused_at >= ?", (since,))[0][0],  # noqa: S608
            "public_history_matches": matches,
            "filter_version": ASSET_FILTER_VERSION,
            "filter_pending": pending,  # characters with the assets scope not yet read with the current filter
            "purged_own_items": int(self.db.get_meta("scout_purged_own_items", "0") or 0),  # since the filter changed
            "purged_orphans": int(self.db.get_meta("scout_purged_orphans", "0") or 0),
        }

    def _dock_stats(self, unknown: str) -> dict[str, Any]:
        """The ``dock_search`` block of ``stats()``: what the character search found, from the database alone."""
        q = self.db.query
        have = {r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
        out: dict[str, Any] = {
            "enabled": self.s.dock_search_enabled,
            "term": self.s.dock_search_term,
            "cap": self.s.dock_search_cap,
            "eligible_characters": len(self.dock_searchers()),  # both scopes
            "characters_searched": 0,
            "capped": 0,
            "with_errors": 0,
            "access_rows": 0,
            "dockable": 0,
            "dockable_without_assets": 0,
            "dockable_unresolved": 0,
        }
        if "scout_dock_search" in have:
            row = q(
                "SELECT COALESCE(SUM(checked_at > 0), 0), COALESCE(SUM(capped), 0), "
                "COALESCE(SUM(error IS NOT NULL), 0) FROM scout_dock_search"
            )[0]
            out["characters_searched"], out["capped"], out["with_errors"] = row[0], row[1], row[2]
        if "structure_access" in have:
            out["access_rows"] = q("SELECT COUNT(*) FROM structure_access")[0][0]
            out["dockable"] = q("SELECT COUNT(DISTINCT structure_id) FROM structure_access")[0][0]
            out["dockable_without_assets"] = q(
                "SELECT COUNT(DISTINCT structure_id) FROM structure_access WHERE structure_id NOT IN "
                "(SELECT structure_id FROM structure_sightings)"
            )[0][0]
            out["dockable_unresolved"] = q(
                f"SELECT COUNT(DISTINCT structure_id) FROM structure_access WHERE structure_id IN ({unknown})"  # noqa: S608
            )[0][0]
        return out

    # historical details: public datasets, kept apart from live ESI records (EVE Ref > local index > Adam4EVE)
    HISTORY_PRIORITY = {"EVE Ref": 3, "Local index": 2, "Adam4EVE": 1}

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
                "WHERE ? >= CASE COALESCE(source,'Adam4EVE') "
                "WHEN 'EVE Ref' THEN 3 WHEN 'Local index' THEN 2 ELSE 1 END",
                [(*r, self.HISTORY_PRIORITY.get(source, 1)) for r in rows],
            )

    async def _refresh_directory(self, force: bool = False) -> dict[str, int]:
        """Match the public datasets locally (EVE Ref, then Adam4EVE); returns the matches stored per source."""
        async with self._history_lock:  # the hourly step and the job never download at the same time
            return await self._refresh_directory_locked(force)

    async def _refresh_directory_locked(self, force: bool) -> dict[str, int]:
        found = {"EVE Ref": 0, "Local index": 0, "Adam4EVE": 0}
        if self.settings.demo:
            return found
        wanted = self._history_wanted()
        if not wanted:
            return found
        try:  # the local flat list costs no network: asked on every run
            rows = self._fetch_local(wanted)
        except (OSError, ValueError) as exc:
            log.warning("local structure index unreadable: %s", exc)
        else:
            self._store_history(rows, "Local index")
            found["Local index"] = len(rows)
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

    def _fetch_local(self, wanted: set[int]) -> list[tuple[Any, ...]]:
        """Matches in ``data/structure-index.jsonl`` (scripts/build_structure_index.py); nothing without the file."""
        rows: list[tuple[Any, ...]] = []
        for rec in lookup_index(index_path(self.settings.data_dir, self.s.index_file), wanted):
            if not (rec.get("name") or rec.get("owner_id") or rec.get("solar_system_id")):
                continue
            rows.append(
                (
                    int(rec["structure_id"]),
                    rec.get("name"),
                    rec.get("owner_id"),
                    rec.get("owner_name"),
                    rec.get("solar_system_id"),
                    rec.get("type_id"),
                    rec.get("type_name"),
                    rec.get("observed"),
                )
            )
        return rows

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
            "(SELECT COUNT(*) FROM structure_access a WHERE a.structure_id=k.structure_id) AS dock_by, "
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
                    "assets": r["seen_by"] > 0,  # a registered character has assets there
                    "dock_by": r["dock_by"],  # characters that may dock there: a count, never who
                    "dockable": r["dock_by"] > 0,
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
            "SELECT k.*, (SELECT COUNT(*) FROM structure_sightings s WHERE s.structure_id=k.structure_id) AS seen_by, "
            "(SELECT COUNT(*) FROM structure_access a WHERE a.structure_id=k.structure_id) AS dock_by "
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
                "assets": r["seen_by"] > 0,
                "dock_by": r["dock_by"],
                "dockable": r["dock_by"] > 0,
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
        source: str = "assets",
        sort: str = "jumps",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """``source``: assets (default, a character has assets there), dock (may be docked at, no assets) or all."""
        everything = self.structures() + self.unresolved()
        by_source = {
            "assets": sum(1 for r in everything if r["assets"]),
            "dock": sum(1 for r in everything if r["dockable"] and not r["assets"]),
            "all": len(everything),
        }
        if source == "all":
            base = everything
        elif source == "dock":
            base = [r for r in everything if r["dockable"] and not r["assets"]]
        else:
            source = "assets"
            base = [r for r in everything if r["assets"]]
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
            "source": source,
            "sources": by_source,  # structures per source filter, before the other filters
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
        job_task = self._job_task
        if self._job is not None:
            self._job.cancel.set()  # the Resolve unknown job ends at its next step; a pause is cut short
        if job_task is not None and not job_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(job_task), timeout=10)
            except TimeoutError:
                job_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await job_task
        self._job_task = None
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
