"""Static roster: an uploaded char_name + star_system_name list, ranked without ESI (UC-12, ROS-01..ROS-22).

A roster entry has no tokens and is never sent to ESI (D10): only a name, a system and the upload time.
One pool per main, merged on upload and keyed by the normalised character name (D11); a system that does
not resolve is stored unresolved with suggestions instead of being rejected (D12).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from .db import Database, RosterRow
from .ranking import RankingService
from .resolver import TargetResolver
from .universe import GateGraph

log = logging.getLogger("evedm.roster")

SCHEMA_NAME = "evedm-static-roster"
# EVE character names: 3-37 characters of letters, digits, spaces, hyphens and apostrophes
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '\-]{1,35}[A-Za-z0-9'\-]$")
RESOLVED, NOT_FOUND, AMBIGUOUS = "resolved", "not_found", "ambiguous"
REASON = {NOT_FOUND: "no such solar system", AMBIGUOUS: "several solar systems match"}


class RosterRejected(Exception):
    """The whole file is rejected; nothing was stored (ROS-02)."""

    def __init__(self, reason: str, errors: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.errors = errors or []


def clean_name(name: str) -> str:
    return " ".join(str(name).split())


def norm_key(name: str) -> str:
    """ROS-04: the identity of an entry - trimmed, inner whitespace collapsed, case-folded."""
    return clean_name(name).casefold()


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    text = resources.files("evedm").joinpath(f"schema/{SCHEMA_NAME}.v1.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def validate(doc: Any) -> list[str]:
    validator = Draft202012Validator(schema(), format_checker=FormatChecker())
    out = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
        out.append(f"{path}: {err.message}")
    return out


def parse(raw: bytes) -> tuple[list[dict[str, Any]], str]:
    """Bytes -> (entries, label). Raises RosterRejected when the file as a whole is wrong (ROS-02, ROS-03)."""
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RosterRejected(f"not valid UTF-8 JSON: {exc}") from exc
    bare = isinstance(doc, list)  # ROS-03: a bare array is the same schema
    wrapped = {"schema": SCHEMA_NAME, "version": 1, "characters": doc} if bare else doc
    errors = validate(wrapped)
    if errors:
        if bare:
            errors = [e.replace("$.characters", "$", 1) for e in errors]
        raise RosterRejected(f"file does not match schema {SCHEMA_NAME} v1", errors)
    return list(wrapped["characters"]), str(wrapped.get("label", ""))[:64]


def jumps_to(parent: Any, origin: int, memo: dict[int, int]) -> int | None:
    """Jump count from ``origin`` along a BFS parent array, memoised across calls (no route built)."""
    if origin < 0 or parent[origin] == -1:
        return None
    stack: list[int] = []
    cur = origin
    while cur not in memo:
        if parent[cur] == cur:
            memo[cur] = 0
            break
        stack.append(cur)
        cur = parent[cur]
    d = memo[cur]
    while stack:
        d += 1
        memo[stack.pop()] = d
    return memo[origin]


class RosterService:
    """The static roster of one instance: import, resolution, management and ranking."""

    def __init__(
        self,
        db: Database,
        graph: GateGraph,
        resolver: TargetResolver,
        ranking: RankingService,
        instance_label: str = "",
    ):
        self.db = db
        self.g = graph
        self.resolver = resolver
        self.ranking = ranking
        self.instance_label = instance_label

    # -- resolution ------------------------------------------------------------------------
    def resolve(self, text: str) -> tuple[int | None, str, list[str]]:
        """(solar system ID, state, suggestions) for a raw system name (ROS-06)."""
        text = str(text).strip()
        idx = self.g.find(text)
        if idx is not None:
            return self.g.ids[idx], RESOLVED, []
        found = self.resolver.extract(text)  # a name inside "Jita IV - Moon 4 - ..." still resolves
        if len(found) == 1:
            return int(found[0]["id"]), RESOLVED, []
        if len(found) > 1:
            return None, AMBIGUOUS, [str(f["name"]) for f in found[:3]]
        return None, NOT_FOUND, [str(s["name"]) for s in self.resolver.autocomplete(text, 3)]

    def resolve_pending(self, owner_id: int = 0) -> int:
        """ROS-11: try the unresolved entries again (after a universe rebuild or a typo fix)."""
        fixed = 0
        for r in self.db.list_roster(owner_id):
            if r.resolve_state == RESOLVED:
                continue
            system_id, state, suggestions = self.resolve(r.system_name_raw)
            self.db.update_roster(
                owner_id, r.name_key, system_id=system_id, resolve_state=state, suggestions=suggestions
            )
            fixed += state == RESOLVED
        return fixed

    # -- import ----------------------------------------------------------------------------
    def import_file(self, raw: bytes, owner_id: int = 0) -> dict[str, Any]:
        """ROS-01..ROS-08: merge an uploaded file into this main's pool and report every entry."""
        entries, label = parse(raw)
        results: list[dict[str, Any]] = []
        keep: list[tuple[int, dict[str, Any], str, str]] = []  # (index, entry, name, key)
        seen: set[str] = set()
        for i, entry in enumerate(entries):
            name = clean_name(entry["char_name"])
            key = norm_key(name)
            if not NAME_RE.match(name):
                reason = "character name must be 3-37 letters, digits, spaces, hyphens or apostrophes"
            elif key in seen:
                reason = "the same character appears earlier in this file"
            else:
                seen.add(key)
                keep.append((i, entry, name, key))
                continue
            results.append(
                {"index": i, "name": name, "system": str(entry["star_system_name"]), "result": "Rejected",
                 "reason": reason, "suggestions": []}
            )
        for i, entry, name, key in keep:
            system_raw = str(entry["star_system_name"]).strip()
            system_id, state, suggestions = self.resolve(system_raw)
            added = self.db.upsert_roster(
                owner_id,
                name_key=key,
                char_name=name,
                system_name_raw=system_raw,
                system_id=system_id,
                resolve_state=state,
                suggestions=suggestions,
                tags=[str(t) for t in entry["tags"]] if entry.get("tags") else None,  # ROS-04: kept when absent
                note=str(entry.get("note", "")),
                enabled=entry.get("enabled"),
            )
            results.append(
                {
                    "index": i,
                    "name": name,
                    "system": system_raw,
                    "result": "Added" if added else "Updated",
                    "reason": "" if state == RESOLVED else REASON[state],
                    "suggestions": suggestions,
                }
            )
        results.sort(key=lambda r: r["index"])
        summary = {
            "Added": sum(1 for r in results if r["result"] == "Added"),
            "Updated": sum(1 for r in results if r["result"] == "Updated"),
            "Unresolved": sum(1 for r in results if r["result"] != "Rejected" and r["reason"]),
            "Rejected": sum(1 for r in results if r["result"] == "Rejected"),
        }
        log.info("roster import for owner %s: %s", owner_id, summary)  # ROS-08: counts only, never contents
        return {"summary": summary, "label": label, "entries": results, "total": self.db.count_roster(owner_id)}

    # -- views -----------------------------------------------------------------------------
    def _row(self, r: RosterRow, live: set[str]) -> dict[str, Any]:
        idx = self.g.index.get(r.system_id) if r.system_id else None
        return {
            "key": r.name_key,
            "name": r.char_name,
            "tags": r.tags,
            "note": r.note,
            "enabled": r.enabled,
            "static": True,
            "system": self.g.describe(idx) if idx is not None else None,
            "system_name": r.system_name_raw,
            "resolve_state": r.resolve_state,
            "reason": "" if r.resolve_state == RESOLVED else REASON.get(r.resolve_state, ""),
            "suggestions": r.suggestions,
            "shadowed": r.name_key in live,  # ROS-15: also a linked character of this main
            "uploaded_at": r.uploaded_at,
            "jumps": None,
            "reachable": False,
            "route": None,
            "route_summary": None,
            "recommended": False,
        }

    def _live_names(self, owner_id: int) -> set[str]:
        return {norm_key(c.name) for c in self.db.list_characters(owner_id)}

    def list_view(self, owner_id: int = 0, state: str = "", tag: str = "") -> list[dict[str, Any]]:
        live = self._live_names(owner_id)
        rows = [self._row(r, live) for r in self.db.list_roster(owner_id)]
        if state == "unresolved":
            rows = [r for r in rows if r["resolve_state"] != RESOLVED]
        elif state in (RESOLVED, NOT_FOUND, AMBIGUOUS):
            rows = [r for r in rows if r["resolve_state"] == state]
        if tag:
            rows = [r for r in rows if tag in r["tags"]]
        return rows

    def stats(self, owner_id: int = 0) -> dict[str, Any]:
        rows = self.db.list_roster(owner_id)
        return {
            "entries": len(rows),
            "enabled": sum(1 for r in rows if r.enabled),
            "unresolved": sum(1 for r in rows if r.resolve_state != RESOLVED),
            "uploaded_at": max((r.uploaded_at for r in rows), default=None),
        }

    # -- ranking (ROS-16..ROS-19) ------------------------------------------------------------
    def rank(self, target: int, owner_id: int = 0, tag: str = "") -> dict[str, Any]:
        """Rank the enabled entries against ``target``, reading the BFS the live ranking already built."""
        t0 = time.perf_counter()
        parent = self.ranking.parents(target)  # RTE-02: one BFS per target serves both lists
        t_bfs = time.perf_counter()
        live = self._live_names(owner_id)
        rows: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for r in self.db.list_roster(owner_id):
            if not r.enabled:
                excluded.append({"name": r.char_name, "reason": "disabled"})
                continue
            if tag and tag not in r.tags:
                continue
            row = self._row(r, live)
            idx = self.g.index.get(r.system_id) if r.system_id else None
            if idx is not None:
                path = self.g.route(parent, idx)
                if path is not None:
                    row.update(
                        {
                            "reachable": True,
                            "jumps": len(path) - 1,
                            "route": [self.g.ids[i] for i in path],
                            "route_summary": self.g.route_summary(path),
                        }
                    )
            rows.append(row)

        def key(r: dict[str, Any]) -> tuple[int, int, str]:
            group = 0 if r["reachable"] else (1 if r["system"] else 2)  # unresolved last (ROS-17)
            return group, r["jumps"] if r["jumps"] is not None else 1 << 30, r["name"].lower()

        rows.sort(key=key)
        for r in rows:
            if r["reachable"]:
                r["recommended"] = True
                break
        return {
            "target": self.g.describe(target),
            "generated_at": time.time(),
            "rows": rows,
            "excluded": excluded,
            "stats": self.stats(owner_id)
            | {
                "ranked": len(rows),
                "reachable": sum(1 for r in rows if r["reachable"]),
                "bfs_ms": round((t_bfs - t0) * 1000, 2),
                "total_ms": round((time.perf_counter() - t0) * 1000, 1),
            },
        }

    # -- alerts (ALR-11) ----------------------------------------------------------------------
    def origins(self, owner_id: int = 0) -> list[tuple[str, int]]:
        """(name, system index) of the enabled, resolved entries, read once per request."""
        live = self._live_names(owner_id)
        out = []
        for r in self.db.list_roster(owner_id):
            idx = self.g.index.get(r.system_id) if r.enabled and r.system_id else None
            if idx is not None and r.name_key not in live:  # ROS-15
                out.append((r.char_name, idx))
        return out

    @staticmethod
    def closest(origins: list[tuple[str, int]], parent: Any, n: int = 3) -> list[dict[str, Any]]:
        """The nearest static characters to a target, jumps only - never a Set destination button."""
        memo: dict[int, int] = {}
        found = []
        for name, idx in origins:
            jumps = jumps_to(parent, idx, memo)
            if jumps is not None:
                found.append({"name": name, "jumps": jumps, "static": True})
        found.sort(key=lambda r: (r["jumps"], r["name"].lower()))
        return found[:n]

    # -- management (ROS-12, ROS-14) ------------------------------------------------------------
    def patch(
        self,
        owner_id: int,
        key: str,
        *,
        enabled: bool | None = None,
        tags: list[str] | None = None,
        system_name: str | None = None,
    ) -> dict[str, Any] | None:
        row = self.db.get_roster(owner_id, key)
        if row is None:
            return None
        fields: dict[str, Any] = {}
        if enabled is not None:
            fields["enabled"] = enabled
        if tags is not None:
            fields["tags"] = tags
        if system_name is not None:
            system_id, state, suggestions = self.resolve(system_name)
            fields |= {
                "system_name_raw": str(system_name).strip()[:64],
                "system_id": system_id,
                "resolve_state": state,
                "suggestions": suggestions,
            }
        self.db.update_roster(owner_id, key, **fields)
        updated = self.db.get_roster(owner_id, key)
        return self._row(updated, self._live_names(owner_id)) if updated else None

    def export(self, owner_id: int = 0) -> dict[str, Any]:
        rows = self.db.list_roster(owner_id)
        if not rows:
            raise RosterRejected("the roster is empty")
        characters = []
        for r in rows:
            item: dict[str, Any] = {"char_name": r.char_name, "star_system_name": r.system_name_raw}
            if r.tags:
                item["tags"] = r.tags
            if r.note:
                item["note"] = r.note
            if not r.enabled:
                item["enabled"] = False
            characters.append(item)
        return {
            "schema": SCHEMA_NAME,
            "version": 1,
            "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "exported_from": self.instance_label[:64],
            "characters": characters,
        }
