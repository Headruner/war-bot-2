"""Command line: ``evedm serve | rank | db | users | wars | universe | import | export | check | ...``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

from . import __version__
from .config import ConfigError, Settings, load_settings

SDE_ORIGIN = "https://developers.eveonline.com/static-data/tranquility"


def _settings(args: argparse.Namespace) -> Settings:
    s = load_settings(getattr(args, "config", None))
    if getattr(args, "data_dir", None):
        s.data_dir = Path(args.data_dir)
    if getattr(args, "demo", False):
        s.demo = True
        if not os.environ.get("EVEDM_WEB_AUTH"):
            s.web.auth = "none"
        if not s.secret_key:
            s.secret_key = "demo-secret-key-change-me"
    return s


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app

    s = _settings(args)
    if args.host:
        s.web.host = args.host
    if args.port:
        s.web.port = args.port
    try:
        app = create_app(s)
    except ConfigError as exc:
        print(f"evedm: {exc}", file=sys.stderr)
        return 2
    try:
        import uvloop  # noqa: F401

        loop = "uvloop"
    except ImportError:
        loop = "asyncio"
    uvicorn.run(
        app,
        host=s.web.host,
        port=s.web.port,
        workers=1,
        loop=loop,
        http="httptools",
        access_log=False,
        log_config=None,
        timeout_keep_alive=5,
        backlog=256,
        limit_concurrency=200,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def _services(s: Settings):  # noqa: ANN202
    from .web.app import build_services

    if not s.secret_key:
        raise ConfigError("EVEDM_SECRET_KEY is required")
    return build_services(s)


def cmd_rank(args: argparse.Namespace) -> int:
    s = _settings(args)
    svc = _services(s)

    async def run() -> dict:
        try:
            idx = svc.graph.find(args.target)
            if idx is None:
                raise SystemExit(f"unknown solar system: {args.target}")
            return await svc.ranking.rank(idx, refresh=0 if args.no_refresh else 2, owner_id=args.owner)
        finally:
            await svc.http.aclose()

    result = asyncio.run(run())
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    t = result["target"]
    print(f"Target: {t['name']} ({t['security']}, {t['region']})")
    print(f"{'#':>2}  {'Character':<18} {'Jumps':>5}  {'System':<16} {'Ship':<22} State")
    for n, r in enumerate(result["rows"], 1):
        jumps = str(r["jumps"]) if r["reachable"] else "—"
        system = r["system"]["name"] if r["system"] else "unknown"
        ship = r.get("ship_type") or "n/a"
        state = ("docked" if r.get("docked") else "in space") + (" *closest*" if r["recommended"] else "")
        print(f"{n:>2}  {r['name']:<18} {jumps:>5}  {system:<16} {ship:<22} {state}")
    for e in result["excluded"]:
        print(f"    excluded: {e['name']} ({e['reason']})")
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    from .crypto import TokenCipher
    from .db import Database
    from .demo import seed_demo
    from .universe import GateGraph

    s = _settings(args)
    db = Database(s.db_path, s.sqlite_journal)
    if args.action == "init":
        print(f"database ready: {s.db_path} (journal={db.journal_mode})")
    elif args.action == "seed-demo":
        if not s.secret_key:
            raise ConfigError("EVEDM_SECRET_KEY is required to encrypt demo tokens")
        n = seed_demo(db, TokenCipher(s.secret_key), GateGraph.load(s.universe_file), force=args.force)
        print(f"seeded {n} demo characters into {s.db_path}")
    db.close()
    return 0


def cmd_universe(args: argparse.Namespace) -> int:
    from .universe import GateGraph, build_from_jsonl_zip, build_from_legacy_tree, write_universe

    out = Path(args.out)
    if args.action == "fetch":
        latest = urllib.request.urlopen(f"{SDE_ORIGIN}/latest.jsonl", timeout=30).read().decode()  # noqa: S310
        build = next(
            json.loads(line)["buildNumber"]
            for line in latest.splitlines()
            if line.strip() and json.loads(line).get("_key") == "sde"
        )
        zip_path = out.parent / f"sde-{build}-jsonl.zip"
        if not zip_path.exists():
            print(f"downloading SDE build {build} ...")
            urllib.request.urlretrieve(  # noqa: S310
                f"{SDE_ORIGIN}/eve-online-static-data-{build}-jsonl.zip", zip_path
            )
        raw = build_from_jsonl_zip(zip_path, build=str(build))
    elif args.jsonl:
        raw = build_from_jsonl_zip(args.jsonl, build=args.build or Path(args.jsonl).stem)
    elif args.legacy:
        raw = build_from_legacy_tree(args.legacy, build=args.build or "legacy")
    else:
        print("use --jsonl ZIP or --legacy DIR (or 'evedm universe fetch')", file=sys.stderr)
        return 2
    print(json.dumps(write_universe(raw, out)))
    g = GateGraph.load(out)
    print(f"ok: {len(g.ids)} systems, {g.edge_count} gate links, build {g.build}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from .bundle import BundleRejected, import_bundle, report

    svc = _services(_settings(args))

    async def run() -> dict:
        try:
            return report(await import_bundle(Path(args.file).read_bytes(), svc.chars, args.owner))
        finally:
            await svc.http.aclose()

    try:
        print(json.dumps(asyncio.run(run()), indent=2))
    except BundleRejected as exc:
        print(json.dumps({"rejected": exc.reason, "errors": exc.errors}, indent=2))
        return 1
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .bundle import export_bundle

    svc = _services(_settings(args))
    doc = export_bundle(svc.chars, owner_id=args.owner)
    Path(args.out).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"exported {len(doc['characters'])} characters to {args.out} (contains credentials!)")
    return 0


def cmd_users(args: argparse.Namespace) -> int:
    from .db import Database

    s = _settings(args)
    db = Database(s.db_path, s.sqlite_journal)
    try:
        if args.action == "list":
            local = len(db.list_characters(0))
            print(json.dumps({"user_id": 0, "name": "(local / unassigned)", "characters": local}))
            for row in db.list_users():
                print(json.dumps(row))
        elif args.action == "adopt":
            if not args.to or db.get_user(args.to) is None:
                print("evedm: --to must be the character ID of a main that has signed in once", file=sys.stderr)
                return 2
            n = db.reassign_owner(args.owner, args.to)
            print(f"moved {n} characters from owner {args.owner} to main {args.to}")
    finally:
        db.close()
    return 0


def cmd_launches(args: argparse.Namespace) -> int:
    from .db import Database

    s = _settings(args)
    for row in Database(s.db_path, s.sqlite_journal).launches(args.limit):
        print(json.dumps(row))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .selfcheck import check

    report = check(_settings(args), data=not args.no_data)
    print(json.dumps(report.as_dict(), indent=2))
    return 0 if report.ok else 1


def cmd_healthcheck(args: argparse.Namespace) -> int:
    port = args.port or int(os.environ.get("EVEDM_PORT", "8080"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:  # noqa: S310
            ok = resp.status == 200 and json.loads(resp.read()).get("status") == "ok"
    except OSError:
        ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evedm", description="EVE Distance Meter")
    p.add_argument("--version", action="version", version=f"evedm {__version__}")
    p.add_argument("--config", help="YAML config file (default: $EVEDM_CONFIG)")
    p.add_argument("--data-dir", help="data directory (DB, logs); default: $EVEDM_DATA_DIR or ./data")
    p.add_argument("--demo", action="store_true", help="demo mode: seeded characters, no EVE application needed")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the web app")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("rank", help="rank characters for a target system (console)")
    sp.add_argument("target")
    sp.add_argument("--no-refresh", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--owner", type=int, default=0, help="main character ID (default 0: local characters)")
    sp.set_defaults(fn=cmd_rank)

    sp = sub.add_parser("users", help="mains and their linked characters")
    sp.add_argument("action", choices=["list", "adopt"])
    sp.add_argument("--owner", type=int, default=0, help="adopt: move characters from this owner (default 0)")
    sp.add_argument("--to", type=int, help="adopt: main character ID that receives them")
    sp.set_defaults(fn=cmd_users)

    sp = sub.add_parser("db", help="database tasks")
    sp.add_argument("action", choices=["init", "seed-demo"])
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_db)

    sp = sub.add_parser("universe", help="build the universe file from CCP static data")
    sp.add_argument("action", choices=["build", "fetch"])
    sp.add_argument("--jsonl", help="SDE JSON Lines zip (current format)")
    sp.add_argument("--legacy", help="legacy SDE directory (YAML tree)")
    sp.add_argument("--build", help="build label stored in the file")
    sp.add_argument("--out", default="data/universe.json.gz")
    sp.set_defaults(fn=cmd_universe)

    sp = sub.add_parser("import", help="import a character bundle (JSON)")
    sp.add_argument("file")
    sp.add_argument("--owner", type=int, default=0, help="main character ID that receives the characters")
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("export", help="export characters to a bundle (contains credentials)")
    sp.add_argument("--out", default="evedm-characters.json")
    sp.add_argument("--owner", type=int, default=0, help="main character ID whose characters are exported")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("launches", help="show recorded launches")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_launches)

    sp = sub.add_parser("check", help="verify dependencies and resource paths (exit 1 on problems)")
    sp.add_argument("--no-data", action="store_true", help="skip the data directory (image build time)")
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("healthcheck", help="exit 0 when the local server is healthy (Docker HEALTHCHECK)")
    sp.add_argument("--port", type=int)
    sp.set_defaults(fn=cmd_healthcheck)

    args = p.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except ConfigError as exc:
        print(f"evedm: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
