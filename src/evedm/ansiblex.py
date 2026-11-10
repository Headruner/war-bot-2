"""Ansiblex jump gates for the Ranking page (UC-16).

ESI has no list of jump gates and no endpoint that says who may use one. What it does offer is the character
structure search: ``GET /characters/{id}/search/?categories=structure`` (scope ``esi-search.search_structures.v1``)
returns only the structures that character has access to. Ansiblex gates are named "A » B - name" by convention, so
searching for " » " finds the gates a character can see; each hit is read once with ``/universe/structures/{id}``
(scope ``esi-universe.read_structures.v1``) for its system, type (35841) and the destination in its name. A gate a
character can see is treated as a gate it may use - ESI does not expose the jump access list itself.

Per character the service keeps the one-way links "gate system -> destination system"; ranking then routes over
stargates plus those links, each link costing one jump, so a route differs per character.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from .characters import REAUTH, CharacterService
from .config import ANSIBLEX_SCOPE, STRUCTURE_SCOPES, AnsiblexSettings
from .db import CharacterRow, Database
from .esi import EsiClient, EsiError
from .sso import SsoError
from .universe import GateGraph

log = logging.getLogger("evedm.ansiblex")

ANSIBLEX_TYPE_ID = 35841
SEARCH_TERM = " » "  # three characters: ESI's search needs at least three
ERROR_RESERVE = 40  # stop reading when ESI has this few errors left in its window (the scout stops at 60)
_NAME = re.compile(r"^\s*(?P<src>.+?)\s*»\s*(?P<dst>.+?)(?:\s+-\s+.*)?\s*$")


def parse_gate_name(name: str | None) -> tuple[str, str] | None:
    """("Jita", "Perimeter") from "Jita » Perimeter - Gate 1", or None when the name does not follow the pattern."""
    m = _NAME.match(name or "")
    return (m.group("src").strip(), m.group("dst").strip()) if m else None


class AnsiblexService:
    def __init__(self, settings: AnsiblexSettings, db: Database, graph: GateGraph, chars: CharacterService,
                 esi: EsiClient | None, *, demo: bool = False):
        self.s = settings
        self.db = db
        self.g = graph
        self.chars = chars
        self.esi = esi
        self.demo = demo
        self.version = 0
        self.state: dict[str, Any] = {"running": False, "last_error": None, "last_run": None}
        self._links: dict[int, tuple[int, dict[int, list[int]]]] = {}  # character -> (version, links)
        self._task: asyncio.Task[None] | None = None

    # -- what a character may use ------------------------------------------------------------------------
    @staticmethod
    def can_search(character: CharacterRow) -> bool:
        """The character granted both scopes the search needs."""
        return ANSIBLEX_SCOPE in character.scopes and STRUCTURE_SCOPES[0] in character.scopes

    def links_for(self, character_id: int) -> dict[int, list[int]]:
        """system index -> destination system indexes of the gates this character can see."""
        cached = self._links.get(character_id)
        if cached is not None and cached[0] == self.version:
            return cached[1]
        rows = self.db.query(
            "SELECT g.solar_system_id AS src, g.dest_system_id AS dst FROM ansiblex_access a "
            "JOIN ansiblex_gates g ON g.structure_id=a.structure_id "
            "WHERE a.character_id=? AND g.status='ok' AND g.solar_system_id IS NOT NULL "
            "AND g.dest_system_id IS NOT NULL",
            (character_id,),
        )
        links: dict[int, list[int]] = {}
        for r in rows:
            a, b = self.g.index.get(r["src"]), self.g.index.get(r["dst"])
            if a is not None and b is not None and a != b and b not in links.setdefault(a, []):
                links[a].append(b)
        self._links[character_id] = (self.version, links)
        return links

    def status_for(self, character: CharacterRow, checks: dict[int, Any]) -> str:
        """no_scope | pending (not read yet) | error | ok."""
        if character.demo or self.demo:
            return "ok"
        if not self.can_search(character):
            return "no_scope"
        row = checks.get(character.character_id)
        if row is None or not row["checked_at"]:
            return "pending"
        return "error" if row["error"] else "ok"

    def checks(self) -> dict[int, Any]:
        return {r["character_id"]: r for r in self.db.query("SELECT * FROM ansiblex_characters")}

    def gate_count(self) -> int:
        return self.db.query("SELECT COUNT(*) FROM ansiblex_gates WHERE status='ok'")[0][0]

    # -- reading from ESI ---------------------------------------------------------------------------------
    def stale(self, characters: Iterable[CharacterRow]) -> list[CharacterRow]:
        """Characters that can search and whose gate list is missing or older than ``refresh_hours``."""
        checks = self.checks()
        limit = time.time() - self.s.refresh_hours * 3600
        out = []
        for c in characters:
            if c.demo or c.status == REAUTH or not self.can_search(c):
                continue
            row = checks.get(c.character_id)
            if row is None or row["checked_at"] < limit:
                out.append(c)
        return out

    async def ensure(self, characters: list[CharacterRow], wait_s: float = 0.0) -> None:
        """Start reading the gates of stale characters in the background (one round at a time).

        ``wait_s`` > 0 waits that long for the round, so the first use of the mode shows gates at once; later
        rounds finish unobserved and the next ranking picks them up.
        """
        if self.demo or self.esi is None:
            return
        todo = self.stale(characters)
        if todo and (self._task is None or self._task.done()):
            self._task = asyncio.ensure_future(self._run(todo))
        if wait_s > 0 and self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=wait_s)
            except TimeoutError:
                pass

    async def _run(self, characters: list[CharacterRow]) -> None:
        self.state["running"] = True
        sem = asyncio.Semaphore(max(1, self.s.concurrency))
        try:
            async def one(c: CharacterRow) -> None:
                async with sem:
                    await self._read_character(c)

            await asyncio.gather(*(one(c) for c in characters), return_exceptions=True)
            self.state["last_run"] = time.time()
        finally:
            self.state["running"] = False
            self.version += 1  # the links of every character may have changed

    def _note(self, character_id: int, gates: int, error: str | None) -> None:
        self.db.execute(
            "INSERT INTO ansiblex_characters(character_id,checked_at,gates,error) VALUES(?,?,?,?) "
            "ON CONFLICT(character_id) DO UPDATE SET checked_at=excluded.checked_at, gates=excluded.gates, "
            "error=excluded.error",
            (character_id, time.time(), gates, error),
        )

    async def _read_character(self, c: CharacterRow) -> None:
        assert self.esi is not None
        cid = c.character_id
        try:
            token = await self.chars.access_token(cid, c.owner_id)
            found = await self.esi.get(
                f"/characters/{cid}/search/?categories=structure&search={quote(SEARCH_TERM)}&strict=false", token
            )
            ids = [int(i) for i in (found or {}).get("structure", [])]
            visible: list[int] = []
            for sid in ids:
                if await self._gate(sid, token):
                    visible.append(sid)
        except (EsiError, SsoError) as exc:
            self.state["last_error"] = repr(exc)[:200]
            self._note(cid, 0, repr(exc)[:200])
            return
        now = time.time()
        with self.db.tx() as conn:
            conn.execute("DELETE FROM ansiblex_access WHERE character_id=?", (cid,))
            conn.executemany(
                "INSERT OR REPLACE INTO ansiblex_access(character_id, structure_id, seen_at) VALUES(?,?,?)",
                [(cid, sid, now) for sid in visible],
            )
        self._note(cid, len(visible), None)
        log.info("character %s can see %s Ansiblex gates", cid, len(visible))

    async def _gate(self, structure_id: int, token: str) -> bool:
        """True when the structure is an Ansiblex gate with a known system and destination (read once, then cached)."""
        assert self.esi is not None
        row = self.db.query("SELECT * FROM ansiblex_gates WHERE structure_id=?", (structure_id,))
        fresh = time.time() - self.s.gate_refresh_hours * 3600
        if row and row[0]["checked_at"] > fresh:
            return row[0]["status"] == "ok"
        if self.esi.blocked_for(ERROR_RESERVE) > 0:
            raise EsiError(420, "ESI error limit: stopped early")
        try:
            info = await self.esi.get(f"/universe/structures/{structure_id}", token)
        except EsiError as exc:
            if exc.status in (403, 404):
                self._save_gate(structure_id, None, "no_access")
                return False
            raise
        if int(info.get("type_id") or 0) != ANSIBLEX_TYPE_ID:
            self._save_gate(structure_id, info, "not_gate")
            return False
        pair = parse_gate_name(info.get("name"))
        dest = self.g.find(pair[1]) if pair else None
        self._save_gate(structure_id, info, "ok" if dest is not None else "not_gate", dest)
        return dest is not None

    def _save_gate(self, structure_id: int, info: dict[str, Any] | None, status: str, dest: int | None = None) -> None:
        info = info or {}
        self.db.execute(
            "INSERT INTO ansiblex_gates(structure_id,name,owner_id,solar_system_id,dest_system_id,checked_at,status) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(structure_id) DO UPDATE SET name=excluded.name, "
            "owner_id=excluded.owner_id, solar_system_id=excluded.solar_system_id, "
            "dest_system_id=excluded.dest_system_id, checked_at=excluded.checked_at, status=excluded.status",
            (
                structure_id,
                info.get("name"),
                info.get("owner_id"),
                info.get("solar_system_id"),
                self.g.ids[dest] if dest is not None else None,
                time.time(),
                status,
            ),
        )

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()


# --------------------------------------------------------------------------- demo
DEMO_GATES = [("Jita", "Amarr"), ("Amarr", "Jita"), ("Perimeter", "Hek"), ("Hek", "Perimeter")]


def seed_demo_gates(db: Database, graph: GateGraph) -> int:
    """Sample gates for demo mode: every demo character may use the same few (no ESI is involved)."""
    if db.query("SELECT 1 FROM ansiblex_gates LIMIT 1"):
        return 0
    chars = [r["character_id"] for r in db.query("SELECT DISTINCT character_id FROM characters WHERE demo=1")]
    now = time.time()
    n = 0
    with db.tx() as c:
        for k, (src, dst) in enumerate(DEMO_GATES):
            a, b = graph.find(src), graph.find(dst)
            if a is None or b is None:
                continue
            sid = 1_029_000_000_000 + k
            c.execute(
                "INSERT OR IGNORE INTO ansiblex_gates(structure_id,name,owner_id,solar_system_id,dest_system_id,"
                "checked_at,status) VALUES(?,?,?,?,?,?, 'ok')",
                (sid, f"{src} » {dst} - Demo gate", 98300001, graph.ids[a], graph.ids[b], now),
            )
            c.executemany(
                "INSERT OR IGNORE INTO ansiblex_access(character_id, structure_id, seen_at) VALUES(?,?,?)",
                [(cid, sid, now) for cid in chars],
            )
            n += 1
    return n
