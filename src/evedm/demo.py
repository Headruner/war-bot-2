"""Demo data: fictional characters at real systems, so the app runs without an EVE application."""

from __future__ import annotations

import json
import time
from typing import Any

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


# (name, system, tags, note) - a static roster for demo mode: no tokens, no ESI (UC-12)
DEMO_ROSTER = [
    ("Rook Tarsis", "Amarr", ["corp"], "parked"),
    ("Vey Alland", "Dodixie", ["hauler"], ""),
    ("Sora Kell", "Perimeter", [], ""),
    ("Dax Morrow", "Old Man Star", ["pvp"], ""),
    ("Irel Voss", "Rens", [], ""),
    ("Juno Sabre", "Tama", ["scout"], ""),
    ("Kael Varn", "Jita", [], "same name as a linked character"),
    ("Pax Ilun", "Jit", [], "misspelt on purpose: unresolved, with suggestions"),
]


def seed_demo_roster(roster: Any, owner_id: int = 0) -> int:
    """Import the demo roster once (idempotent). Returns the number of entries added."""
    if roster.db.count_roster(owner_id):
        return 0
    doc = {
        "schema": "evedm-static-roster",
        "version": 1,
        "label": "Demo roster",
        "characters": [
            {"char_name": name, "star_system_name": system}
            | ({"tags": tags} if tags else {})
            | ({"note": note} if note else {})
            for name, system, tags, note in DEMO_ROSTER
        ],
    }
    return int(roster.import_file(json.dumps(doc).encode("utf-8"), owner_id)["summary"]["Added"])


# (character_id, type, system, minutes ago) - sample notifications for demo mode (UC-13)
DEMO_NOTIFICATIONS = [
    (3900000001, "StructureItemsMovedToSafety", "Hek", 8),
    (3900000002, "StructureUnderAttack", "Amamake", 23),
    (3900000003, "StructureLostShields", "Amamake", 41),
    (3900000001, "StructureFuelAlert", "Perimeter", 95),
    (3900000004, "TowerAlertMsg", "Tama", 150),
    (3900000002, "StructureImpendingAbandonmentAssetsAtRisk", "Rens", 400),
    (3900000003, "InsurancePayoutMsg", "Jita", 600),  # filtered out by default: not a structure type
]


def seed_demo_notifications(db: Database, graph: GateGraph) -> int:
    """Insert the sample notifications once (demo mode only). Returns how many were inserted."""
    if db.query("SELECT 1 FROM character_notifications LIMIT 1"):
        return 0
    now = time.time()
    rows = []
    for n, (cid, ntype, system, minutes) in enumerate(DEMO_NOTIFICATIONS, start=1):
        idx = graph.find(system)
        if idx is None:
            continue
        when = now - minutes * 60
        rows.append(
            {
                "character_id": cid,
                "notification_id": 9_000_000 + n,
                "type": ntype,
                "sender_id": 1000132,
                "sender_type": "corporation",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
                "body": f"solarsystemID: {graph.ids[idx]}\nstructureID: 1035000000001\nstructureTypeID: 35832\n",
                "solar_system_id": graph.ids[idx],
                "structure_id": 1035000000001,
                "structure_type_id": 35832,
            }
        )
    return db.save_notifications(rows)
