"""Demo data: fictional characters at real systems, so the app runs without an EVE application."""

from __future__ import annotations

import time

from .characters import CharacterService
from .crypto import TokenCipher
from .db import Database
from .universe import GateGraph

ALL_SCOPES = [
    "esi-location.read_location.v1",
    "esi-location.read_online.v1",
    "esi-location.read_ship_type.v1",
]

# (id, name, system, docked, ship_type_id, ship_name, online, last_logout, scopes, status, tags)
DEMO_CHARACTERS = [
    (
        3900000001,
        "Kael Varn",
        "Jita",
        True,
        22456,
        "Quick Exit",
        False,
        "2026-10-28T21:14:00Z",
        ALL_SCOPES,
        "OK",
        ["main", "fc"],
    ),
    (
        3900000002,
        "Mira Sunfall",
        "Perimeter",
        False,
        29990,
        "Lantern",
        False,
        "2026-10-29T02:40:00Z",
        ALL_SCOPES,
        "OK",
        ["pvp"],
    ),
    (
        3900000003,
        "Toro Ishida",
        "Uedama",
        True,
        24702,
        "Stormbreaker",
        False,
        "2026-10-27T18:05:00Z",
        ALL_SCOPES,
        "OK",
        ["pvp"],
    ),
    (
        3900000004,
        "Vex Aldane",
        "Dodixie",
        True,
        17478,
        "Rock Muncher",
        False,
        "2026-10-25T09:30:00Z",
        ALL_SCOPES,
        "OK",
        ["industry"],
    ),
    (
        3900000005,
        "Juno Krell",
        "Amarr",
        True,
        28659,
        "Lampwick",
        True,
        "2026-10-29T08:01:00Z",
        ALL_SCOPES,
        "OK",
        ["pvp", "logi"],
    ),
    (
        3900000006,
        "Ash Morrow",
        "Old Man Star",
        False,
        670,
        "Capsule",
        False,
        "2026-10-29T00:12:00Z",
        ALL_SCOPES,
        "OK",
        ["scout"],
    ),
    (
        3900000007,
        "Nyx Calder",
        "1DQ1-A",
        True,
        17738,
        "Night Shift",
        False,
        "2026-10-20T22:45:00Z",
        ALL_SCOPES,
        "OK",
        ["capital"],
    ),
    (
        3900000008,
        "Rho Senn",
        "J164710",
        False,
        33468,
        "Tunnel Rat",
        False,
        "2026-10-26T16:20:00Z",
        ALL_SCOPES,
        "OK",
        ["scout"],
    ),
    (
        3900000009,
        "Lena Orrin",
        "Hek",
        True,
        None,
        None,
        False,
        "2026-10-24T12:00:00Z",
        ["esi-location.read_location.v1", "esi-location.read_online.v1"],
        "LIMITED",
        ["alt"],
    ),
    (3900000010, "Old Alt", "Rens", True, 587, "Rifter", False, "2025-02-01T10:00:00Z", ALL_SCOPES, "REAUTH", []),
]


def seed_demo(
    db: Database, cipher: TokenCipher, graph: GateGraph, chars: CharacterService | None = None, force: bool = False
) -> int:
    """Insert the demo characters (idempotent unless force). Returns the number inserted."""
    if db.list_characters() and not force:
        return 0
    now = time.time()
    inserted = 0
    for cid, name, system, docked, ship_type, ship_name, online, logout, scopes, status, tags in DEMO_CHARACTERS:
        idx = graph.find(system)
        if idx is None:
            continue
        db.upsert_character(cid, name, status, scopes, demo=True, tags=tags)
        db.save_tokens(
            cid,
            "demo",
            cipher.encrypt("demo-access"),
            now + 3600 * 24 * 365,
            cipher.encrypt(f"demo-refresh-{cid}"),
            str(cid)[-4:],
        )
        db.save_snapshot(
            cid,
            solar_system_id=graph.ids[idx],
            station_id=60003760 if docked else None,
            structure_id=None,
            ship_type_id=ship_type,
            ship_item_id=None,
            ship_name=ship_name,
            online=int(online),
            last_login=None,
            last_logout=logout,
            fetched_at=now,
            error=None,
        )
        inserted += 1
    db.set_meta("demo_seeded_at", str(now))
    return inserted
