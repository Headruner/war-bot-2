#!/usr/bin/env python3
"""Download the public structure datasets and merge them into one flat list for the application.

Everything is written below the data folder (``data/`` by default, which git and Docker already ignore):

    data/structure-sources/adam4eve/*.csv                 every CSV of https://static.adam4eve.eu/IDs/
    data/structure-sources/everef/structures-latest.v2.json
    data/structure-sources/everef/history/<year>/...      every file of https://data.everef.net/structures/history/<year>/
    data/structure-sources/sde/eve-online-static-data-<build>-jsonl.zip   CCP Static Data Export (latest build)
    data/structure-index.jsonl                            the result: one JSON object per line, one line per ID
    data/structure-index.meta.json                        when it was built and what went in

A structure ID found in several files is merged into ONE record: the newest observation wins field by field
(source priority breaks ties) and older observations only fill fields the newest one lacks. System, region, type
and NPC-corporation names are then completed from the Static Data Export.

The application (``evedm.structure_index``) reads the result when a structure ID stays unresolved.

Usage (standard library only, Python 3.10+):

    python scripts/build_structure_index.py            # download everything, then build
    python scripts/build_structure_index.py download   # only download (resumable: finished files are skipped)
    python scripts/build_structure_index.py build      # only merge what is already on disk
    python scripts/build_structure_index.py --year 2025 --only everef,sde
"""

from __future__ import annotations

import argparse
import bz2
import csv
import datetime as dt
import gzip
import json
import os
import re
import sys
import tarfile
import time
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin
from urllib.request import Request, urlopen

ADAM_URL = "https://static.adam4eve.eu/IDs/"
EVEREF_LATEST = "https://data.everef.net/structures/structures-latest.v2.json"
EVEREF_HISTORY = "https://data.everef.net/structures/history/{year}/"
SDE_LATEST = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
SDE_ZIP = "https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-{build}-jsonl.zip"
UTC = dt.timezone.utc  # noqa: UP017 - dt.UTC needs Python 3.11; this script also runs on 3.10
USER_AGENT = "evedm-structure-index/1.0 (EVE Distance Meter; local dataset build)"

INDEX_NAME = "structure-index.jsonl"
META_NAME = "structure-index.meta.json"
SOURCES_DIR = "structure-sources"

# higher wins when two observations carry the same time
PRIORITY = {"EVE Ref latest": 4, "EVE Ref history": 3, "Adam4EVE extended": 2, "Adam4EVE": 1}
FIELDS = (
    "name",
    "owner_id",
    "owner_name",
    "solar_system_id",
    "region_id",
    "type_id",
    "type_name",
    "position",
)
OUTPUT_ORDER = (
    "structure_id",
    "kind",
    "name",
    "owner_id",
    "owner_name",
    "solar_system_id",
    "solar_system_name",
    "region_id",
    "region_name",
    "type_id",
    "type_name",
    "position",
    "observed",
    "first_seen",
    "source",
)


def log(message: str) -> None:
    print(message, flush=True)


# ----------------------------------------------------------------------------------------------- time
def norm_time(value: Any) -> str | None:
    """Any ISO-like time or date -> ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); None if empty, invalid or the 1970 placeholder."""
    if not value:
        return None
    text = str(value).strip()
    if text.startswith("1970-01-01") or len(text) < 10:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})[_T ](\d{2})[-:](\d{2})[-:](\d{2})")


def stamp_from_name(name: str, fallback: float | None = None) -> str | None:
    """The scrape time carried by a file name such as ``structures-2026-03-31_04-34-43.v2.json.bz2``."""
    m = _STAMP.search(name)
    if m:
        return "{}-{}-{}T{}:{}:{}Z".format(*m.groups())
    if fallback is not None:
        return dt.datetime.fromtimestamp(fallback, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def text_or_none(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


# ------------------------------------------------------------------------------------------------- HTTP
class Fetcher:
    """urllib with retries (429 and 5xx, honouring Retry-After), conditional GETs and atomic file writes."""

    def __init__(self, retries: int = 5, timeout: float = 120.0, delay: float = 0.0):
        self.retries = retries
        self.timeout = timeout
        self.delay = delay

    def open(self, url: str, headers: dict[str, str] | None = None):
        head = {"User-Agent": USER_AGENT, "Accept": "*/*"} | (headers or {})
        for attempt in range(self.retries + 1):
            try:
                if self.delay:
                    time.sleep(self.delay)
                return urlopen(Request(url, headers=head), timeout=self.timeout)  # noqa: S310 - http(s) URLs only
            except HTTPError as exc:
                if exc.code == 304:
                    raise
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise
                wait = to_int(exc.headers.get("Retry-After")) if exc.headers else None
                time.sleep(min(wait if wait is not None else 2**attempt, 120))
            except (URLError, TimeoutError, ConnectionError):
                if attempt == self.retries:
                    raise
                time.sleep(min(2**attempt, 60))
        raise RuntimeError("unreachable")

    def get_bytes(self, url: str) -> bytes:
        with self.open(url) as response:
            return response.read()

    def download(self, url: str, dest: Path, *, refresh: bool = False) -> str:
        """Returns ``downloaded``, ``skipped`` (already on disk) or ``not-modified`` (refresh found nothing newer).

        Without ``refresh`` an existing file is never fetched again (history files do not change). With it the file
        is asked for with If-Modified-Since, so an unchanged "latest" file costs no download.
        """
        headers: dict[str, str] = {}
        if dest.exists() and dest.stat().st_size > 0:
            if not refresh:
                return "skipped"
            headers["If-Modified-Since"] = formatdate(dest.stat().st_mtime, usegmt=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            with self.open(url, headers) as response:
                expected = to_int(response.headers.get("Content-Length"))
                size = 0
                with open(part, "wb") as out:
                    while chunk := response.read(1 << 20):
                        out.write(chunk)
                        size += len(chunk)
                if expected is not None and size != expected:
                    raise OSError(f"truncated download of {url}: {size} of {expected} bytes")
                modified = response.headers.get("Last-Modified")
        except HTTPError as exc:
            if exc.code == 304:
                return "not-modified"
            raise
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        os.replace(part, dest)
        if modified:
            try:
                stamp = parsedate_to_datetime(modified).timestamp()
                os.utime(dest, (stamp, stamp))
            except (TypeError, ValueError):
                pass
        return "downloaded"


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value)


def list_files(url: str, fetcher: Fetcher, *, _root: str | None = None, _depth: int = 0) -> list[str]:
    """Every file below an HTML directory listing, folders included (``href`` ending in ``/``)."""
    root = _root or url
    if _depth > 8:
        return []
    page = fetcher.get_bytes(url).decode("utf-8", "replace")
    parser = _Links()
    parser.feed(page)
    found: list[str] = []
    for href in parser.links:
        if href.startswith(("#", "?", "mailto:")):
            continue
        target = urljoin(url, href).split("#")[0].split("?")[0]
        if not target.startswith(url) or target == url:
            continue  # the parent link, another site, a sort link or the page itself: only entries below this folder
        if target.endswith("/"):
            found += list_files(target, fetcher, _root=root, _depth=_depth + 1)
        else:
            found.append(target)
    return sorted(set(found))


def local_path(root_url: str, file_url: str, base: Path) -> Path:
    """data/.../<path of the file below the listing URL>, never leaving ``base``."""
    relative = unquote(file_url[len(root_url) :]).lstrip("/")
    dest = (base / relative).resolve()
    if base.resolve() not in dest.parents:
        raise ValueError(f"refusing to write outside {base}: {file_url}")
    return dest


# ------------------------------------------------------------------------------------------- downloading
def download_many(
    fetcher: Fetcher, jobs: list[tuple[str, Path]], *, workers: int, refresh: bool = False, label: str = ""
) -> dict[str, int]:
    counts = {"downloaded": 0, "skipped": 0, "not-modified": 0, "failed": 0}

    def one(job: tuple[str, Path]) -> str:
        url, dest = job
        try:
            return fetcher.download(url, dest, refresh=refresh)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            log(f"  FAILED {url}: {exc}")
            return "failed"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, status in enumerate(pool.map(one, jobs), 1):
            counts[status] += 1
            if status == "downloaded" or n % 50 == 0 or n == len(jobs):
                log(f"  {label} {n}/{len(jobs)}  {counts}")
    return counts


def download_adam4eve(fetcher: Fetcher, base: Path, *, include_txt: bool) -> dict[str, int]:
    log(f"Adam4EVE: {ADAM_URL}")
    urls = [u for u in list_files(ADAM_URL, fetcher) if u.endswith(".csv") or (include_txt and u.endswith(".txt"))]
    jobs = [(u, local_path(ADAM_URL, u, base / "adam4eve")) for u in urls]
    # one at a time and slowly: the host answers 429 to bursts. The files change daily, so they are always refreshed
    slow = Fetcher(retries=fetcher.retries, timeout=fetcher.timeout, delay=1.0)
    return download_many(slow, jobs, workers=1, refresh=True, label="adam4eve")


def download_everef(
    fetcher: Fetcher, base: Path, *, year: int, workers: int, list_file: Path | None, history_url: str | None
) -> dict[str, int]:
    log(f"EVE Ref latest: {EVEREF_LATEST}")
    latest = download_many(
        fetcher,
        [(EVEREF_LATEST, base / "everef" / "structures-latest.v2.json")],
        workers=1,
        refresh=True,
        label="latest",
    )
    root = history_url or EVEREF_HISTORY.format(year=year)
    if list_file is not None:
        urls = sorted({ln.strip() for ln in list_file.read_text(encoding="utf-8").splitlines() if ln.strip()})
        log(f"EVE Ref history: {len(urls)} URLs from {list_file}")
    else:
        log(f"EVE Ref history: listing {root} (all folders)")
        urls = list_files(root, fetcher)
        log(f"  {len(urls)} files found")
    history_dir = base / "everef" / "history" / str(year)
    jobs = []
    for u in urls:
        try:
            jobs.append((u, local_path(root, u, history_dir)))
        except ValueError as exc:
            log(f"  skipped: {exc}")
    history = download_many(fetcher, jobs, workers=workers, label="history")
    return {k: latest[k] + history[k] for k in latest}


def download_sde(fetcher: Fetcher, base: Path) -> dict[str, int]:
    log(f"CCP Static Data Export: {SDE_LATEST}")
    first = fetcher.get_bytes(SDE_LATEST).decode("utf-8").strip().splitlines()[0]
    build = json.loads(first)["buildNumber"]
    dest = base / "sde" / f"eve-online-static-data-{build}-jsonl.zip"
    counts = download_many(fetcher, [(SDE_ZIP.format(build=build), dest)], workers=1, label=f"sde {build}")
    if counts["failed"] == 0:
        for old in dest.parent.glob("eve-online-static-data-*-jsonl.zip"):
            if old != dest:
                log(f"  removing the older SDE copy {old.name}")
                old.unlink()
    return counts


# --------------------------------------------------------------------------------------------- reading
def load_json_bytes(name: str, data: bytes) -> Any:
    lower = name.lower()
    if lower.endswith(".bz2"):
        data = bz2.decompress(data)
    elif lower.endswith(".gz"):
        data = gzip.decompress(data)
    return json.loads(data)


def _is_json_name(name: str) -> bool:
    return bool(re.search(r"\.json(\.bz2|\.gz)?$", name.lower()))


def iter_snapshots(directory: Path) -> Iterator[tuple[str, str, Any]]:
    """(scrape time, label, parsed JSON) of every history file, oldest first; tar archives member by member."""
    files = sorted(p for p in directory.rglob("*") if p.is_file() and not p.name.endswith(".part"))
    items: list[tuple[str, Path]] = [
        (stamp_from_name(p.name, p.stat().st_mtime) or "", p)
        for p in files
        if _is_json_name(p.name) or ".tar" in p.name
    ]
    for stamp, path in sorted(items, key=lambda it: (it[0], str(it[1]))):
        try:
            if ".tar" in path.name.lower():
                with tarfile.open(path) as tar:
                    members = sorted(
                        (m for m in tar.getmembers() if m.isfile() and _is_json_name(m.name)), key=lambda m: m.name
                    )
                    for member in members:
                        data = tar.extractfile(member)
                        if data is not None:
                            yield (
                                stamp_from_name(member.name, member.mtime) or stamp,
                                f"{path.name}:{member.name}",
                                load_json_bytes(member.name, data.read()),
                            )
            else:
                yield stamp, path.name, load_json_bytes(path.name, path.read_bytes())
        except (OSError, ValueError, tarfile.TarError, EOFError) as exc:
            log(f"  unreadable, skipped: {path.name} ({exc})")


def everef_records(data: Any, stamp: str | None, source: str) -> Iterator[dict[str, Any]]:
    """A scrape is an object keyed by structure ID whose values are ESI's structure answer plus scrape fields."""
    if not isinstance(data, dict):
        return
    for key, rec in data.items():
        sid = to_int(key)
        if sid is None or not isinstance(rec, dict):
            continue
        name, owner = text_or_none(rec.get("name")), to_int(rec.get("owner_id"))
        if not name and not owner:  # an ID the scrape never got details for tells nothing
            continue
        pos = rec.get("position")
        yield {
            "structure_id": sid,
            "kind": "structure",
            "name": name,
            "owner_id": owner,
            "owner_name": None,
            "solar_system_id": to_int(rec.get("solar_system_id")),
            "region_id": to_int(rec.get("region_id")),
            "type_id": to_int(rec.get("type_id")),
            "type_name": None,
            "position": pos if isinstance(pos, dict) and "x" in pos else None,
            "observed": norm_time(rec.get("last_structure_get")) or stamp,
            "first_seen": norm_time(rec.get("first_seen")),
            "source": source,
        }


def read_csv(path: Path) -> Iterator[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh, delimiter=";")


def adam4eve_records(directory: Path) -> Iterator[dict[str, Any]]:
    """playerStructure_extended.csv (dated), playerStructure_IDs.csv (undated), npcStation_ids.csv (stations)."""
    ext, ids, stations = (
        directory / n for n in ("playerStructure_extended.csv", "playerStructure_IDs.csv", "npcStation_ids.csv")
    )
    if ids.exists():
        for row in read_csv(ids):
            sid = to_int(row.get("structureID"))
            if sid is not None:
                yield _adam(sid, "structure", row, "Adam4EVE", observed=None, first=None)
    if ext.exists():
        for row in read_csv(ext):
            sid = to_int(row.get("structureID"))
            if sid is not None:
                yield _adam(
                    sid,
                    "structure",
                    row,
                    "Adam4EVE extended",
                    norm_time(row.get("lastSeen")),
                    norm_time(row.get("firstSeen")),
                )
    if stations.exists():
        for row in read_csv(stations):
            sid = to_int(row.get("stationID"))
            if sid is not None:
                row = {**row, "name": row.get("stationName", "")}
                yield _adam(sid, "station", row, "Adam4EVE", observed=None, first=None)


def _adam(
    sid: int, kind: str, row: dict[str, str], source: str, observed: str | None, first: str | None
) -> dict[str, Any]:
    return {
        "structure_id": sid,
        "kind": kind,
        "name": text_or_none(row.get("name")),
        "owner_id": to_int(row.get("ownerID")),
        "owner_name": text_or_none(row.get("ownerName")),
        "solar_system_id": to_int(row.get("solarSystemID")),
        "region_id": to_int(row.get("regionID")),
        "type_id": to_int(row.get("typeID")),
        "type_name": text_or_none(row.get("typeName")),
        "position": None,
        "observed": observed,
        "first_seen": first,
        "source": source,
    }


def _localized(value: Any) -> str | None:
    """SDE names are plain strings or ``{"en": ...}`` maps."""
    if isinstance(value, dict):
        value = value.get("en") or next(iter(value.values()), None)
    return text_or_none(value)


class Sde:
    """Names from the Static Data Export zip: solar systems (with region), regions, types, NPC corporations."""

    WANTED = ("mapSolarSystems", "mapRegions", "types", "npcCorporations")

    def __init__(self) -> None:
        self.systems: dict[int, tuple[str | None, int | None]] = {}
        self.regions: dict[int, str | None] = {}
        self.types: dict[int, str | None] = {}
        self.corporations: dict[int, str | None] = {}
        self.build: str | None = None

    @classmethod
    def load(cls, directory: Path) -> Sde:
        sde = cls()
        zips = sorted(directory.glob("eve-online-static-data-*-jsonl.zip"))
        if not zips:
            return sde
        path = zips[-1]
        m = re.search(r"-(\d+)-jsonl\.zip$", path.name)
        sde.build = m.group(1) if m else None
        with zipfile.ZipFile(path) as archive:
            members = {Path(n).name: n for n in archive.namelist()}
            for stem in cls.WANTED:
                member = members.get(f"{stem}.jsonl")
                if member is None:
                    continue
                with archive.open(member) as fh:
                    for line in fh:
                        if line.strip():
                            sde._add(stem, json.loads(line))
        return sde

    def _add(self, stem: str, rec: dict[str, Any]) -> None:
        key = to_int(rec.get("_key", rec.get("_id")))
        if key is None:
            return
        name = _localized(rec.get("name"))
        if stem == "mapSolarSystems":
            self.systems[key] = (name, to_int(rec.get("regionID")))
        elif stem == "mapRegions":
            self.regions[key] = name
        elif stem == "types":
            self.types[key] = name
        else:
            self.corporations[key] = name


# ------------------------------------------------------------------------------------------------ merge
def rank(rec: dict[str, Any]) -> tuple[str, int]:
    return (rec.get("observed") or "", PRIORITY.get(rec.get("source") or "", 0))


def merge_into(index: dict[int, dict[str, Any]], rec: dict[str, Any]) -> None:
    """Keep one record per ID: the newest observation wins; the other only fills what the winner lacks."""
    sid = rec["structure_id"]
    cur = index.get(sid)
    if cur is None:
        index[sid] = rec
        return
    winner, loser = (rec, cur) if rank(rec) >= rank(cur) else (cur, rec)
    for field in FIELDS:
        if winner.get(field) in (None, ""):
            winner[field] = loser.get(field)
    firsts = [t for t in (winner.get("first_seen"), loser.get("first_seen")) if t]
    winner["first_seen"] = min(firsts) if firsts else None
    if winner.get("kind") != loser.get("kind") and loser.get("kind") == "structure":
        winner["kind"] = "structure"
    index[sid] = winner


def enrich(index: dict[int, dict[str, Any]], sde: Sde) -> None:
    for rec in index.values():
        system = sde.systems.get(rec.get("solar_system_id") or -1)
        rec["solar_system_name"] = system[0] if system else None
        if system and system[1] and not rec.get("region_id"):
            rec["region_id"] = system[1]
        rec["region_name"] = sde.regions.get(rec.get("region_id") or -1)
        if not rec.get("type_name"):
            rec["type_name"] = sde.types.get(rec.get("type_id") or -1)
        if not rec.get("owner_name"):
            rec["owner_name"] = sde.corporations.get(rec.get("owner_id") or -1)


def build_index(base: Path, out_dir: Path) -> dict[str, Any]:
    started = time.time()
    index: dict[int, dict[str, Any]] = {}
    counts: dict[str, int] = {}

    def add(rec: dict[str, Any]) -> None:
        counts[rec["source"]] = counts.get(rec["source"], 0) + 1
        merge_into(index, rec)

    adam = base / "adam4eve"
    if adam.exists():
        log("merging Adam4EVE")
        for rec in adam4eve_records(adam):
            add(rec)
    history = base / "everef" / "history"
    if history.exists():
        log("merging EVE Ref history (oldest first)")
        n = 0
        for stamp, _label, data in iter_snapshots(history):
            n += 1
            for rec in everef_records(data, stamp, "EVE Ref history"):
                add(rec)
            if n % 25 == 0:
                log(f"  {n} snapshots, {len(index)} structures")
        counts["history snapshots"] = n
    # the downloaded copy, and one dropped by hand straight into the data folder (the newest observation wins anyway)
    for latest in (base / "everef" / "structures-latest.v2.json", out_dir / "structures-latest.v2.json"):
        if latest.is_file():
            log(f"merging EVE Ref latest: {latest}")
            stamp = stamp_from_name(latest.name, latest.stat().st_mtime)
            for rec in everef_records(json.loads(latest.read_bytes()), stamp, "EVE Ref latest"):
                add(rec)
    sde = Sde.load(base / "sde")
    log(f"enriching with the Static Data Export (build {sde.build or 'none found'})")
    enrich(index, sde)

    kept = [r for r in index.values() if r.get("name") or r.get("owner_id") or r.get("solar_system_id")]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / INDEX_NAME
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for rec in sorted(kept, key=lambda r: r["structure_id"]):
            # structure_id comes first so a reader can skip lines without parsing them
            fh.write(
                json.dumps({k: rec.get(k) for k in OUTPUT_ORDER}, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
    os.replace(tmp, target)
    meta = {
        "built_at": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "records": len(kept),
        "structures": sum(1 for r in kept if r["kind"] == "structure"),
        "stations": sum(1 for r in kept if r["kind"] == "station"),
        "observations_read": counts,
        "sde_build": sde.build,
        "seconds": round(time.time() - started, 1),
    }
    (out_dir / META_NAME).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {target} ({len(kept)} records)")
    return meta


# --------------------------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", choices=("all", "download", "build"), default="all")
    ap.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data")
    ap.add_argument("--year", type=int, default=dt.date.today().year, help="EVE Ref history year (default: this year)")
    ap.add_argument("--only", default="adam4eve,everef,sde", help="comma list of sources to download")
    ap.add_argument("--workers", type=int, default=4, help="parallel downloads of the EVE Ref history")
    ap.add_argument("--history-url", help="listing URL of the history folder (default: derived from --year)")
    ap.add_argument(
        "--list-file", type=Path, help="text file with one history file URL per line instead of the listing"
    )
    ap.add_argument("--with-txt", action="store_true", help="also download the .txt twins of the Adam4EVE CSVs")
    args = ap.parse_args(argv)

    base = args.data_dir / SOURCES_DIR
    failed = 0
    if args.action in ("all", "download"):
        fetcher = Fetcher()
        only = {s.strip() for s in args.only.split(",") if s.strip()}
        steps = {
            "adam4eve": lambda: download_adam4eve(fetcher, base, include_txt=args.with_txt),
            "everef": lambda: download_everef(
                fetcher,
                base,
                year=args.year,
                workers=args.workers,
                list_file=args.list_file,
                history_url=args.history_url,
            ),
            "sde": lambda: download_sde(fetcher, base),
        }
        for name in [n for n in steps if n in only]:
            try:
                failed += steps[name]()["failed"]
            except (HTTPError, URLError, OSError, ValueError, KeyError) as exc:
                log(f"{name}: FAILED: {exc}")
                failed += 1
    if args.action in ("all", "build"):
        build_index(base, args.data_dir)
    if failed:
        log(f"{failed} download(s) failed: run again, finished files are skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
