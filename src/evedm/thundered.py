"""Import characters and refresh tokens from a ThunderED database (SQLite dump of the previous app).

ThunderED keeps one plain-text EVE SSO refresh token per character *and purpose* in ``tokens`` (type 0 general,
1 mail, 2 notifications, 4 contracts, 6 structures, 11 standings), each with its own scope list. evedm keeps one
encrypted token per character link, so the token whose scopes cover most of what evedm asks for is taken:
required scopes first, then optional ones, then the widest list.

Nothing is sent to EVE here: the token is stored without an access token, so the first use refreshes it. A token
that EVE refuses marks the character REAUTH (TOK-04), the same as any expired grant.

The tokens belong to ThunderED's EVE application, not evedm's: they are stored with the legacy client
(``eve.legacy_client_id`` / ``eve.legacy_client_secret``, entered on the Settings page) and refresh with it. Imported
before it is entered, they carry the marker ``legacy`` and wait (error ``legacy_client_missing``, no REAUTH).
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .crypto import TokenCipher, tail
from .db import Database
from .sso import LEGACY_CLIENT

LIMITED, OK = "LIMITED", "OK"
TAG = "thundered"


@dataclass
class LegacyToken:
    character_id: int
    token_type: int
    refresh_token: str
    scopes: list[str]


@dataclass
class ImportEntry:
    character_id: int
    name: str
    result: str  # Added | Limited | Skipped
    token_type: int | None = None
    scopes: int = 0
    missing_scopes: list[str] = field(default_factory=list)
    reason: str = ""


def read_legacy(path: str | Path) -> tuple[list[LegacyToken], dict[int, str]]:
    """Tokens and the character names ThunderED knows (only its registered users carry a name)."""
    src = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "tokens" not in tables:
            raise ValueError(f"{path}: no 'tokens' table, not a ThunderED database")
        tokens = [
            LegacyToken(int(cid), int(typ), str(tok).strip(), [s for s in (scopes or "").split(",") if s.strip()])
            for cid, typ, tok, scopes in src.execute(
                "SELECT character_id, type, token, scopes FROM tokens WHERE token IS NOT NULL AND token <> ''"
            )
        ]
        names: dict[int, str] = {}
        if "auth_users" in tables:
            cols = {r[1] for r in src.execute("PRAGMA table_info(auth_users)")}
            if "character_name" in cols:
                names = {
                    int(cid): str(name)
                    for cid, name in src.execute("SELECT characterID, character_name FROM auth_users")
                    if name
                }
        return tokens, names
    finally:
        src.close()


def best_tokens(tokens: list[LegacyToken], required: list[str], optional: list[str]) -> dict[int, LegacyToken]:
    """Per character, the token whose scopes serve evedm best."""

    def score(t: LegacyToken) -> tuple[int, int, int, int]:
        have = set(t.scopes)
        return (sum(s in have for s in required), sum(s in have for s in optional), len(have), -t.token_type)

    best: dict[int, LegacyToken] = {}
    for t in tokens:
        if t.character_id not in best or score(t) > score(best[t.character_id]):
            best[t.character_id] = t
    return best


def import_legacy(
    path: str | Path, db: Database, cipher: TokenCipher, settings: Settings, owner_id: int, dry_run: bool = False
) -> list[ImportEntry]:
    """Add every ThunderED character to ``owner_id``; a character this main already has is left alone."""
    tokens, names = read_legacy(path)
    required, optional = settings.eve.required_scopes, settings.eve.optional_scopes
    known = db.character_names([t.character_id for t in tokens])  # names other mains already resolved
    out: list[ImportEntry] = []
    for cid, tok in sorted(best_tokens(tokens, required, optional).items()):
        name = names.get(cid) or known.get(cid) or f"Character {cid}"  # the first refresh stores the EVE name
        missing = [s for s in required if s not in tok.scopes]
        result = "Limited" if missing else "Added"
        entry = ImportEntry(cid, name, result, tok.token_type, len(tok.scopes), missing)
        if db.get_character(cid, owner_id) is not None:
            entry.result, entry.reason = "Skipped", "already linked to this main"
        elif not tok.scopes:  # a ThunderED sign-in token: it can read nothing from ESI
            entry.result, entry.reason = "Skipped", "token has no ESI scopes: the player must sign in once"
        elif not dry_run:
            db.upsert_character(cid, name, LIMITED if missing else OK, tok.scopes, tags=[TAG], owner_id=owner_id)
            db.save_tokens(
                cid,
                settings.eve.legacy_client_id or LEGACY_CLIENT,
                None,
                0.0,  # no access token: the first use refreshes
                cipher.encrypt(tok.refresh_token),
                tail(tok.refresh_token)[1:],
                owner_id=owner_id,
            )
        out.append(entry)
    return out


def report(entries: list[ImportEntry], dry_run: bool) -> dict[str, Any]:
    results = ("Added", "Limited", "Skipped")
    return {
        "dry_run": dry_run,
        "summary": {r: sum(1 for e in entries if e.result == r) for r in results},
        "entries": [asdict(e) for e in entries],
    }
