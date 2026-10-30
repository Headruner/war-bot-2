"""Characters and their tokens (SSO-03, SSO-05, SSO-06, TOK-01..TOK-05, CHR-01..CHR-04)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import Settings
from .crypto import TokenCipher, tail
from .db import Database
from .sso import AuthService, SsoError, TokenSet

log = logging.getLogger("evedm.characters")

OK, LIMITED, REAUTH, DISABLED = "OK", "LIMITED", "REAUTH", "DISABLED"


class CharacterService:
    def __init__(self, db: Database, cipher: TokenCipher, auth: AuthService | None, settings: Settings):
        self.db = db
        self.cipher = cipher
        self.auth = auth
        self.settings = settings
        self._locks: dict[int, asyncio.Lock] = {}

    # -- scopes ---------------------------------------------------------------------------------
    def missing_scopes(self, scopes: list[str]) -> list[str]:
        return [s for s in self.settings.eve.required_scopes if s not in scopes]

    def status_for(self, scopes: list[str]) -> str:
        return LIMITED if self.missing_scopes(scopes) else OK

    # -- storing tokens ---------------------------------------------------------------------------
    def store(self, ts: TokenSet, demo: bool = False) -> tuple[bool, str]:
        """Upsert the character and its encrypted tokens; returns (is_new, status)."""
        ident = ts.identity
        status = self.status_for(ident.scopes)
        is_new = self.db.upsert_character(
            ident.character_id, ident.name, status, ident.scopes, owner_hash=ident.owner_hash, demo=demo
        )
        self.db.save_tokens(
            ident.character_id,
            self.settings.eve.client_id,
            self.cipher.encrypt(ts.access_token),
            ts.expires_at,
            self.cipher.encrypt(ts.refresh_token),
            tail(ts.refresh_token)[1:],
        )
        log.info("stored tokens for character %s (%s, %s)", ident.character_id, "new" if is_new else "updated", status)
        return is_new, status

    # -- token lifecycle -----------------------------------------------------------------------------
    async def access_token(self, character_id: int) -> str:
        """Valid access token, refreshing when < 60 s remain (TOK-02); one refresh per character (TOK-03)."""
        lock = self._locks.setdefault(character_id, asyncio.Lock())
        async with lock:
            row = self.db.get_tokens(character_id)
            if row is None:
                raise SsoError("no_token")
            if row["access_token"] and row["access_expires_at"] - time.time() > 60:
                return self.cipher.decrypt(row["access_token"])
            if self.auth is None:
                raise SsoError("sso_disabled")
            refresh = self.cipher.decrypt(row["refresh_token"])
            try:
                ts = await self.auth.refresh(refresh)
            except SsoError as exc:
                if exc.code in ("invalid_grant", "invalid_token", "wrong_client"):
                    self.db.set_status(character_id, REAUTH)  # TOK-04
                    log.warning("character %s needs re-authorization (%s)", character_id, exc.code)
                raise
            if ts.identity.character_id != character_id:
                raise SsoError("identity_mismatch")
            self.store(ts)
            return ts.access_token

    async def remove(self, character_id: int) -> None:
        row = self.db.get_tokens(character_id)
        if row is not None and self.auth is not None:
            try:
                await self.auth.revoke(self.cipher.decrypt(row["refresh_token"]))
            except ValueError:
                pass
        self.db.delete_character(character_id)

    # -- views ---------------------------------------------------------------------------------------
    def list_view(self) -> list[dict[str, Any]]:
        out = []
        for c in self.db.list_characters():
            tok = self.db.get_tokens(c.character_id)
            out.append(
                {
                    "character_id": c.character_id,
                    "name": c.name,
                    "enabled": c.enabled,
                    "status": DISABLED if not c.enabled and c.status == OK else c.status,
                    "scopes": c.scopes,
                    "missing_scopes": self.missing_scopes(c.scopes),
                    "tags": c.tags,
                    "demo": c.demo,
                    "token_expires_at": tok["access_expires_at"] if tok else None,
                    "refresh_tail": ("…" + tok["refresh_tail"]) if tok and tok["refresh_tail"] else "",
                }
            )
        return out
