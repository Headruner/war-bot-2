"""Parsers against real notifications exported from a running container (scripts/export-notification-samples.ps1).
Skipped when data/notification-samples.jsonl is absent (it is git-ignored: it holds real IDs)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evedm.notifications import parse_body, structure_of

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "notification-samples.jsonl"
LINES = SAMPLES.read_text(encoding="utf-8").splitlines() if SAMPLES.exists() else []
ROWS = [json.loads(x) for x in LINES if x.strip()]

NAMED = ("structureID:", "structureLink:", "structureShowInfoData:")  # keys that carry a structure

pytestmark = pytest.mark.skipif(not ROWS, reason="no exported notification samples")


@pytest.mark.parametrize(
    "row", [r for r in ROWS if any(k in (r["body"] or "") for k in NAMED)], ids=lambda r: str(r["id"])
)
def test_a_body_that_names_a_structure_gives_its_id(row):
    assert structure_of(row["type"], parse_body(row["body"]))[0] is not None
