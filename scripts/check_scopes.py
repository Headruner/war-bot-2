#!/usr/bin/env python3
"""Read-only check of the ESI scope settings of an evedm install.

Usage (from the repo root):
  python scripts/check_scopes.py [--env .env] [--db data/evedm.sqlite3] [--config config/evedm.yaml]

Checks: the scopes the app will request (.env / YAML override / defaults) against the nine evedm needs,
the SSO basics (client id, callback URL), and which stored characters lack which scope.
Prints no secrets and writes nothing. Exit code 0 = OK, 1 = problems found.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REQUIRED = [
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-location.read_online.v1",
    "esi-characters.read_notifications.v1",
]
OPTIONAL = [
    "esi-universe.read_structures.v1",
    "esi-assets.read_assets.v1",
    "esi-search.search_structures.v1",
    "esi-mail.read_mail.v1",
    "esi-ui.write_waypoint.v1",
]
FEATURE = {
    "esi-characters.read_notifications.v1": "Notifications page",
    "esi-universe.read_structures.v1": "structure scout",
    "esi-assets.read_assets.v1": "structure scout",
    "esi-search.search_structures.v1": "Ansiblex routes / dock search",
    "esi-mail.read_mail.v1": "structures named in mails",
    "esi-ui.write_waypoint.v1": "Set destination",
}
problems: list[str] = []


def ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def bad(msg: str) -> None:
    problems.append(msg)
    print(f"  [FAIL] {msg}")


def warn(msg: str) -> None:
    print(f"  [warn] {msg}")


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--db", default="data/evedm.sqlite3")
    ap.add_argument("--config", default="config/evedm.yaml")
    a = ap.parse_args()
    env = read_env(Path(a.env))

    print("1. SSO basics")
    if env.get("EVE_CLIENT_ID"):
        ok("EVE_CLIENT_ID is set")
    else:
        bad("EVE_CLIENT_ID is empty: sign-in is impossible")
    if env.get("EVE_CLIENT_SECRET"):
        ok("EVE_CLIENT_SECRET is set")
    else:
        bad("EVE_CLIENT_SECRET is empty")
    cb = env.get("EVE_CALLBACK_URL", "")
    port = env.get("EVEDM_PUBLISH", "8080")
    if cb:
        ok(f"callback URL {cb} (must be registered on the EVE application exactly like this)")
        if "localhost" in cb and f":{port}/" not in cb:
            bad(f"callback port does not match EVEDM_PUBLISH={port}")
    else:
        warn("EVE_CALLBACK_URL not set: the default http://localhost:8080/sso/callback is used")

    print("2. Scopes the app will request")
    optional = list(OPTIONAL)
    source = "default list"
    cfg = Path(a.config)
    if cfg.is_file():
        try:
            import yaml  # type: ignore

            raw = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("eve", {}) or {}
            if "optional_scopes" in raw:
                optional, source = list(raw["optional_scopes"] or []), f"{cfg}"
            if "required_scopes" in raw:
                REQUIRED[:] = list(raw["required_scopes"] or [])
        except ImportError:
            warn(f"{cfg} exists but PyYAML is not installed here: its eve.* scopes were not checked")
    val = env.get("EVE_OPTIONAL_SCOPES", "")
    if val:
        parts = val.replace(",", " ").split()
        optional = [] if [p.lower() for p in parts] == ["none"] else parts
        source = "EVE_OPTIONAL_SCOPES in .env"
    print(f"     optional scopes come from: {source}")
    requested = list(dict.fromkeys([*REQUIRED, *optional]))
    for s in [*REQUIRED, *OPTIONAL]:
        if s in requested:
            ok(f"{s}")
        else:
            bad(f"{s} is NOT requested ({FEATURE.get(s, 'required')}): characters will never get it")
    for s in requested:
        if s not in REQUIRED and s not in OPTIONAL:
            warn(f"{s} is requested but evedm does not use it")
    print("     -> every requested scope must also be ticked on developers.eveonline.com,")
    print("        otherwise EVE SSO answers invalid_scope (this script cannot see that setting).")

    print("3. Stored characters")
    dbp = Path(a.db)
    if not dbp.is_file():
        warn(f"{dbp} not found: skipped")
    else:
        con = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select owner_id, character_id, name, status, scopes from characters where demo = 0"
            ).fetchall()
        except sqlite3.Error as exc:
            rows = []
            warn(f"could not read characters: {exc}")
        wanted = [s for s in requested if s in FEATURE or s in REQUIRED]
        per_owner: dict[int, list] = defaultdict(list)
        for r in rows:
            per_owner[r[0]].append(r)
        for owner, chars in sorted(per_owner.items()):
            missing: dict[str, list[str]] = defaultdict(list)
            for _owner, _cid, name, _status, scopes in chars:
                have = set(json.loads(scopes or "[]"))
                for s in wanted:
                    if s not in have:
                        missing[s].append(name)
            head = f"main {owner}: {len(chars)} characters"
            if not missing:
                ok(head + ", all hold every requested scope")
                continue
            bad(head + f", {len({n for v in missing.values() for n in v})} are missing scopes")
            for s, names in missing.items():
                print(f"         {s} ({FEATURE.get(s, 'required')}): {len(names)} missing, e.g. {', '.join(names[:3])}")
        by_status = defaultdict(int)
        for r in rows:
            by_status[r[3]] += 1
        print(f"     statuses: {dict(by_status)}")
        print("     fix: sign in as the main, Characters -> Add character (re-authorize) once per character.")

    print()
    if problems:
        print(f"RESULT: {len(problems)} problem(s) found")
        return 1
    print("RESULT: settings look OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
