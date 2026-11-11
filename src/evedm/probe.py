"""Read-only diagnostics for the structure scout: what the asset locations of a few characters really are.

The scout takes a location for a structure when its ID is at least 1e12, its flag is one of ``STRUCTURE_FLAGS`` and
it is not one of the character's own items (``is_structure_candidate`` in ``scout.py``; ``location_type`` is not used).
This module reads the asset lists of some characters and groups the locations by (location_type, location_flag), so
the rule can be checked against evidence: how many distinct IDs each group has, how many pass the rule (``candidates``),
how many are the character's own items (ships, containers), and what the database already knows about them (resolved,
refused, never asked, gone, or not stored at all).

Nothing here writes to ``known_structures``, ``structure_sightings``, ``structure_access`` or
``structure_refusals``. The only ESI calls are ``GET /characters/{id}/assets`` pages and, for ``search_probe``,
``GET /characters/{id}/search/`` (neither costs errors); a token refresh goes through ``CharacterService``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .characters import CharacterService
from .db import Database
from .esi import EsiClient, EsiError, search_structure_ids
from .scout import STRUCTURE_MIN_ID as HIGH_ID
from .scout import WSPACE_MIN_ID, is_structure_candidate
from .sso import SsoError

STATUSES = ("ok", "no_access", "new", "gone", "not_in_db")
TOP_PARENT_TYPES = 5


@dataclass
class _Bucket:
    rows: int = 0
    locs: set[int] = field(default_factory=set)
    own: set[int] = field(default_factory=set)  # locations that are items of the character itself
    parents: Counter[int] = field(default_factory=Counter)  # type_id of those parent items, once per location
    cands: set[int] = field(default_factory=set)  # locations the scout's rule takes for structures


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
            if is_structure_candidate(loc, r.get("location_flag"), type_of):
                bucket.cands.add(loc)
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
            t.cands |= b.cands
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
                "candidates": len(b.cands),  # distinct IDs that pass the scout's rule
                "db": {s: len(v) for s, v in by.items()},
                "samples": {s: v[: max(0, samples)] for s, v in by.items() if v},
                "own_item_parent_types": [
                    {"type_id": t, "name": names.get(t), "count": n} for t, n in b.parents.most_common(TOP_PARENT_TYPES)
                ],
            }
        )
    out.sort(key=lambda x: (-x["locations"], x["location_type"], x["location_flag"]))
    return out


def _verdict(total: list[dict[str, Any]], candidates: dict[str, Any]) -> dict[str, Any]:
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
    return {"location_type_other_by_flag": flags, "candidates": candidates}


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
    cand_ids = {loc for b in total.values() for loc in b.cands}
    by_status = Counter(status[loc] for loc in cand_ids)
    candidates = {"distinct_ids": len(cand_ids), "db": {s: by_status.get(s, 0) for s in STATUSES}}
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
        "verdict": _verdict(rendered_total, candidates),
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


async def search_probe(
    db: Database,
    chars: CharacterService,
    esi: EsiClient,
    character_id: int,
    owner_id: int,
    terms: list[str],
    *,
    cap: int,
    samples: int = 5,
) -> dict[str, Any]:
    """Run the structure search of one character for each term and report what came back (nothing is stored).

    This is how to check the two assumptions behind the scout's dock search on a live character: that the term " - "
    matches as a substring (``count`` well above 0) and where the result is cut off (``hit_cap``, ``count`` equal to the
    cap on every long term). Stops cleanly while ESI's error window is low or after a 420/429.
    """
    out: list[dict[str, Any]] = []
    stopped: dict[str, Any] | None = None
    token: str | None = None
    for term in terms:
        wait = esi.blocked_for()
        if wait > 0:
            stopped = {"reason": "blocked", "retry_in_s": round(wait), "before_term": term}
            break
        row: dict[str, Any] = {
            "term": term,
            "count": None,
            "hit_cap": None,
            "structure_ids": None,
            "sample": [],
            "error": None,
        }
        try:
            token = token or await chars.access_token(character_id, owner_id)
            ids = await search_structure_ids(esi, character_id, token, term)
            row["count"] = len(ids)
            row["hit_cap"] = len(ids) >= cap
            row["structure_ids"] = sum(1 for i in ids if i >= HIGH_ID)
            row["sample"] = ids[: max(0, samples)]
        except (EsiError, SsoError) as exc:
            row["error"] = str(exc)[:120]
            if isinstance(exc, EsiError) and exc.status in (420, 429):
                stopped = {"reason": "blocked", "retry_in_s": round(esi.blocked_for()), "before_term": term}
        out.append(row)
        if stopped is not None:
            break
    return {"character_id": character_id, "cap": cap, "terms": out, "stopped": stopped}


def default_search_terms(db: Database, graph: Any, character_id: int, prefix_len: int = 3) -> list[str]:
    """The Ansiblex term " » ", and the prefix and the full name of the character's last known system."""
    from .ansiblex import SEARCH_TERM

    terms = [SEARCH_TERM]
    rows = db.query("SELECT solar_system_id FROM snapshots WHERE character_id=?", (character_id,))
    idx = graph.index.get(rows[0][0]) if rows and rows[0][0] else None
    if idx is not None:
        terms += [graph.names[idx][:prefix_len], graph.names[idx]]
    return list(dict.fromkeys(terms))


# -- self-test: known structures must be found by their own system name ----------------------------------------
SHAPES = ("plain", "hyphen_early", "hyphen_late", "space", "digit_start", "jspace", "other")
TERM_KINDS = ("prefix3", "prefix4", "full")


def name_shape(name: str, system_id: int) -> str:
    """The shape of a system name, which decides how ESI's start-of-name match may treat its prefix."""
    if system_id >= WSPACE_MIN_ID:
        return "jspace"
    if " " in name:
        return "space"
    if name[:1].isdigit():
        return "digit_start"
    if "-" in name[:3]:
        return "hyphen_early"
    if "-" in name:
        return "hyphen_late"
    return "plain" if name.isalpha() else "other"


def pick_known_structures(db: Database, graph: Any, character_id: int, limit: int) -> list[dict[str, Any]]:
    """Resolved structures the character sights or has access rows for, a spread of system name shapes first."""
    rows = db.query(
        "SELECT k.structure_id, k.solar_system_id, "
        "EXISTS(SELECT 1 FROM structure_sightings s WHERE s.structure_id=k.structure_id "
        "AND s.character_id=?1) AS sighted, "
        "EXISTS(SELECT 1 FROM structure_access a WHERE a.structure_id=k.structure_id AND a.character_id=?1) AS docks "
        "FROM known_structures k WHERE k.status='ok' AND k.solar_system_id IS NOT NULL "
        "AND (sighted OR docks) ORDER BY docks DESC, k.structure_id",
        (character_id,),
    )
    queues: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        idx = graph.index.get(r["solar_system_id"])
        if idx is None:
            continue
        name = graph.names[idx]
        queues.setdefault(name_shape(name, r["solar_system_id"]), []).append(
            {
                "structure_id": r["structure_id"],
                "system": name,
                "shape": name_shape(name, r["solar_system_id"]),
                "sighted": bool(r["sighted"]),
                "access_row": bool(r["docks"]),
            }
        )
    for shape, queue in queues.items():  # distinct systems first, the rest behind them
        first: dict[str, dict[str, Any]] = {}
        rest: list[dict[str, Any]] = []
        for item in queue:
            if item["system"] in first:
                rest.append(item)
            else:
                first[item["system"]] = item
        queues[shape] = [*first.values(), *rest]
    picked: list[dict[str, Any]] = []
    while len(picked) < limit and any(queues.values()):
        for shape in SHAPES:
            if queues.get(shape) and len(picked) < limit:
                picked.append(queues[shape].pop(0))
    return picked


async def self_test(
    db: Database,
    graph: Any,
    chars: CharacterService,
    esi: EsiClient,
    characters: list[tuple[int, int]],
    *,
    limit: int = 12,
    cap: int = 500,
    ansiblex_owners: dict[int, int] | None = None,
) -> dict[str, Any]:
    """Search known structures by their 3- and 4-character system prefix and full system name (nothing is stored).

    A structure counts as evidence against the prefix only when the full name finds it and the prefix does not: a
    miss of the full name can also mean the character lost docking there (assets do not prove access).
    """
    per_char: dict[str, Any] = {}
    stopped: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []
    for cid, owner in characters:
        picked = pick_known_structures(db, graph, cid, limit)
        entry: dict[str, Any] = {"picked": len(picked), "structures": []}
        per_char[str(cid)] = entry
        cache: dict[str, list[int]] = {}
        token: str | None = None
        for item in picked:
            name = item["system"]
            terms = {"prefix3": name[:3], "prefix4": name[:4], "full": name}
            row: dict[str, Any] = dict(item) | {"terms": {}}
            for kind, term in terms.items():
                wait = esi.blocked_for()
                if term not in cache and wait > 0:
                    stopped = {"reason": "blocked", "retry_in_s": round(wait), "before_term": term}
                    break
                try:
                    if term not in cache:
                        token = token or await chars.access_token(cid, owner)
                        cache[term] = await search_structure_ids(esi, cid, token, term, cache=False)
                    ids = cache[term]
                    row["terms"][kind] = {
                        "term": term,
                        "count": len(ids),
                        "hit": item["structure_id"] in ids,
                        "capped": len(ids) >= cap,
                    }
                except (EsiError, SsoError) as exc:
                    row["terms"][kind] = {"term": term, "error": str(exc)[:120]}
                    if isinstance(exc, EsiError) and exc.status in (420, 429):
                        stopped = {"reason": "blocked", "retry_in_s": round(esi.blocked_for()), "before_term": term}
                        break
            entry["structures"].append(row)
            results.append(row)
            if stopped is not None:
                break
        if stopped is not None:
            break
    report = summarize_self_test(results)
    report["characters"] = per_char
    report["stopped"] = stopped
    report["ansiblex"] = await ansiblex_check(db, chars, esi, ansiblex_owners or {})
    return report


def summarize_self_test(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per shape and in total: hits/tries per term kind; and the recommendation computed from it."""

    def blank() -> dict[str, Any]:
        t: dict[str, Any] = {"structures": 0, "full_found": 0}
        t |= {k: {"hits": 0, "tries": 0} for k in TERM_KINDS}
        t |= {"evidence_against_prefix3": 0, "evidence_against_prefix4": 0}
        return t

    shapes: dict[str, dict[str, Any]] = {}
    total = blank()
    evidence = {"punctuated": 0, "plain": 0, "plain4": 0, "punctuated4": 0}
    for row in results:
        t = row["terms"]
        for table in (shapes.setdefault(row["shape"], blank()), total):
            table["structures"] += 1
            for kind in TERM_KINDS:
                if kind in t and "hit" in t[kind]:
                    table[kind]["tries"] += 1
                    table[kind]["hits"] += int(t[kind]["hit"])
            if t.get("full", {}).get("hit"):
                table["full_found"] += 1
                for kind, key in (("prefix3", "evidence_against_prefix3"), ("prefix4", "evidence_against_prefix4")):
                    if kind in t and "hit" in t[kind] and not t[kind]["hit"] and not t[kind]["capped"]:
                        table[key] += 1
        if t.get("full", {}).get("hit"):
            punct = "prefix3" in t and not t["prefix3"]["term"].isalnum()
            if "prefix3" in t and "hit" in t["prefix3"] and not t["prefix3"]["hit"] and not t["prefix3"]["capped"]:
                evidence["punctuated" if punct else "plain"] += 1
            if "prefix4" in t and "hit" in t["prefix4"] and not t["prefix4"]["hit"] and not t["prefix4"]["capped"]:
                evidence["punctuated4" if punct else "plain4"] += 1
    bad_shapes = sorted(k for k, v in shapes.items() if v["evidence_against_prefix3"])
    if not results:
        text, settings = "no resolved structures to test: let the scout resolve some first", {}
    elif not total["full_found"]:
        text = (
            "inconclusive: the full system name found none of the known structures (the characters may not be allowed "
            "to dock there, or the search does not match full names)"
        )
        settings = {}
    elif not evidence["punctuated"] and not evidence["plain"]:
        text = "prefix ok for all shapes tested: keep dock_search_prefix_len 3 and dock_search_punctuated prefix"
        settings = {}
    elif not evidence["plain"]:
        text = "punctuated prefixes fail: use the full system name for those: set dock_search_punctuated to full"
        settings = {"dock_search_punctuated": "full"}
    elif not evidence["plain4"] and not evidence["punctuated4"]:
        text = f"3-character prefixes fail for shapes {bad_shapes}, 4-character ones work: set dock_search_prefix_len 4"
        settings = {"dock_search_prefix_len": 4}
    else:
        text = f"prefix search is unreliable for shapes {bad_shapes}: full system names would be needed for them"
        settings = {}
    return {
        "shapes": shapes,
        "totals": total,
        "recommendation": text,
        "recommended_settings": settings,
        "note": (
            "A miss can also mean the character lost docking there (assets alone do not prove access): only structures "
            "where the full system name hits but the prefix misses (evidence_against_prefix3/4, not counting results "
            "at the cap) count as evidence against the prefix."
        ),
    }


async def ansiblex_check(
    db: Database, chars: CharacterService, esi: EsiClient, owners: dict[int, int], limit: int = 3
) -> dict[str, Any]:
    """Does the Ansiblex search (term " » ") work live? Its count next to the gates stored for up to 3 characters."""
    from .ansiblex import SEARCH_TERM

    rows = db.query(
        "SELECT character_id, COUNT(*) FROM ansiblex_access GROUP BY character_id ORDER BY 2 DESC, 1 LIMIT ?", (limit,)
    )
    out: dict[str, Any] = {"term": SEARCH_TERM, "characters": []}
    if not rows:
        out["note"] = "no character has stored Ansiblex gates: nothing to compare the live search with"
        return out
    for cid, stored in rows:
        item: dict[str, Any] = {"character_id": cid, "stored_gates": stored}
        if cid not in owners:
            item["skipped"] = "not registered with both search scopes"
        else:
            try:
                token = await chars.access_token(cid, owners[cid])
                ids = await search_structure_ids(esi, cid, token, SEARCH_TERM, cache=False)
                item["search_count"] = len(ids)
                item["works"] = len(ids) > 0
            except (EsiError, SsoError) as exc:
                item["error"] = str(exc)[:120]
        out["characters"].append(item)
    tested = [c for c in out["characters"] if "works" in c]
    if tested and not any(c["works"] for c in tested):
        out["note"] = 'the " » " search returned nothing although gates are stored: it does not seem to work live'
    return out
