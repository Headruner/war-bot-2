"""Public structure ownership from the EVE Ref structures dataset.

The dataset (structures_url, ~1-2k public Upwell structures) maps
structure_id -> {name, owner_id, type_id, solar_system_id, ...}; names look
like "Jita - Trade Hub". It is streamed with ijson into an owner -> structures
index and refreshed periodically. Only *public* structures are listed there,
so "none known" is common.
"""
from __future__ import annotations
import io
import logging
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
import ijson

from .esi import EsiClient, EsiError

log = logging.getLogger(__name__)

TOP_CORPS = 5  # alliance summary: corporations listed individually


@dataclass(frozen=True)
class Structure:
    structure_id: int
    name: str
    type_id: int
    system_id: Optional[int]
    owner_id: int


def make_structures(s: Dict, creds: Dict, store: Any) -> "StructureIndex":
    contact = creds.get("contact") or "no-contact"
    user_agent = f"eve-war-bot ({contact})"
    return StructureIndex(store, s.get("structures_url"), user_agent, s.get("structures_refresh_hours", 24))


def build_index(stream: Any) -> Dict[int, List[Structure]]:
    """owner corporation id -> structures, from the v2 dataset (a JSON object)."""
    index: Dict[int, List[Structure]] = {}
    for key, item in ijson.kvitems(stream, ""):
        owner, type_id = item.get("owner_id"), item.get("type_id")
        if not (owner and type_id):
            continue
        sid = int(item.get("structure_id") or key)
        index.setdefault(int(owner), []).append(Structure(
            structure_id=sid,
            name=str(item.get("name") or f"Structure {sid}"),
            type_id=int(type_id),
            system_id=int(item["solar_system_id"]) if item.get("solar_system_id") else None,
            owner_id=int(owner),
        ))
    return index


class StructureIndex:
    def __init__(self, db, structures_url: str, user_agent: str, refresh_hours: int = 24):
        self.db = db
        self.structures_url = structures_url
        self.user_agent = user_agent
        self.refresh_interval = float(refresh_hours) * 3600
        self.index: Optional[Dict[int, List[Structure]]] = None
        self.last_refresh: float = 0.0

    @property
    def loaded(self) -> bool:
        return self.index is not None

    async def refresh_if_due(self) -> None:
        now = time.time()
        if self.loaded and now - self.last_refresh < self.refresh_interval:
            return
        if not self.structures_url:
            return
        try:
            async with httpx.AsyncClient(timeout=60, headers={"User-Agent": self.user_agent},
                                         follow_redirects=True) as client:
                response = await client.get(self.structures_url)
                response.raise_for_status()
            self.index = build_index(io.BytesIO(response.content))
            self.last_refresh = now
            log.info("Structures: %d owners with public structures loaded", len(self.index))
        except Exception as e:  # keep the previous index, retry next cycle
            log.warning("Could not refresh structure dataset: %s", e)

    def owned_by(self, corp_ids: List[int]) -> Dict[int, List[Structure]]:
        return {c: self.index[c] for c in corp_ids if self.index and c in self.index}

    async def summarize(self, esi: EsiClient, party: Any) -> Optional[Dict[str, Any]]:
        """{'total', 'by_type', 'corps': [(corp_id, n)], 'structures': [Structure], ...}.

        None when the dataset is not loaded or the party is unknown.
        """
        if not self.loaded or not getattr(party, "entity_id", None):
            return None
        if party.type == "alliance":
            try:
                corp_ids = await esi.alliance_corporations(party.entity_id) or []
            except EsiError as e:
                log.debug("alliance corps for %s failed: %s", party.entity_id, e)
                return None
        else:
            corp_ids = [party.entity_id]
        per_corp = self.owned_by([int(c) for c in corp_ids])
        structures = [st for sts in per_corp.values() for st in sts]
        corps = sorted(((c, len(sts)) for c, sts in per_corp.items()), key=lambda x: -x[1])
        return {
            "total": len(structures),
            "by_type": dict(Counter(st.type_id for st in structures)),
            "corps": corps[:TOP_CORPS],
            "corps_with_structures": len(corps),
            "corp_count": len(corp_ids),
            "structures": structures,
        }
