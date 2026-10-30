"""Public structure ownership from the EVE Ref structures dataset.

The dataset (structures_url) maps structure_id -> {name, owner_id, type_id, solar_system_id, ...}; names look like
"Jita - Trade Hub". Only *public* structures are listed, so "none known" is common. Systems, security and regions
come from the bundled universe (no ESI calls), so every system can be ranked against right away.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx

from ..universe import GateGraph
from .esi import WarEsi, WarEsiError

log = logging.getLogger("evedm.wars.structures")

TOP_CORPS = 5  # alliance summary: corporations listed individually
HIGHSEC_MIN = 0.45  # EVE shows security rounded to one decimal; 0.45 and up is high-sec


@dataclass(frozen=True)
class Structure:
    structure_id: int
    name: str
    type_id: int
    system_id: int | None
    owner_id: int


def build_index(raw: bytes | str) -> dict[int, list[Structure]]:
    """owner corporation id -> structures, from the v2 dataset (a JSON object keyed by structure id)."""
    doc = json.loads(raw)
    items = doc.items() if isinstance(doc, dict) else ((str(x.get("structure_id")), x) for x in doc)
    index: dict[int, list[Structure]] = {}
    for key, item in items:
        owner, type_id = item.get("owner_id"), item.get("type_id")
        if not (owner and type_id):
            continue
        sid = int(item.get("structure_id") or key)
        index.setdefault(int(owner), []).append(
            Structure(
                structure_id=sid,
                name=str(item.get("name") or f"Structure {sid}"),
                type_id=int(type_id),
                system_id=int(item["solar_system_id"]) if item.get("solar_system_id") else None,
                owner_id=int(owner),
            )
        )
    return index


class StructureIndex:
    def __init__(
        self,
        graph: GateGraph,
        structures_url: str,
        user_agent: str,
        refresh_hours: float = 24,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.g = graph
        self.structures_url = structures_url
        self.user_agent = user_agent
        self.refresh_interval = float(refresh_hours) * 3600
        self.transport = transport
        self.index: dict[int, list[Structure]] | None = None
        self.last_refresh = 0.0

    @property
    def loaded(self) -> bool:
        return self.index is not None

    @property
    def owners(self) -> int:
        return len(self.index or {})

    async def refresh_if_due(self) -> None:
        now = time.time()
        if (self.loaded and now - self.last_refresh < self.refresh_interval) or not self.structures_url:
            return
        try:
            async with httpx.AsyncClient(
                timeout=60, headers={"User-Agent": self.user_agent}, follow_redirects=True, transport=self.transport
            ) as client:
                response = await client.get(self.structures_url)
                response.raise_for_status()
            self.index = build_index(response.content)
            self.last_refresh = now
            log.info("structures: %d owners with public structures loaded", len(self.index))
        except Exception as e:  # noqa: BLE001 - keep the previous index, retry next cycle
            log.warning("could not refresh the structure dataset: %s", e)

    def owned_by(self, corp_ids: list[int]) -> dict[int, list[Structure]]:
        return {c: self.index[c] for c in corp_ids if self.index and c in self.index}

    def _system(self, system_id: int | None) -> dict[str, Any] | None:
        idx = self.g.index.get(system_id) if system_id else None
        if idx is None:
            return None
        d = self.g.describe(idx)
        d["highsec"] = self.g.sec[idx] >= HIGHSEC_MIN
        return d

    async def summarize(self, esi: WarEsi, party: Any) -> dict[str, Any] | None:
        """Counts by type and corporation, plus every structure with its system. None if unknown."""
        if not self.loaded or not getattr(party, "entity_id", None):
            return None
        if party.type == "alliance":
            try:
                corp_ids = await esi.alliance_corporations(party.entity_id) or []
            except WarEsiError as e:
                log.debug("alliance corporations for %s failed: %s", party.entity_id, e)
                return None
        else:
            corp_ids = [party.entity_id]
        per_corp = self.owned_by([int(c) for c in corp_ids])
        structures = [st for sts in per_corp.values() for st in sts]
        corps = sorted(((c, len(sts)) for c, sts in per_corp.items()), key=lambda x: -x[1])
        items = []
        for st in structures:
            system = self._system(st.system_id)
            name = st.name
            if system and name.startswith(f"{system['name']} - "):  # "System - Structure" -> "Structure"
                name = name.split(" - ", 1)[1]
            items.append({"name": name, "type_id": st.type_id, "owner_id": st.owner_id, "system": system})
        items.sort(key=lambda x: ((x["system"] or {}).get("name", "~"), x["name"]))
        return {
            "total": len(structures),
            "by_type": dict(Counter(st.type_id for st in structures)),
            "corps": corps[:TOP_CORPS],
            "corps_with_structures": len(corps),
            "corp_count": len(corp_ids),
            "structures": items,
            "highsec": [
                {
                    "system": i["system"]["name"],
                    "security": i["system"]["security"],
                    "name": i["name"],
                    "type_id": i["type_id"],
                }
                for i in items
                if i["system"] and i["system"]["highsec"]
            ],
        }


def systems_of(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Distinct systems holding structures, most structures first: the targets offered on the Wars page."""
    if not summary:
        return []
    count: Counter[int] = Counter()
    info: dict[int, dict[str, Any]] = {}
    for item in summary.get("structures", []):
        if item["system"]:
            count[item["system"]["id"]] += 1
            info[item["system"]["id"]] = item["system"]
    return [
        info[sid] | {"structures": n} for sid, n in sorted(count.items(), key=lambda kv: (-kv[1], info[kv[0]]["name"]))
    ]
