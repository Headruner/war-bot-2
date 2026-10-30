"""Public structure ownership from the EVE Ref structures dataset.

The dataset (structures_url) maps structure_id -> {name, owner_id, type_id, solar_system_id, ...}; names look like
"Jita - Trade Hub". Only *public* structures are listed, so "none known" is common. Systems, security and regions
come from the bundled universe (no ESI calls), so every system can be ranked against right away.
"""

from __future__ import annotations

import codecs
import json
import logging
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from ..universe import GateGraph
from .esi import WarEsi, WarEsiError

log = logging.getLogger("evedm.wars.structures")

TOP_CORPS = 5  # alliance summary: corporations listed individually
MAX_DATASET_BYTES = 256 * 1024 * 1024  # refuse anything larger than this (the dataset is a few MB)
HIGHSEC_MIN = 0.45  # EVE shows security rounded to one decimal; 0.45 and up is high-sec


@dataclass(frozen=True)
class Structure:
    structure_id: int
    name: str
    type_id: int
    system_id: int | None
    owner_id: int


class StreamingItems:
    """Incremental parser for a JSON document that is one top-level object ({"id": {...}, ...}) or array.

    Bytes are fed in chunks as they arrive; complete members come out as (key, value) pairs (key None for arrays).
    Only one member is held in memory at a time, so a large dataset never has to be loaded whole (the bot used
    ijson for this; the standard library's raw_decode does the same job here without a dependency).
    """

    _WS = " \t\r\n"

    def __init__(self) -> None:
        self._dec = codecs.getincrementaldecoder("utf-8-sig")()
        self._json = json.JSONDecoder()
        self._buf = ""
        self._pos = 0
        self._state = "start"  # start | key | colon | value | sep | done
        self._array = False
        self._key: str | None = None

    def _skip_ws(self) -> None:
        while self._pos < len(self._buf) and self._buf[self._pos] in self._WS:
            self._pos += 1

    def _decode(self, final: bool) -> tuple[Any, bool]:
        """Decode one JSON value at the cursor; (value, True) or (None, False) when more input is needed."""
        try:
            value, end = self._json.raw_decode(self._buf, self._pos)
        except json.JSONDecodeError:
            if final:
                raise
            return None, False
        if end >= len(self._buf) and not final:  # a number or literal may continue in the next chunk
            return None, False
        self._pos = end
        return value, True

    def feed(self, chunk: bytes, final: bool = False) -> Iterator[tuple[str | None, Any]]:
        self._buf += self._dec.decode(chunk, final)
        while True:
            self._skip_ws()
            if self._pos >= len(self._buf):
                break
            ch = self._buf[self._pos]
            if self._state == "start":
                if ch not in "{[":
                    raise ValueError("structure dataset: expected a JSON object or array")
                self._array = ch == "["
                self._pos += 1
                self._state = "value" if self._array else "key"
                self._skip_ws()
                if self._pos < len(self._buf) and self._buf[self._pos] in "}]":
                    self._pos += 1
                    self._state = "done"
            elif self._state == "key":
                key, ok = self._decode(final)
                if not ok:
                    break
                if not isinstance(key, str):
                    raise ValueError("structure dataset: object key expected")
                self._key, self._state = key, "colon"
            elif self._state == "colon":
                if ch != ":":
                    raise ValueError("structure dataset: ':' expected")
                self._pos += 1
                self._state = "value"
            elif self._state == "value":
                value, ok = self._decode(final)
                if not ok:
                    break
                yield (None if self._array else self._key), value
                self._state = "sep"
            elif self._state == "sep":
                self._pos += 1
                if ch == ",":
                    self._state = "value" if self._array else "key"
                elif ch in "}]":
                    self._state = "done"
                else:
                    raise ValueError(f"structure dataset: unexpected {ch!r}")
            else:  # done: only whitespace may follow
                raise ValueError("structure dataset: data after the end of the document")
        if self._pos > 65536:  # drop consumed text
            self._buf, self._pos = self._buf[self._pos :], 0
        if final and self._state != "done":
            raise ValueError("structure dataset: truncated document")


def _structure(key: str | None, item: Any) -> Structure | None:
    if not isinstance(item, dict):
        return None
    owner, type_id = item.get("owner_id"), item.get("type_id")
    if not (owner and type_id):
        return None
    try:
        sid = int(item.get("structure_id") or key or 0)
        system = int(item["solar_system_id"]) if item.get("solar_system_id") else None
        return Structure(sid, str(item.get("name") or f"Structure {sid}"), int(type_id), system, int(owner))
    except (TypeError, ValueError):
        return None  # malformed entry: skip it, keep the rest


def add_items(index: dict[int, list[Structure]], items: Iterator[tuple[str | None, Any]]) -> None:
    for key, item in items:
        st = _structure(key, item)
        if st is not None:
            index.setdefault(st.owner_id, []).append(st)


def build_index(raw: bytes | str, chunk_size: int = 65536) -> dict[int, list[Structure]]:
    """owner corporation id -> structures, from the v2 dataset (a JSON object keyed by structure id)."""
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    parser, index = StreamingItems(), {}
    for start in range(0, max(len(data), 1), chunk_size):
        add_items(index, parser.feed(data[start : start + chunk_size]))
    add_items(index, parser.feed(b"", final=True))
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
            index: dict[int, list[Structure]] = {}
            parser = StreamingItems()
            size = 0
            async with httpx.AsyncClient(
                timeout=60, headers={"User-Agent": self.user_agent}, follow_redirects=True, transport=self.transport
            ) as client:
                async with client.stream("GET", self.structures_url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(65536):  # parsed as it arrives: low peak memory
                        size += len(chunk)
                        if size > MAX_DATASET_BYTES:
                            raise ValueError(f"structure dataset larger than {MAX_DATASET_BYTES} bytes")
                        add_items(index, parser.feed(chunk))
            add_items(index, parser.feed(b"", final=True))
            self.index = index
            self.last_refresh = now
            log.info("structures: %d owners with public structures loaded (%d KB)", len(index), size // 1024)
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
