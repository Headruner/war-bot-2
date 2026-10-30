"""Budget check for a 2 vCPU / 1 GB RAM VM (marker: perf). Writes a report to logs/perf/."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf
SCRIPT = Path(__file__).with_name("footprint.py")


def test_local_process_stays_within_budget():
    # 50 characters, 600 requests at concurrency 20, 40 ms fake ESI latency, server pinned to 2 CPUs.
    # Budget: peak RSS <= 160 MB (16 % of 1 GB), p95 <= 1000 ms, no errors.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--label", "pytest-local", "--max-rss", "160", "--max-p95", "1000"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    print(result.stdout[-3000:])
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
