#!/usr/bin/env bash
# Runs the test suites and keeps their logs:
#   logs/tests/<UTC timestamp>-<suite>.log   full pytest output
#   logs/tests/<UTC timestamp>-<suite>.xml   JUnit report
#   logs/tests/test-runs.jsonl               one summary line per pytest session (written by tests/conftest.py)
# Usage: scripts/run-tests.sh [unit|integration|perf|all]   (default: all)
set -uo pipefail
cd "$(dirname "$0")/.."
SUITE="${1:-all}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p logs/tests
status=0
run_suite() {
  local name="$1"; shift
  echo "== $name"
  python -m pytest "$@" --junitxml="logs/tests/$STAMP-$name.xml" 2>&1 | tee "logs/tests/$STAMP-$name.log"
  local rc="${PIPESTATUS[0]}"
  [ "$rc" -eq 0 ] || status="$rc"
}
case "$SUITE" in
  unit|all) run_suite unit tests/unit -m "not integration and not perf" ;;&
  integration|all) run_suite integration tests/integration -m integration ;;&
  perf|all) run_suite perf tests/perf -m perf -s ;;
esac
exit "$status"
