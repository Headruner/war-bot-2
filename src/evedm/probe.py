"""Read-only diagnostics for the structure scout: what the asset locations of a few characters really are.

The scout takes every asset whose ``location_type`` is ``other`` and whose ``location_id`` is at least 1e12 for the
ID of an Upwell structure. Customs offices and corporation offices share that ID range and cannot be resolved, and
``other`` also covers items inside a hangar flag such as ``AutoFit``. This module reads the asset lists of some
characters and groups the locations by (location_type, location_flag), so the ingestion filter can be chosen from
evidence: how many distinct IDs each group has, how many are the character's own items (ships, containers), and what
the database already knows about the rest (resolved, refused, never asked, gone, or not stored at all).

Nothing here writes to ``known_structures``, ``structure_sightings`` or ``structure_refusals``. The only ESI calls are
``GET /characters/{id}/assets`` pages (they cost no errors); a token refresh goes through ``CharacterService``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .characters import CharacterService
from .db import Database
from .esi import EsiClient, EsiError
from .sso import SsoError

HIGH_ID = 1_000_000_000_000  # structures, customs offices and corporation offices all live above this
STATUSES = ("ok", "no_access", "new", "gone", "not_in_db")
TOP_PARENT_TYPES = 5


@dataclass
class _Bucket:
    rows: int = 0
    locs: set[int] = field(default_factory=set)
    own: set[int] = field(default_factory=set)  # locations that are items of the character itself
    parents: Counter[int] = field(default_factory=Counter)  # type_id of those parent items, once per location


def _scan(rows: list[dict[str, Any]]) -> dict[tuple[str, str], _Bucket]:
    """Group the rows located at an ID of 1e12 or more by (location_type, location_flag)."""
    type_of = {int(r["item_id"]): r.get("type_id") for r in rows if r.get("item_id") is not None}
    out: dict[tuple[str, str], _Bucket] = {}
    for r in rows:
        loc = int(r.get("location_id") or 0)
        if loc < HIGH_ID:
            continue
        bucket = out.setdefault((str(r.get("location_type") or ""), str(r.get("location_flag") or "")), _Bucket())
        bucket.rows += 1
        if loc not in bucket.locs:
            bucket.locs.add(loc)
            if loc in type_of:
                bucket.own.add(loc)
                if type_of[loc] is not None:
                    bucket.parents[int(type_of[loc])] += 1
    return out


def _merge(parts: list[dict[tuple[str, str], _Bucket]]) -> dict[tuple[str, str], _Bucket]:
    total: dict[tuple[str, str], _Bucket] = {}
    for part in parts:
        for key, b in part.items():
            t = total.setdefault(key, _Bucket())
            t.rows += b.rows
            t.locs |= b.locs
            t.own |= b.own
            t.parents.update(b.parents)
    return total


def _statuses(db: Database, ids: set[int]) -> dict[int, str]:
    """known_structures.status per ID; an ID the database does not hold is ``not_in_db``."""
    found: dict[int, str] = {}
    ordered = sorted(ids)
    for start in range(0, len(ordered), 900):
        chunk = ordered[start : start + 900]
        sql = f"SELECT structure_id, status FROM known_structures WHERE structure_id IN ({','.join('?' * len(chunk))})"  # noqa: S608
        found.update({r[0]: r[1] for r in db.query(sql, tuple(chunk))})
    return {i: found.get(i, "not_in_db") for i in ids}


def _render(
    buckets: dict[tuple[str, str], _Bucket], status: dict[int, str], names: dict[int, str], samples: int
) -> list[dict[str, Any]]:
    out = []
    for (ltype, flag), b in buckets.items():
        by: dict[str, list[int]] = {s: [] for s in STATUSES}
        for loc in sorted(b.locs):
            by.setdefault(status[loc], []).append(loc)
        out.append(
            {
                "location_type": ltype,
                "location_flag": flag,
                "rows": b.rows,
                "locations": len(b.locs),
                "own_item": len(b.own),
                "db": {s: len(v) for s, v in by.items()},
                "samples": {s: v[: max(0, samples)] for s, v in by.items() if v},
                "own_item_parent_types": [
                    {"type_id": t, "name": names.get(t), "count": n} for t, n in b.parents.most_common(TOP_PARENT_TYPES)
                ],
            }
        )
    out.sort(key=lambda x: (-x["locations"], x["location_type"], x["location_flag"]))
    return out


def _verdict(total: list[dict[str, Any]]) -> dict[str, Any]:
    """Per location_flag under location_type ``other``: the share of distinct IDs resolved (ok) against refused."""
    flags: dict[str, Any] = {}
    for b in total:
        if b["location_type"] != "other":
            continue
        n = b["locations"]
        ok, refused = b["db"]["ok"], b["db"]["no_access"]
        tag = "mixed"
        if n and ok / n >= 0.5:
            tag = "mostly_resolved"
        elif n and refused / n >= 0.5:
            tag = "mostly_refused"
        flags[b["location_flag"]] = {
            "distinct_ids": n,
            "ok": ok,
            "no_access": refused,
            "ok_share": round(ok / n, 3) if n else 0.0,
            "no_access_share": round(refused / n, 3) if n else 0.0,
            "new_gone_or_not_in_db": n - ok - refused,
            "own_item": b["own_item"],
            "reading": tag,  # mostly_resolved: ok >= half; mostly_refused: no_access >= half; else mixed
        }
    return {"location_type_other_by_flag": flags}


def probe_assets(assets: dict[int, list[dict[str, Any]]], db: Database, samples: int = 5) -> dict[str, Any]:
    """Report on the asset rows (character_id -> rows as ESI returns them); reads the database, never writes it.

    Per character and over all of them: for each (location_type, location_flag) the number of rows, the distinct
    location IDs, how many of those are the character's own items, the split of the distinct IDs by database status,
    the commonest types of those parent items and ``samples`` example IDs per status. A combined ID counts once even
    when several characters hold assets there.
    """
    scans = {cid: _scan(rows) for cid, rows in assets.items()}
    total = _merge(list(scans.values()))
    status = _statuses(db, {loc for part in scans.values() for b in part.values() for loc in b.locs})
    type_ids = {t for part in scans.values() for b in part.values() for t in b.parents}
    names = db.get_type_names(sorted(type_ids)) if type_ids else {}
    rendered_total = _render(total, status, names, samples)
    return {
        "characters": {
            str(cid): {
                "assets": len(assets[cid]),
                "assets_at_1e12_or_more": sum(b.rows for b in part.values()),
                "buckets": _render(part, status, names, samples),
            }
            for cid, part in scans.items()
        },
        "total": {
            "characters": len(scans),
            "assets": sum(len(rows) for rows in assets.values()),
            "assets_at_1e12_or_more": sum(b.rows for b in total.values()),
            "buckets": rendered_total,
        },
        "verdict": _verdict(rendered_total),
    }


def pick_characters(
    db: Database, readers: list[tuple[int, int, list[str]]], count: int, explicit: list[int] | None = None
) -> tuple[list[tuple[int, int]], list[dict[str, Any]]]:
    """(character_id, owner_id) to read, and the requested IDs that cannot be read.

    ``readers`` are the characters holding the assets scope (``StructureScout.contributors``). Without ``explicit``
    the ``count`` of them with the most rows in ``structure_sightings`` are taken.
    """
    owner = {cid: own for cid, own, _ in readers}
    if explicit:
        picked = [(cid, owner[cid]) for cid in dict.fromkeys(explicit) if cid in owner]
        skipped = [
            {"character_id": cid, "reason": "not registered with the assets scope"}
            for cid in dict.fromkeys(explicit)
            if cid not in owner
        ]
        return picked, skipped
    sql = "SELECT character_id, COUNT(*) FROM structure_sightings GROUP BY character_id"
    seen = {r[0]: r[1] for r in db.query(sql)}
    ranked = sorted(owner, key=lambda cid: (-seen.get(cid, 0), cid))
    return [(cid, owner[cid]) for cid in ranked[: max(0, count)]], []


async def probe_characters(
    db: Database,
    chars: CharacterService,
    esi: EsiClient,
    picked: list[tuple[int, int]],
    *,
    max_pages: int = 50,
    samples: int = 5,
) -> dict[str, Any]:
    """Read the asset pages of each character, then ``probe_assets``. Stops cleanly while ESI's error window is low."""
    assets: dict[int, list[dict[str, Any]]] = {}
    read: dict[str, Any] = {}
    stopped: dict[str, Any] | None = None
    for cid, owner in picked:
        wait = esi.blocked_for()
        if wait > 0:
            stopped = {"reason": "blocked", "retry_in_s": round(wait), "before_character": cid}
            break
        rows: list[dict[str, Any]] = []
        info: dict[str, Any] = {"pages_read": 0, "pages_total": 0, "complete": False, "error": None}
        try:
            token = await chars.access_token(cid, owner)
            async for page, pages in esi.iter_pages(f"/characters/{cid}/assets", token, max_pages=max_pages):
                rows.extend(page)
                info["pages_read"] += 1
                info["pages_total"] = pages
            info["complete"] = info["pages_total"] <= max_pages
        except (EsiError, SsoError) as exc:
            info["error"] = str(exc)[:120]
            if isinstance(exc, EsiError) and exc.status in (420, 429):
                stopped = {"reason": "blocked", "retry_in_s": round(esi.blocked_for()), "before_character": cid}
        assets[cid] = rows  # what was read before a failure is still analysed (complete: false)
        read[str(cid)] = info
        if stopped is not None:
            break
    report = probe_assets(assets, db, samples)
    for cid_text, info in read.items():
        report["characters"][cid_text]["read"] = info
    report["run"] = {"characters": [c for c, _ in picked], "max_pages": max_pages, "stopped": stopped}
    return report
