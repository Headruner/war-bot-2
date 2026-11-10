"""Local structure index: the flat list built by ``scripts/build_structure_index.py``.

``data/structure-index.jsonl`` holds one JSON object per line and one line per structure (or NPC station) ID, already
merged from the EVE Ref scrapes, the Adam4EVE lists and the CCP Static Data Export (newest observation wins).
Lines are sorted by ID and start with ``{"structure_id":<id>,`` so a lookup of a few IDs reads the file once without
parsing the lines it does not need and stops as soon as it is past the largest wanted ID.

Fields: structure_id, kind (structure | station), name, owner_id, owner_name, solar_system_id, solar_system_name,
region_id, region_name, type_id, type_name, position {x, y, z}, observed (UTC), first_seen, source.

The file is never required: without it the lookup returns nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

INDEX_NAME = "structure-index.jsonl"
_PREFIX = '{"structure_id":'


def index_path(data_dir: Path, configured: str = "") -> Path:
    """``structures.index_file`` when set, else ``<data dir>/structure-index.jsonl``."""
    return Path(configured).expanduser() if configured else Path(data_dir) / INDEX_NAME


def lookup(path: Path, wanted: set[int]) -> list[dict[str, Any]]:
    """The records of ``wanted`` IDs found in the index file; [] when the file or the IDs are missing."""
    if not wanted or not path.is_file():
        return []
    last = max(wanted)
    found: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            sid: int | None = None
            if line.startswith(_PREFIX):
                head = line[len(_PREFIX) : line.find(",")]
                sid = int(head) if head.isdecimal() else None
            if sid is None:  # not written by the build script: parse it
                try:
                    sid = int(json.loads(line)["structure_id"])
                except (ValueError, KeyError, TypeError):
                    continue
            if sid > last:
                break  # sorted by ID: nothing further can match
            if sid in wanted:
                found.append(json.loads(line))
    return found
