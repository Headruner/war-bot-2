"""Ranking characters by stargate jumps to a target, with the full route per character (RNK-*, RTE-*)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .characters import REAUTH, CharacterService
from .config import WAYPOINT_SCOPE
from .db import CharacterRow, Database
from .esi import EsiClient, EsiError
from .evescout import EveScout
from .sso import SsoError
from .universe import CAPSULE_TYPE_IDS, GateGraph

log = logging.getLogger("evedm.ranking")


class StateProvider(Protocol):
    async def fetch(self, character: CharacterRow) -> dict[str, Any]: ...


class LiveProvider:
    """Reads location, ship and online state from ESI with the character's own token."""

    def __init__(self, chars: CharacterService, esi: EsiClient):
        self.chars = chars
        self.esi = esi

    async def fetch(self, character: CharacterRow) -> dict[str, Any]:
        token = await self.chars.access_token(character.character_id, character.owner_id)
        state = await self.esi.character_state(character.character_id, token, character.scopes)
        # Only the parts this link may read: the snapshot is shared by every main that links the character,
        # so a link without the ship scope must not blank out the ship another main's link fetched.
        out: dict[str, Any] = {}
        if "location" in state:
            loc = state["location"] or {}
            out |= {k: loc.get(k) for k in ("solar_system_id", "station_id", "structure_id")}
        if "ship" in state:
            ship = state["ship"] or {}
            out |= {k: ship.get(k) for k in ("ship_type_id", "ship_item_id", "ship_name")}
        if "online" in state:
            online = state["online"] or {}
            out |= {
                "online": None if not online else int(bool(online.get("online"))),
                "last_login": online.get("last_login"),
                "last_logout": online.get("last_logout"),
            }
        return out


class DemoProvider:
    """Demo mode: no network, the seeded snapshots are the truth."""

    def __init__(self, db: Database):
        self.db = db

    async def fetch(self, character: CharacterRow) -> dict[str, Any]:
        row = self.db.get_snapshots().get(character.character_id)
        if row is None:
            raise EsiError(404, "no demo snapshot")
        return {
            k: row[k]
            for k in (
                "solar_system_id",
                "station_id",
                "structure_id",
                "ship_type_id",
                "ship_item_id",
                "ship_name",
                "online",
                "last_login",
                "last_logout",
            )
        }


@dataclass
class _RefreshState:
    """Single-flight refresh bookkeeping, one per owner (main) so users never wait on each other."""

    task: asyncio.Task[dict[int, str | None]] | None = None
    last_refresh: float = 0.0
    last_errors: dict[int, str | None] = field(default_factory=dict)
    last_target: tuple[int, float] = (-1, 0.0)


class RankingService:
    def __init__(
        self,
        graph: GateGraph,
        db: Database,
        provider: StateProvider,
        *,
        esi: EsiClient | None = None,
        avoid_names: list[str] | None = None,
        stale_after_s: int = 120,
        refresh_interval_s: float = 5.0,
        max_wait_age_s: float = 30.0,
    ):
        self.g = graph
        self.db = db
        self.provider = provider
        self.esi = esi
        self.stale_after_s = stale_after_s
        self.avoid = [i for i in (graph.find(n) for n in (avoid_names or [])) if i is not None]
        self._bfs_cache: dict[int, Any] = {}
        # single-flight refresh: concurrent rankings share one ESI round (ESI caches location for 5 s anyway)
        self.refresh_interval_s = refresh_interval_s
        self.max_wait_age_s = max_wait_age_s
        self._states: dict[int, _RefreshState] = {}

    def _state(self, owner_id: int) -> _RefreshState:
        return self._states.setdefault(owner_id, _RefreshState())

    def parents(self, target: int) -> Any:
        """Cached BFS from ``target`` (RTE-02): one search serves the ranking, the roster and the alerts."""
        parent = self._bfs_cache.get(target)
        if parent is None:
            parent = self.g.bfs(target, self.avoid)
            if len(self._bfs_cache) >= 64:
                self._bfs_cache.pop(next(iter(self._bfs_cache)))
            self._bfs_cache[target] = parent
        return parent

    _parents = parents  # the earlier private name, still used by callers

    async def _fetch_one(self, c: CharacterRow) -> tuple[int, dict[str, Any] | None, str | None]:
        try:
            fields = await self.provider.fetch(c)
        except SsoError as exc:
            return c.character_id, None, f"sso:{exc.code}"
        except EsiError as exc:
            return c.character_id, None, f"esi:{exc.status}"
        fields["fetched_at"] = time.time()
        fields["error"] = None
        return c.character_id, fields, None

    async def _do_refresh(self, chars: list[CharacterRow], st: _RefreshState) -> dict[int, str | None]:
        results = await asyncio.gather(*(self._fetch_one(c) for c in chars))
        self.db.save_snapshots([(cid, fields, err) for cid, fields, err in results])  # one transaction
        st.last_refresh = time.time()
        st.last_errors = {cid: err for cid, _, err in results}
        return st.last_errors

    async def refresh_states(
        self, chars: list[CharacterRow], mode: int = 1, owner_id: int = 0
    ) -> dict[int, str | None]:
        """Refresh character states from ESI.

        mode 2: wait for a fresh round (the Refresh button). mode 1: stale-while-revalidate - answer from
        snapshots younger than ``max_wait_age_s`` and refresh in the background once ``refresh_interval_s``
        has passed. Concurrent callers always share one in-flight round (single flight).
        """
        st = self._state(owner_id)
        running = st.task is not None and not st.task.done()
        known = all(c.character_id in st.last_errors for c in chars)
        age = time.time() - st.last_refresh
        if mode == 1 and known and age < self.max_wait_age_s:
            if age >= self.refresh_interval_s and not running:
                st.task = asyncio.ensure_future(self._do_refresh(chars, st))
            return st.last_errors
        if running:
            return await asyncio.shield(st.task)  # type: ignore[arg-type]
        if mode != 2 and known and age < self.refresh_interval_s:
            return st.last_errors
        st.task = asyncio.ensure_future(self._do_refresh(chars, st))
        return await asyncio.shield(st.task)

    async def _ship_names(self, type_ids: set[int]) -> dict[int, str]:
        names = {t: self.g.ships[t] for t in type_ids if t in self.g.ships}
        missing = [t for t in type_ids if t not in names]
        if missing:
            names.update(self.db.get_type_names(missing))
            missing = [t for t in missing if t not in names]
        if missing and self.esi is not None:
            try:
                fetched = await self.esi.names(missing)
                self.db.save_type_names(fetched)
                names.update(fetched)
            except EsiError:
                pass
        return names

    def _wormhole_view(self, plan: dict[str, Any], direct_jumps: int | None, used: bool) -> dict[str, Any]:
        """What the ranking row says about the Thera / Turnur route (EVS-05)."""

        def end(idx: int, link: dict[str, Any]) -> dict[str, Any]:
            return self.g.describe(idx) | {
                k: link.get(k) for k in ("wh_type", "max_ship_size", "remaining_hours", "expires_at")
            }

        return {
            "hub": plan["hub"],
            "hub_id": plan["hub_id"],
            "jumps": plan["jumps"],
            "direct_jumps": direct_jumps,
            "used": used,
            "entry": end(plan["entry"], plan["entry_link"]),
            "exit": end(plan["exit"], plan["exit_link"]),
            "gates_to_entry": len(plan["to_entry"]) - 1,
            "gates_from_exit": len(plan["from_exit"]) - 1,
        }

    async def rank(
        self, target: int, refresh: int = 1, owner_id: int = 0, evescout: EveScout | None = None
    ) -> dict[str, Any]:
        """Rank the enabled characters of ``owner_id`` for ``target``.

        refresh: 0 = snapshots only, 1 = stale-while-revalidate, 2 = wait for fresh data.
        evescout: when given, a route through Thera / Turnur replaces the gate route if it is shorter (EVS-04).
        """
        t0 = time.perf_counter()
        all_chars = self.db.list_characters(owner_id)
        chars = [c for c in all_chars if c.enabled and c.status != REAUTH]
        excluded = [
            {
                "character_id": c.character_id,
                "name": c.name,
                "reason": "re-auth required" if c.status == REAUTH else "disabled",
            }
            for c in all_chars
            if not (c.enabled and c.status != REAUTH)
        ]
        errors: dict[int, str | None] = {}
        if refresh and chars:
            errors = dict(await self.refresh_states(chars, int(refresh), owner_id))
            # a refresh can flip a character to REAUTH (TOK-04): drop it from the ranking
            reauth = {c.character_id for c in self.db.list_characters(owner_id) if c.status == REAUTH}
            for c in [c for c in chars if c.character_id in reauth]:
                chars.remove(c)
                excluded.append({"character_id": c.character_id, "name": c.name, "reason": "re-auth required"})
        t_fetch = time.perf_counter()
        parent = self.parents(target)
        t_bfs = time.perf_counter()
        snaps = self.db.get_snapshots([c.character_id for c in chars])
        ship_names = await self._ship_names({s["ship_type_id"] for s in snaps.values() if s["ship_type_id"]})
        now = time.time()
        rows: list[dict[str, Any]] = []
        for c in chars:
            s = snaps.get(c.character_id)
            row: dict[str, Any] = {
                "character_id": c.character_id,
                "name": c.name,
                "tags": c.tags,
                "status": c.status,
                # this main's own refresh result wins over the shared snapshot (another main's link may fail)
                "error": errors[c.character_id] if c.character_id in errors else (s["error"] if s else None),
                "jumps": None,
                "reachable": False,
                "system": None,
                "route": None,
                "route_summary": None,
                "recommended": False,
                "route_legs": None,
                "wormhole": None,
                "can_set_route": WAYPOINT_SCOPE in c.scopes or c.demo,
            }
            idx = self.g.index.get(s["solar_system_id"]) if s and s["solar_system_id"] else None
            if s is not None:
                fetched = s["fetched_at"] or 0
                row.update(
                    {
                        "docked": bool(s["station_id"] or s["structure_id"]),
                        "ship_type_id": s["ship_type_id"],
                        "ship_type": ship_names.get(s["ship_type_id"], "") if s["ship_type_id"] else "",
                        "ship_name": s["ship_name"] or "",
                        "in_pod": s["ship_type_id"] in CAPSULE_TYPE_IDS,
                        "online": None if s["online"] is None else bool(s["online"]),
                        "last_logout": s["last_logout"],
                        "last_login": s["last_login"],
                        "fetched_at": fetched or None,
                        "age_s": round(now - fetched) if fetched else None,
                        "stale": bool(fetched) and now - fetched > self.stale_after_s,
                    }
                )
            if idx is not None:
                row["system"] = self.g.describe(idx)
                path = self.g.route(parent, idx)
                direct = None if path is None else len(path) - 1
                if path is not None:
                    ids = [self.g.ids[i] for i in path]
                    row.update(
                        {
                            "reachable": True,
                            "jumps": direct,
                            "route": ids,
                            "route_summary": self.g.route_summary(path),
                            "route_legs": [{"mode": "gate", "ids": ids}],
                        }
                    )
                plan = evescout.via(idx, target, self.avoid) if evescout is not None else None
                if plan is not None:
                    better = direct is None or plan["jumps"] < direct  # a tie keeps the plain gate route
                    row["wormhole"] = self._wormhole_view(plan, direct, better)
                    if better:
                        first, second = plan["to_entry"], plan["from_exit"]
                        sid = self.g.ids
                        row.update(
                            {
                                "reachable": True,
                                "jumps": plan["jumps"],
                                "route": [sid[i] for i in first + second],
                                "route_summary": self.g.route_summary(first + second),
                                "route_legs": [
                                    {"mode": "gate", "ids": [sid[i] for i in first]},
                                    {
                                        "mode": "wormhole",
                                        "hub": plan["hub"],
                                        "hub_id": plan["hub_id"],
                                        "ids": [sid[plan["entry"]], sid[plan["exit"]]],
                                    },
                                    {"mode": "gate", "ids": [sid[i] for i in second]},
                                ],
                            }
                        )
            rows.append(row)

        def key(r: dict[str, Any]) -> tuple[int, int, int, str]:
            group = 0 if r["reachable"] else (1 if r["system"] else 2)
            return (
                group,
                r["jumps"] if r["jumps"] is not None else 1 << 30,
                0 if r.get("online") else 1,
                r["name"].lower(),
            )

        rows.sort(key=key)
        for r in rows:
            if r["reachable"]:
                r["recommended"] = True  # RNK-04 (v0.3): fewest jumps, online state is informational
                break
        st = self._state(owner_id)
        last_target, last_at = st.last_target
        if last_target != target or now - last_at > 60:  # avoid a DB write per auto-refresh
            self.db.remember_target(self.g.ids[target], owner_id)
            st.last_target = (target, now)
        return {
            "target": self.g.describe(target),
            "generated_at": now,
            "rows": rows,
            "excluded": excluded,
            "evescout": evescout.status() if evescout is not None else None,
            "stats": {
                "characters": len(rows),
                "reachable": sum(1 for r in rows if r["reachable"]),
                "fetch_ms": round((t_fetch - t0) * 1000, 1),
                "bfs_ms": round((t_bfs - t_fetch) * 1000, 2),
                "total_ms": round((time.perf_counter() - t0) * 1000, 1),
            },
        }
