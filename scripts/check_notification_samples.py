"""Runs the notification parsers over real samples (data/notification-samples.jsonl, made by
scripts/export-notification-samples.ps1) and reports what they extract and what they miss.

    .venv\\Scripts\\python scripts\\check_notification_samples.py [file] [--type STRUCTURE_...] [--show]

Exit code 1 when a body names a structure but no structure ID is found.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from evedm.notifications import (  # noqa: E402
    parse_body,
    structure_of,
)

NAMED = ("structureID:", "structureLink:", "structureShowInfoData:")  # keys that carry a structure


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(ROOT / "data" / "notification-samples.jsonl"))
    ap.add_argument("--type", action="append", help="only these notification types")
    ap.add_argument("--show", action="store_true", help="print each parsed sample")
    args = ap.parse_args()
    path = Path(args.file)
    if not path.exists():
        print(f"{path} not found: run scripts/export-notification-samples.ps1 first", file=sys.stderr)
        return 2
    rows = [r for r in load(path) if not args.type or r["type"] in args.type]
    keys: dict[str, Counter] = defaultdict(Counter)
    problems: list[str] = []
    counts = Counter(r["type"] for r in rows)
    for r in rows:
        body = parse_body(r["body"] or "")
        keys[r["type"]].update(body.keys())
        struct = structure_of(r["type"], body)
        if args.show:
            print(r["type"], r["timestamp"], "structure", struct)
        if any(k in (r["body"] or "") for k in NAMED) and struct[0] is None:
            problems.append(f"{r['type']} #{r['id']}: a structure is named but no ID found (keys: {sorted(body)})")
    for t, n in counts.most_common():
        print(f"{t}: {n}")
        print("   keys:", ", ".join(f"{k}({c})" for k, c in sorted(keys[t].items())))
    print()
    for p in problems[:50]:
        print("MISSING", p)
    print(f"{len(rows)} notifications, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
