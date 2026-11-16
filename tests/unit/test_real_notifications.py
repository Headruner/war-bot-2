"""Parsers against real notifications exported from a running container (scripts/export-notification-samples.ps1).
Skipped when data/notification-samples.jsonl is absent (it is git-ignored: it holds real IDs)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from evedm.notifications import DAYS_LEFT_TYPES, SAFETY_TYPES, days_left_of, parse_body, safety_of

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "notification-samples.jsonl"
LINES = SAMPLES.read_text(encoding="utf-8").splitlines() if SAMPLES.exists() else []
ROWS = [json.loads(x) for x in LINES if x.strip()]

pytestmark = pytest.mark.skipif(not ROWS, reason="no exported notification samples")


@pytest.mark.parametrize("row", [r for r in ROWS if r["type"] in SAFETY_TYPES], ids=lambda r: str(r["id"]))
def test_asset_safety_moments_found(row):
    sent = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
    assert safety_of(parse_body(row["body"]), sent)


@pytest.mark.parametrize("row", [r for r in ROWS if r["type"] in DAYS_LEFT_TYPES], ids=lambda r: str(r["id"]))
def test_days_left_found(row):
    assert days_left_of(parse_body(row["body"])) is not None
