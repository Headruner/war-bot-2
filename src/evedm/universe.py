"""Universe data: building it from CCP's static data (SDE) and routing over the stargate graph.

The app ships a compact, pre-built file (``data/universe.json.gz``) so it starts without
downloading the SDE. ``evedm universe build`` / ``evedm universe fetch`` regenerate it from
either the current SDE (JSON Lines zip from developers.eveonline.com) or the legacy YAML tree.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import sys
import zipfile
from array import array
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

LY = 9.4607e15  # metres per light year
UNIVERSE_FORMAT = 1
SHIP_CATEGORY_ID = 6
CAPSULE_TYPE_IDS = frozenset({670, 33328})

# Region IDs used to orient the 2D projection (north = Caldari, west = Gallente).
_THE_FORGE, _DOMAIN, _SINQ_LAISON, _HEIMATAR = 10000002, 10000043, 10000032, 10000030


# --------------------------------------------------------------------------- security
def security_display(sec: float) -> float:
    """Security status as the game shows it (one decimal; tiny positives show as 0.1)."""
    if 0.0 < sec < 0.05:
        return 0.1
    return math.floor(sec * 10 + 0.5) / 10


def security_class(sec: float) -> str:
    shown = security_display(sec)
    if shown >= 0.5:
        return "high"
    if shown > 0.0:
        return "low"
    return "null"


# --------------------------------------------------------------------------- building
@dataclass
class RawUniverse:
    systems: dict[int, dict[str, Any]]  # id -> {name, region, sec, x, y, z}
    edges: set[tuple[int, int]]
    regions: dict[int, str]
    ships: dict[int, str]
    source: str
    build: str


def _name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("en") or next(iter(value.values()), ""))
    return str(value)


def _xyz(value: Any) -> tuple[float, float, float]:
    if isinstance(value, dict):
        return float(value["x"]), float(value["y"]), float(value["z"])
    x, y, z = value
    return float(x), float(y), float(z)


def _iter_jsonl(fh: io.TextIOBase | Iterable[str]) -> Iterator[dict[str, Any]]:
    for line in fh:
        line = line.strip()
        if line:
            yield json.loads(line)


def build_from_jsonl_zip(zip_path: str | Path, build: str = "") -> RawUniverse:
    """Current SDE format: one ``<table>.jsonl`` per table, rows ``{"_key": id, ...}``."""
    with zipfile.ZipFile(zip_path) as zf:
        members = {Path(n).name: n for n in zf.namelist()}

        def table(name: str) -> Iterator[dict[str, Any]]:
            member = members.get(f"{name}.jsonl")
            if member is None:
                raise FileNotFoundError(f"{name}.jsonl not found in {zip_path}")
            with zf.open(member) as raw:
                yield from _iter_jsonl(io.TextIOWrapper(raw, encoding="utf-8"))

        regions = {int(r["_key"]): _name(r.get("name", r["_key"])) for r in table("mapRegions")}
        systems: dict[int, dict[str, Any]] = {}
        for row in table("mapSolarSystems"):
            sid = int(row["_key"])
            x, y, z = _xyz(row["position"])
            sec = row.get("securityStatus", row.get("security", 0.0))
            systems[sid] = {
                "name": _name(row["name"]),
                "region": int(row["regionID"]),
                "sec": float(sec),
                "x": x,
                "y": y,
                "z": z,
            }
        edges: set[tuple[int, int]] = set()
        for gate in table("mapStargates"):
            a = int(gate["solarSystemID"])
            dest = gate.get("destination") or {}
            b = int(dest.get("solarSystemID", 0))
            if a in systems and b in systems and a != b:
                edges.add((min(a, b), max(a, b)))
        ship_groups = {int(g["_key"]) for g in table("groups") if int(g.get("categoryID", -1)) == SHIP_CATEGORY_ID}
        ships = {
            int(t["_key"]): _name(t.get("name", "")) for t in table("types") if int(t.get("groupID", -1)) in ship_groups
        }
    known = {k: v for k, v in systems.items() if 30000000 <= k < 32000000}  # k-space + w-space
    edges = {e for e in edges if e[0] in known and e[1] in known}
    return RawUniverse(known, edges, regions, ships, "sde-jsonl", build)


def build_from_legacy_tree(sde_dir: str | Path, build: str = "") -> RawUniverse:
    """Legacy SDE format (YAML tree: ``fsd/universe/eve/<Region>/<Const>/<System>/solarsystem.yaml``)."""
    import yaml

    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    root = Path(sde_dir)
    base = root / "universe" if (root / "universe").is_dir() else root / "fsd" / "universe"
    space_dirs = [base / "eve", base / "wormhole"]  # k-space with gates + w-space (named, no gates)
    names: dict[int, str] = {}
    inv_names = root / "bsd" / "invNames.yaml"
    if inv_names.is_file():
        for row in yaml.load(inv_names.read_text(encoding="utf-8"), Loader=loader):  # noqa: S506
            item = int(row["itemID"])
            if 30000000 <= item < 31000000 or 10000000 <= item < 11000000:
                names[item] = str(row["itemName"])
    systems: dict[int, dict[str, Any]] = {}
    regions: dict[int, str] = {}
    gate_owner: dict[int, int] = {}
    gate_dest: dict[int, int] = {}
    region_dirs = sorted(p for d in space_dirs if d.is_dir() for p in d.iterdir() if p.is_dir())
    for region_dir in region_dirs:
        region = yaml.load((region_dir / "region.yaml").read_text(encoding="utf-8"), Loader=loader)  # noqa: S506
        rid = int(region["regionID"])
        regions[rid] = names.get(rid, region_dir.name)
        for ss_file in region_dir.glob("*/*/solarsystem.yaml"):
            data = yaml.load(ss_file.read_text(encoding="utf-8"), Loader=loader)  # noqa: S506
            sid = int(data["solarSystemID"])
            x, y, z = _xyz(data["center"])
            systems[sid] = {
                "name": names.get(sid, ss_file.parent.name),
                "region": rid,
                "sec": float(data.get("security", 0.0)),
                "x": x,
                "y": y,
                "z": z,
            }
            for gid, gate in (data.get("stargates") or {}).items():
                gate_owner[int(gid)] = sid
                gate_dest[int(gid)] = int(gate["destination"])
    edges: set[tuple[int, int]] = set()
    for gid, dest_gate in gate_dest.items():
        a, b = gate_owner[gid], gate_owner.get(dest_gate)
        if b is not None and a != b:
            edges.add((min(a, b), max(a, b)))
    ships: dict[int, str] = {}
    groups_file, types_file = root / "fsd" / "groups.yaml", root / "fsd" / "types.yaml"
    if groups_file.is_file() and types_file.is_file():
        groups = yaml.load(groups_file.read_text(encoding="utf-8"), Loader=loader)  # noqa: S506
        ship_groups = {int(k) for k, g in groups.items() if int(g.get("categoryID", -1)) == SHIP_CATEGORY_ID}
        types = yaml.load(types_file.read_text(encoding="utf-8"), Loader=loader)  # noqa: S506
        ships = {int(k): _name(t.get("name", "")) for k, t in types.items() if int(t.get("groupID", -1)) in ship_groups}
    return RawUniverse(systems, edges, regions, ships, "sde-legacy-yaml", build)


def _orientation(raw: RawUniverse) -> tuple[int, int]:
    """Return (sx, sz) signs so that The Forge is north of Domain and Sinq Laison west of Heimatar."""

    def centroid(region_id: int) -> tuple[float, float] | None:
        pts = [(s["x"], s["z"]) for sid, s in raw.systems.items() if s["region"] == region_id and sid < 31000000]
        if not pts:
            return None
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)

    sx = sz = 1
    forge, domain = centroid(_THE_FORGE), centroid(_DOMAIN)
    sinq, heim = centroid(_SINQ_LAISON), centroid(_HEIMATAR)
    if forge and domain and forge[1] < domain[1]:
        sz = -1
    if sinq and heim and sinq[0] > heim[0]:
        sx = -1
    return sx, sz


def write_universe(raw: RawUniverse, out_path: str | Path) -> dict[str, Any]:
    """Write the compact gzip JSON used at runtime. Map coordinates are light years, north up."""
    sx, sz = _orientation(raw)
    systems = [
        [
            sid,
            s["name"],
            s["region"],
            round(s["sec"], 4),
            round(sx * s["x"] / LY, 2),
            round(sz * s["z"] / LY, 2),
        ]
        for sid, s in sorted(raw.systems.items())
    ]
    used_regions = {s["region"] for s in raw.systems.values()}
    doc = {
        "format": UNIVERSE_FORMAT,
        "source": raw.source,
        "build": raw.build,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regions": {str(k): v for k, v in sorted(raw.regions.items()) if k in used_regions},
        "systems": systems,
        "edges": sorted([list(e) for e in raw.edges]),
        "ships": {str(k): v for k, v in sorted(raw.ships.items())},
    }
    data = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(out, "wb", mtime=0) as fh:
        fh.write(data)
    return {"systems": len(systems), "edges": len(doc["edges"]), "ships": len(doc["ships"])}


# --------------------------------------------------------------------------- runtime graph
class GateGraph:
    """Stargate graph with index-based adjacency for fast BFS (one search serves every character)."""

    def __init__(self, doc: dict[str, Any]):
        if doc.get("format") != UNIVERSE_FORMAT:
            raise ValueError(f"unsupported universe format {doc.get('format')!r}")
        self.source: str = doc.get("source", "")
        self.build: str = str(doc.get("build", ""))
        self.regions: dict[int, str] = {int(k): v for k, v in doc["regions"].items()}
        self.ships: dict[int, str] = {int(k): v for k, v in doc.get("ships", {}).items()}
        rows = doc["systems"]
        n = len(rows)
        # compact storage (arrays, CSR adjacency): ~4x less memory than lists of Python objects
        self.ids = array("l", (r[0] for r in rows))
        self.names: list[str] = [sys.intern(r[1]) for r in rows]
        self.region_of = array("l", (r[2] for r in rows))
        self.sec = array("d", (r[3] for r in rows))
        self.mx = array("d", (r[4] for r in rows))
        self.my = array("d", (r[5] for r in rows))
        self.index: dict[int, int] = {sid: i for i, sid in enumerate(self.ids)}
        self.by_name: dict[str, int] = {nm.lower(): i for i, nm in enumerate(self.names)}
        degree = [0] * n
        pairs = []
        for a, b in doc["edges"]:
            ia, ib = self.index.get(a), self.index.get(b)
            if ia is None or ib is None:
                continue
            pairs.append((ia, ib))
            degree[ia] += 1
            degree[ib] += 1
        self.adj_off = array("l", [0]) * (n + 1)
        for i in range(n):
            self.adj_off[i + 1] = self.adj_off[i] + degree[i]
        self.adj_nb = array("l", [0]) * self.adj_off[n]
        fill = array("l", self.adj_off[:-1])
        for ia, ib in pairs:
            self.adj_nb[fill[ia]] = ib
            fill[ia] += 1
            self.adj_nb[fill[ib]] = ia
            fill[ib] += 1
        self.edge_count = len(pairs)
        self._map_payload: bytes | None = None
        self._map_etag = ""

    # -- loading -----------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> GateGraph:
        if path is None:
            raw = resources.files("evedm").joinpath("data/universe.json.gz").read_bytes()
        else:
            raw = Path(path).read_bytes()
        return cls(json.loads(gzip.decompress(raw)))

    # -- lookups -----------------------------------------------------------------------
    def find(self, name: str) -> int | None:
        return self.by_name.get(name.strip().lower())

    def system_id(self, i: int) -> int:
        return self.ids[i]

    def describe(self, i: int) -> dict[str, Any]:
        return {
            "id": self.ids[i],
            "name": self.names[i],
            "region": self.regions.get(self.region_of[i], ""),
            "security": security_display(self.sec[i]),
            "class": security_class(self.sec[i]),
        }

    # -- routing (RTE-01..RTE-04) -------------------------------------------------------
    def bfs(self, target: int, avoid: Iterable[int] = ()) -> array:
        """Breadth-first search from ``target``.

        Returns ``parent`` where ``parent[i]`` is the next hop from ``i`` toward the target,
        ``parent[target] == target`` and ``-1`` marks systems with no stargate path.
        Avoided systems are never used as transit nodes but remain valid origins/targets.
        """
        parent = array("i", [-1]) * len(self.ids)
        blocked = set(avoid)
        blocked.discard(target)
        parent[target] = target
        queue = deque([target])
        off, nb = self.adj_off, self.adj_nb
        while queue:
            node = queue.popleft()
            if node in blocked:
                continue
            for k in range(off[node], off[node + 1]):
                nxt = nb[k]
                if parent[nxt] == -1:
                    parent[nxt] = node
                    queue.append(nxt)
        return parent

    def neighbors(self, i: int) -> list[int]:
        return list(self.adj_nb[self.adj_off[i] : self.adj_off[i + 1]])

    @staticmethod
    def route(parent: array, origin: int) -> list[int] | None:
        """Ordered system indexes from origin to target, or None when unreachable."""
        if parent[origin] == -1:
            return None
        path = [origin]
        while parent[path[-1]] != path[-1]:
            path.append(parent[path[-1]])
        return path

    def route_summary(self, path: list[int]) -> dict[str, int]:
        counts = {"high": 0, "low": 0, "null": 0}
        for i in path:
            counts[security_class(self.sec[i])] += 1
        return counts

    # -- map payload for the browser ------------------------------------------------------
    def is_wspace(self, i: int) -> bool:
        return self.ids[i] >= 31000000

    def map_payload(self) -> tuple[bytes, str]:
        """Gzip JSON for the 2D star map, k-space only (cached; the browser caches it by ETag)."""
        if self._map_payload is None:
            shown = [i for i in range(len(self.ids)) if not self.is_wspace(i)]
            region_ids = sorted({self.region_of[i] for i in shown})
            region_index = {rid: n for n, rid in enumerate(region_ids)}
            sums: dict[int, list[float]] = {}
            for i in shown:
                rid = self.region_of[i]
                acc = sums.setdefault(rid, [0.0, 0.0, 0])
                acc[0] += self.mx[i]
                acc[1] += self.my[i]
                acc[2] += 1
            regions = [
                [
                    self.regions.get(rid, str(rid)),
                    round(sums[rid][0] / sums[rid][2], 1),
                    round(sums[rid][1] / sums[rid][2], 1),
                ]
                for rid in region_ids
            ]
            edges: list[int] = []
            pos = {i: n for n, i in enumerate(shown)}
            for i in shown:
                for j in self.neighbors(i):
                    if i < j and j in pos:
                        edges.extend((pos[i], pos[j]))
            doc = {
                "build": self.build,
                "ids": [self.ids[i] for i in shown],
                "systems": [
                    [
                        self.names[i],
                        self.mx[i],
                        self.my[i],
                        security_display(self.sec[i]),
                        region_index[self.region_of[i]],
                    ]
                    for i in shown
                ],
                "edges": edges,
                "regions": regions,
            }
            raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self._map_payload = gzip.compress(raw, compresslevel=9, mtime=0)
            self._map_etag = '"' + hashlib.sha256(self._map_payload).hexdigest()[:20] + '"'
        return self._map_payload, self._map_etag
