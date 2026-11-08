"""War census: every active war in New Eden, for the Statistics tab and the high-sec structures list.

The new-wars feed (service.py) only follows wars declared from now on. The census also walks back through the newest
war IDs (paged ``/wars/?max_war_id=``), because active wars can be old: mutual wars run for months. A war is active
while ``finished`` is empty or in the future. Active wars are re-read every ``census_refresh_hours``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from typing import Any

from ..db import Database
from ..universe import GateGraph
from .config import WarsSettings
from .esi import WarEsi, WarEsiError
from .structures import HIGHSEC_MIN, StructureIndex

log = logging.getLogger("evedm.wars.census")

ACTIVE = "(finished IS NULL OR finished > ?)"
TOMBSTONE = "1970-01-01T00:00:00Z"  # finished time stored for IDs ESI does not know: known, never active
ALLIANCE_CORPS_TTL_S = 86400
NAMES_COOLDOWN_S = 60  # after a failed on-demand name lookup, pages are served without asking ESI again
STATES = ("fighting", "pending", "ending")  # order of interest for the structure list


def iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


ZKILL = "https://zkillboard.com"


def zkill_war(war_id: int) -> str:
    return f"{ZKILL}/war/{int(war_id)}/"


def zkill_entity(entity_id: int | None, kind: str | None) -> str | None:
    if not entity_id:
        return None
    return f"{ZKILL}/{'alliance' if kind == 'alliance' else 'corporation'}/{int(entity_id)}/"


def label(names: dict[int, str], entity_id: int | None, kind: str | None) -> str:
    """The resolved name, or a readable stand-in until ESI has answered (never a bare number)."""
    if not entity_id:
        return "unknown"
    if entity_id in names:
        return names[entity_id]
    return f"{(kind or 'entity').replace('_', ' ').capitalize()} {entity_id}"


def war_state(started: str | None, finished: str | None, now_iso: str) -> str:
    """pending: declared, fighting not started yet; ending: an end time is set; fighting: otherwise."""
    if started and started > now_iso:
        return "pending"
    if finished:
        return "ending"
    return "fighting"


def _side(raw: dict) -> tuple[int | None, str]:
    if raw.get("alliance_id"):
        return int(raw["alliance_id"]), "alliance"
    cid = raw.get("corporation_id")
    return (int(cid) if cid else None), "corporation"


class WarCensus:
    def __init__(
        self, ws: WarsSettings, db: Database, graph: GateGraph, esi: WarEsi, structures: StructureIndex | None = None
    ):
        self.s = ws
        self.db = db
        self.g = graph
        self.esi = esi
        self.structures = structures
        self.version = 0  # bumped whenever stored census data changes (cache key)
        self._loot: tuple[tuple[int, float], list[dict[str, Any]]] | None = None
        self._stats: tuple[tuple[int, float, int], dict[str, Any]] | None = None
        self.last_run: dict[str, Any] = {}
        self._names_lock = asyncio.Lock()
        self._names_failed_at = 0.0

    # -- meta ---------------------------------------------------------------------------------
    def _meta(self, key: str, default: str | None = None) -> str | None:
        return self.db.get_meta(f"census.{key}", default)

    def _set_meta(self, key: str, value: Any) -> None:
        self.db.set_meta(f"census.{key}", str(value))

    # -- crawling -------------------------------------------------------------------------------
    def _known(self, ids: list[int]) -> set[int]:
        out: set[int] = set()
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]
            marks = ",".join("?" * len(chunk))
            sql = f"SELECT war_id FROM war_census WHERE war_id IN ({marks})"  # noqa: S608 - placeholders only
            out.update(r[0] for r in self.db.query(sql, tuple(chunk)))
        return out

    @staticmethod
    def _row(wid: int, d: dict | None, now: float) -> tuple[Any, ...]:
        if d is None:  # unknown to ESI: remember it, never active
            return (wid, None, None, TOMBSTONE, None, 0, 0, None, None, 0, 0.0, None, None, 0, 0.0, "[]", now)
        agg, dfn = d.get("aggressor", {}) or {}, d.get("defender", {}) or {}
        a_id, a_type = _side(agg)
        d_id, d_type = _side(dfn)
        allies = [list(_side(x)) for x in d.get("allies", []) or []]
        return (
            wid,
            d.get("declared"),
            d.get("started"),
            d.get("finished"),
            d.get("retracted"),
            int(bool(d.get("mutual"))),
            int(bool(d.get("open_for_allies"))),
            a_id,
            a_type,
            int(agg.get("ships_killed") or 0),
            float(agg.get("isk_destroyed") or 0),
            d_id,
            d_type,
            int(dfn.get("ships_killed") or 0),
            float(dfn.get("isk_destroyed") or 0),
            json.dumps(allies),
            now,
        )

    async def _fetch(self, ids: list[int]) -> int:
        """Read wars from ESI and store them; returns how many failed (left for a later cycle)."""
        if not ids:
            return 0
        sem = asyncio.Semaphore(self.s.census_concurrency)

        async def one(wid: int) -> tuple[int, dict | None | WarEsiError]:
            async with sem:
                try:
                    return wid, await self.esi.war(wid)
                except WarEsiError as e:
                    return wid, (None if e.not_found else e)

        results = await asyncio.gather(*(one(i) for i in ids))
        now = time.time()
        rows = [self._row(wid, d, now) for wid, d in results if not isinstance(d, WarEsiError)]
        with self.db.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO war_census(war_id,declared,started,finished,retracted,mutual,open_for_allies,"
                "aggressor_id,aggressor_type,aggressor_ships,aggressor_isk,defender_id,defender_type,defender_ships,"
                "defender_isk,allies,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        if rows:
            self.version += 1
        return len(results) - len(rows)

    async def run(self) -> dict[str, Any]:
        """One census step: new war IDs, part of the backwards scan, due refreshes, names, alliance members."""
        t0 = time.time()
        newest = sorted({int(i) for i in (await self.esi.wars() or [])}, reverse=True)
        stats = {"new": 0, "scanned_pages": 0, "refreshed": 0, "failed": 0}
        if newest:
            unknown = [i for i in newest if i not in self._known(newest)]
            stats["failed"] += await self._fetch(unknown)
            stats["new"] = len(unknown)
            await self.ensure_names()
            if self._meta("floor") is None:  # first run: the backwards scan starts below the newest page
                self._set_meta("floor", min(newest))
                self._set_meta("examined", len(newest))
        floor = int(self._meta("floor") or 0)
        examined = int(self._meta("examined") or 0)
        while (
            floor > 1
            and self._meta("complete") != "true"
            and examined < self.s.census_max_ids
            and stats["scanned_pages"] < self.s.census_pages_per_cycle
        ):
            page = [int(i) for i in (await self.esi.wars(max_war_id=floor) or []) if int(i) < floor]
            if not page:
                self._set_meta("complete", "true")
                break
            failed = await self._fetch([i for i in page if i not in self._known(page)])
            stats["failed"] += failed
            if failed:  # retry this page next cycle, so no war below it is skipped
                break
            floor, examined = min(page), examined + len(page)
            self._set_meta("floor", floor)
            self._set_meta("examined", examined)
            stats["scanned_pages"] += 1
            await self.ensure_names()  # names show up page by page during the first scan
        if examined >= self.s.census_max_ids:
            self._set_meta("complete", "true")
        due = [
            r[0]
            for r in self.db.query(
                f"SELECT war_id FROM war_census WHERE {ACTIVE} AND checked_at < ? ORDER BY checked_at LIMIT 2000",  # noqa: S608
                (iso(), time.time() - self.s.census_refresh_hours * 3600),
            )
        ]
        stats["failed"] += await self._fetch(due)
        stats["refreshed"] = len(due)
        await self._alliance_corps()
        await self._names()
        self.last_run = stats | {"at": time.time(), "duration_s": round(time.time() - t0, 1)}
        log.info("census: %s", self.last_run)
        return self.last_run

    def active_rows(self) -> list[Any]:
        return self.db.query(f"SELECT * FROM war_census WHERE {ACTIVE} ORDER BY war_id DESC", (iso(),))  # noqa: S608

    def _alliances_at_war(self) -> set[int]:
        out: set[int] = set()
        for r in self.active_rows():
            for eid, kind in ((r["aggressor_id"], r["aggressor_type"]), (r["defender_id"], r["defender_type"])):
                if eid and kind == "alliance":
                    out.add(int(eid))
            out.update(int(a) for a, kind in json.loads(r["allies"]) if a and kind == "alliance")
        return out

    async def _alliance_corps(self) -> None:
        stored = {r[0]: r[1] for r in self.db.query("SELECT alliance_id, updated_at FROM alliance_corps")}
        due = [a for a in self._alliances_at_war() if time.time() - stored.get(a, 0) > ALLIANCE_CORPS_TTL_S]
        if not due:
            return
        sem = asyncio.Semaphore(self.s.census_concurrency)

        async def one(aid: int) -> tuple[int, list[int] | None]:
            async with sem:
                try:
                    return aid, [int(c) for c in await self.esi.alliance_corporations(aid) or []]
                except WarEsiError as e:
                    return aid, ([] if e.not_found else None)

        now = time.time()
        rows = [
            (aid, json.dumps(corps), now)
            for aid, corps in await asyncio.gather(*(one(a) for a in due))
            if corps is not None
        ]
        with self.db.tx() as c:
            c.executemany("INSERT OR REPLACE INTO alliance_corps(alliance_id, corps, updated_at) VALUES(?,?,?)", rows)
        if rows:
            self.version += 1

    def corps_at_war(self) -> dict[int, list[int]]:
        """corporation -> active war IDs it is part of (directly, through its alliance, or as an ally)."""
        alliance_corps = self.alliance_corps()
        out: dict[int, list[int]] = {}
        for r in self.active_rows():
            sides = [(r["aggressor_id"], r["aggressor_type"]), (r["defender_id"], r["defender_type"])]
            sides += [(a, kind) for a, kind in json.loads(r["allies"])]
            for eid, kind in sides:
                if not eid:
                    continue
                for corp in alliance_corps.get(int(eid), []) if kind == "alliance" else [int(eid)]:
                    wars = out.setdefault(int(corp), [])
                    if r["war_id"] not in wars:
                        wars.append(r["war_id"])
        return out

    def alliance_corps(self) -> dict[int, list[int]]:
        return {r[0]: json.loads(r[1]) for r in self.db.query("SELECT alliance_id, corps FROM alliance_corps")}

    def wanted_names(self) -> set[int]:
        """IDs shown by the statistics and the structure list: war sides, allies, structure owners and types."""
        wanted: set[int] = set()
        for r in self.active_rows():
            wanted.update(i for i in (r["aggressor_id"], r["defender_id"]) if i)
            wanted.update(int(a) for a, _ in json.loads(r["allies"]) if a)
        for row in self._loot_base():
            wanted.update((row["owner_id"], row["type_id"]))
        return wanted

    async def ensure_names(self, ids: set[int] | None = None, timeout: float | None = None) -> int:
        """Resolve every ID still without a name (ESI /universe/names, 1000 per call). Returns names added.

        Runs after each census step and again right before the statistics or the structure list are served
        (``timeout`` set: a page never waits long, and after a failure it does not ask again for a minute), so names
        never wait for the end of a long first scan. Unresolved IDs are shown as "Corporation 98000001".
        """
        if timeout is not None and time.time() - self._names_failed_at < NAMES_COOLDOWN_S:
            return 0
        async with self._names_lock:
            wanted = self.wanted_names() if ids is None else {int(i) for i in ids if i}
            known = self.names(list(wanted))
            missing = [i for i in wanted if i not in known]
            if not missing:
                return 0
            try:
                lookup = self.esi.names(missing)
                found = await (asyncio.wait_for(lookup, timeout) if timeout is not None else lookup)
            except (WarEsiError, TimeoutError) as e:
                self._names_failed_at = time.time()
                log.warning("census: name lookup failed (retried later): %r", e)
                return 0
            now = time.time()
            with self.db.tx() as c:
                c.executemany(
                    "INSERT OR REPLACE INTO entity_names(entity_id, name, category, updated_at) VALUES(?,?,?,?)",
                    [(i, n, cat, now) for i, (n, cat) in found.items()],
                )
            if found:
                self.version += 1
            return len(found)

    async def _names(self) -> None:
        await self.ensure_names()

    def names(self, ids: list[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        ids = [int(i) for i in ids if i]
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]
            marks = ",".join("?" * len(chunk))
            rows = self.db.query(f"SELECT entity_id, name FROM entity_names WHERE entity_id IN ({marks})", tuple(chunk))  # noqa: S608
            out.update({r[0]: r[1] for r in rows})
        return out

    # -- high-sec structures of both sides ------------------------------------------------------------
    def _loot_base(self) -> list[dict[str, Any]]:
        """High-sec structures owned by any side of an active war (cached until census or dataset change)."""
        if self.structures is None or not self.structures.loaded:
            return []
        key = (self.version, self.structures.last_refresh, self.structures.revision, id(self.structures.index))
        if self._loot is not None and self._loot[0] == key:
            return self._loot[1]
        now_iso = iso()
        alliance_corps = self.alliance_corps()
        by_entity: dict[int, list[dict[str, Any]]] = {}
        kinds: dict[int, str] = {}
        for r in self.active_rows():
            war = {
                "war_id": r["war_id"],
                "state": war_state(r["started"], r["finished"], now_iso),
                "type": "mutual" if r["mutual"] else ("open_for_allies" if r["open_for_allies"] else "none"),
                "started": r["started"],
                "finished": r["finished"],
            }
            a_kind, d_kind = r["aggressor_type"], r["defender_type"]
            sides = [(r["aggressor_id"], a_kind, "aggressor", r["defender_id"], d_kind)]
            sides.append((r["defender_id"], d_kind, "defender", r["aggressor_id"], a_kind))
            sides += [(a, kind, "ally", r["aggressor_id"], a_kind) for a, kind in json.loads(r["allies"])]
            for eid, kind, side, opponent, opponent_kind in sides:
                if eid:
                    by_entity.setdefault(int(eid), []).append(
                        war | {"side": side, "opponent_id": opponent, "opponent_type": opponent_kind}
                    )
                    kinds[int(eid)] = kind or "corporation"
        rows: dict[int, dict[str, Any]] = {}
        for eid, wars in by_entity.items():
            corps = alliance_corps.get(eid, []) if kinds[eid] == "alliance" else [eid]
            for corp in corps:
                for st in self.structures.index.get(int(corp), []) if self.structures.index else []:
                    idx = self.g.index.get(st.system_id) if st.system_id else None
                    if idx is None or self.g.sec[idx] < HIGHSEC_MIN:
                        continue
                    row = rows.get(st.structure_id)
                    if row is None:
                        system = self.g.describe(idx)
                        name = st.name
                        if name.startswith(f"{system['name']} - "):
                            name = name.split(" - ", 1)[1]
                        row = rows[st.structure_id] = {
                            "structure_id": st.structure_id,
                            "name": name,
                            "type_id": st.type_id,
                            "system": system,
                            "system_index": idx,
                            "owner_id": int(corp),
                            "entities": [],
                            "wars": [],
                        }
                    row["entities"].append({"id": eid, "type": kinds[eid]})
                    row["wars"].extend(wars)
        out = list(rows.values())
        for row in out:
            seen: set[tuple[int, str]] = set()
            row["wars"] = [
                w
                for w in row["wars"]
                if (w["war_id"], w["side"]) not in seen and not seen.add((w["war_id"], w["side"]))
            ]
            row["state"] = min((w["state"] for w in row["wars"]), key=STATES.index)
            row["sides"] = sorted({"defender" if w["side"] == "ally" else w["side"] for w in row["wars"]})
        self._loot = (key, out)
        return out

    def loot(
        self,
        sources: dict[int, list[str]],
        avoid: list[int],
        *,
        type_name: str = "",
        region: str = "",
        side: str = "",
        state: str = "",
        min_sec: float = 0.5,
        sort: str = "jumps",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Filtered, sorted page of high-sec structures with jumps from the caller's nearest character.

        ``sources`` maps system index -> names of the caller's characters there.
        """
        base = self._loot_base()
        dist, origin = self.g.nearest(sources.keys(), avoid)
        names = self.names(
            [r["owner_id"] for r in base]
            + [r["type_id"] for r in base]
            + [e["id"] for r in base for e in r["entities"]]
            + [w["opponent_id"] for r in base for w in r["wars"] if w["opponent_id"]]
        )
        rows = []
        for r in base:
            i = r["system_index"]
            o = origin[i]
            item = {k: v for k, v in r.items() if k != "system_index"}
            item.update(
                type=label(names, r["type_id"], "type"),
                owner=label(names, r["owner_id"], "corporation"),
                owner_zkill=zkill_entity(r["owner_id"], "corporation"),
                entities=[
                    e | {"name": label(names, e["id"], e["type"]), "zkill": zkill_entity(e["id"], e["type"])}
                    for e in r["entities"]
                ],
                wars=[
                    w
                    | {
                        "opponent": label(names, w["opponent_id"], w.get("opponent_type")),
                        "zkill": zkill_war(w["war_id"]),
                    }
                    for w in r["wars"]
                ],
                jumps=dist[i] if dist[i] >= 0 else None,
                nearest=None if o < 0 else {"system": self.g.names[o], "characters": sources.get(o, [])},
            )
            rows.append(item)
        facets = {
            "types": Counter(r["type"] for r in rows).most_common(),
            "regions": Counter(r["system"]["region"] for r in rows).most_common(),
            "total": len(rows),
        }
        if type_name:
            rows = [r for r in rows if r["type"] == type_name]
        if region:
            rows = [r for r in rows if r["system"]["region"] == region]
        if side:
            rows = [r for r in rows if side in r["sides"]]
        if state:
            rows = [r for r in rows if r["state"] == state]
        rows = [r for r in rows if r["system"]["security"] >= min_sec]
        far = 1 << 30
        keys = {
            "jumps": lambda r: (r["jumps"] if r["jumps"] is not None else far, r["system"]["name"], r["name"]),
            "security": lambda r: (-r["system"]["security"], r["system"]["name"], r["name"]),
            "system": lambda r: (r["system"]["name"], r["name"]),
            "type": lambda r: (r["type"], r["jumps"] if r["jumps"] is not None else far),
            "owner": lambda r: (r["owner"].lower(), r["system"]["name"]),
        }
        rows.sort(key=keys.get(sort, keys["jumps"]))
        return {
            "rows": rows[offset : offset + limit],
            "matched": len(rows),
            "facets": facets,
            "characters": sum(len(v) for v in sources.values()),
            "structures_loaded": bool(self.structures and self.structures.loaded),
        }

    # -- statistics -----------------------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        loot_key = (self.structures.last_refresh, self.structures.revision) if self.structures else 0
        key = (self.version, loot_key, int(time.time() // 60))
        if self._stats is not None and self._stats[0] == key:
            return self._stats[1]
        now = time.time()
        now_iso = iso(now)
        rows = self.active_rows()
        states = Counter(war_state(r["started"], r["finished"], now_iso) for r in rows)
        aggressors = Counter(r["aggressor_id"] for r in rows if r["aggressor_id"])
        defenders = Counter(r["defender_id"] for r in rows if r["defender_id"])
        kinds = {r["aggressor_id"]: r["aggressor_type"] for r in rows} | {
            r["defender_id"]: r["defender_type"] for r in rows
        }
        top_isk = sorted(rows, key=lambda r: -(r["aggressor_isk"] + r["defender_isk"]))[:10]
        loot = self._loot_base()
        ids = (
            list(aggressors)
            + list(defenders)
            + [r["aggressor_id"] for r in top_isk]
            + [r["defender_id"] for r in top_isk]
        )
        ids += [r["type_id"] for r in loot]
        names = self.names(ids)

        def top(counter: Counter[int]) -> list[dict[str, Any]]:
            return [
                {
                    "id": i,
                    "name": label(names, i, kinds.get(i)),
                    "type": kinds.get(i),
                    "wars": n,
                    "zkill": zkill_entity(i, kinds.get(i)),
                }
                for i, n in counter.most_common(10)
            ]

        result = {
            "generated_at": now,
            "totals": {
                "active": len(rows),
                "fighting": states["fighting"],
                "pending": states["pending"],
                "ending": states["ending"],
                "mutual": sum(r["mutual"] for r in rows),
                "open_for_allies": sum(r["open_for_allies"] for r in rows),
                "with_allies": sum(1 for r in rows if r["allies"] != "[]"),
                "declared_24h": sum(1 for r in rows if (r["declared"] or "") >= iso(now - 86400)),
                "declared_7d": sum(1 for r in rows if (r["declared"] or "") >= iso(now - 7 * 86400)),
                "ships_killed": sum(r["aggressor_ships"] + r["defender_ships"] for r in rows),
                "isk_destroyed": sum(r["aggressor_isk"] + r["defender_isk"] for r in rows),
                "aggressors": len(aggressors),
                "defenders": len(defenders),
            },
            "top_aggressors": top(aggressors),
            "top_defenders": top(defenders),
            "top_isk": [
                {
                    "war_id": r["war_id"],
                    "zkill": zkill_war(r["war_id"]),
                    "aggressor": label(names, r["aggressor_id"], r["aggressor_type"]),
                    "defender": label(names, r["defender_id"], r["defender_type"]),
                    "isk_destroyed": r["aggressor_isk"] + r["defender_isk"],
                    "ships_killed": r["aggressor_ships"] + r["defender_ships"],
                    "state": war_state(r["started"], r["finished"], now_iso),
                }
                for r in top_isk
                if r["aggressor_isk"] + r["defender_isk"] > 0
            ],
            "structures": {
                "highsec": len(loot),
                "by_type": [
                    [label(names, t, "type"), n] for t, n in Counter(r["type_id"] for r in loot).most_common(10)
                ],
                "by_region": Counter(r["system"]["region"] for r in loot).most_common(10),
                "loaded": bool(self.structures and self.structures.loaded),
            },
            "census": {
                "known_wars": self.db.query("SELECT COUNT(*) FROM war_census")[0][0],
                "examined_ids": int(self._meta("examined") or 0),
                "max_ids": self.s.census_max_ids,
                "complete": self._meta("complete") == "true",
                "oldest_examined_id": int(self._meta("floor") or 0) or None,
                "last_run": self.last_run or None,
            },
        }
        self._stats = (key, result)
        return result
