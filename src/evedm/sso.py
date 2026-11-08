"""EVE SSO v2 (OAuth 2.0 authorization code + refresh) and JWT validation (SSO-01..SSO-05, TOK-02)."""

from __future__ import annotations

import base64
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from .config import EveSettings


class SsoError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Identity:
    character_id: int
    name: str
    scopes: list[str]
    owner_hash: str | None
    expires_at: float
    client_id: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float
    identity: Identity


class AuthService:
    def __init__(self, settings: EveSettings, client: httpx.AsyncClient):
        self.s = settings
        self.http = client
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    # -- URLs -----------------------------------------------------------------------------
    @staticmethod
    def new_state() -> str:
        return secrets.token_urlsafe(24)

    def authorize_url(self, state: str, scopes: list[str] | None = None) -> str:
        params = {
            "response_type": "code",
            "redirect_uri": self.s.callback_url,
            "client_id": self.s.client_id,
            "scope": " ".join(self.s.required_scopes if scopes is None else scopes),
            "state": state,
        }
        return f"{self.s.sso_base.rstrip('/')}/v2/oauth/authorize?{urlencode(params)}"

    def _basic(self) -> dict[str, str]:
        raw = f"{self.s.client_id}:{self.s.client_secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    # -- token endpoint -----------------------------------------------------------------------
    async def _token(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            resp = await self.http.post(
                f"{self.s.sso_base.rstrip('/')}/v2/oauth/token",
                data=data,
                headers={**self._basic(), "Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise SsoError("sso_unreachable", type(exc).__name__) from exc
        if resp.status_code != 200:
            try:
                body = resp.json()
                code = str(body.get("error") or f"http_{resp.status_code}")
                detail = str(body.get("error_description") or "")
            except ValueError:
                code, detail = f"http_{resp.status_code}", ""
            raise SsoError(code, detail)
        return resp.json()

    async def exchange_code(self, code: str) -> TokenSet:
        body = await self._token({"grant_type": "authorization_code", "code": code})
        return await self._token_set(body)

    async def refresh(self, refresh_token: str) -> TokenSet:
        body = await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        if not body.get("refresh_token"):
            body["refresh_token"] = refresh_token
        return await self._token_set(body)

    async def revoke(self, refresh_token: str) -> None:
        try:
            await self.http.post(
                f"{self.s.sso_base.rstrip('/')}/v2/oauth/revoke",
                data={"token_type_hint": "refresh_token", "token": refresh_token},
                headers=self._basic(),
            )
        except httpx.HTTPError:
            pass  # best effort (TOK-05)

    async def _token_set(self, body: dict[str, Any]) -> TokenSet:
        access = str(body["access_token"])
        identity = await self.verify(access)
        expires_at = time.time() + float(body.get("expires_in", 1199))
        return TokenSet(access, str(body["refresh_token"]), min(expires_at, identity.expires_at), identity)

    # -- JWT --------------------------------------------------------------------------------------
    async def _keys(self, force: bool = False) -> dict[str, Any]:
        if self._jwks is not None and not force and time.time() - self._jwks_at < 3600:
            return self._jwks
        base = self.s.sso_base.rstrip("/")
        try:
            meta = await self.http.get(f"{base}/.well-known/oauth-authorization-server")
            jwks_uri = meta.json().get("jwks_uri") if meta.status_code == 200 else None
            resp = await self.http.get(jwks_uri or f"{base}/oauth/jwks")
            resp.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            raise SsoError("jwks_unavailable", type(exc).__name__) from exc
        self._jwks = {k["kid"]: k for k in resp.json().get("keys", []) if "kid" in k}
        self._jwks_at = time.time()
        return self._jwks

    async def verify(self, token: str, allow_expired: bool = False) -> Identity:
        """Validate signature, issuer, audience (client_id) and, unless allowed, expiry."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise SsoError("invalid_token", "malformed JWT") from exc
        keys = await self._keys()
        jwk = keys.get(header.get("kid", ""))
        if jwk is None:
            jwk = (await self._keys(force=True)).get(header.get("kid", ""))
        if jwk is None:
            raise SsoError("invalid_token", "unknown signing key")
        try:
            claims = jwt.decode(
                token,
                key=jwt.PyJWK(jwk).key,
                algorithms=["RS256", "ES256"],
                audience=self.s.client_id or None,
                options={"verify_exp": not allow_expired, "verify_aud": bool(self.s.client_id), "verify_iss": False},
                leeway=30,
            )
        except jwt.ExpiredSignatureError as exc:
            raise SsoError("token_expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise SsoError("wrong_client", "token was issued for another EVE application") from exc
        except jwt.PyJWTError as exc:
            raise SsoError("invalid_token", type(exc).__name__) from exc
        issuers = {i.rstrip("/") for i in self.s.jwt_issuers} | {self.s.sso_base.rstrip("/")}
        if str(claims.get("iss", "")).rstrip("/") not in issuers:
            raise SsoError("invalid_token", "unexpected issuer")
        return identity_from_claims(claims)


def identity_from_claims(claims: dict[str, Any]) -> Identity:
    sub = str(claims.get("sub", ""))
    try:
        character_id = int(sub.rsplit(":", 1)[-1])
    except ValueError as exc:
        raise SsoError("invalid_token", "sub is not a character") from exc
    scp = claims.get("scp", [])
    scopes = [scp] if isinstance(scp, str) else list(scp or [])
    aud = claims.get("aud")
    client_id = None
    if isinstance(aud, list):
        client_id = next((a for a in aud if a != "EVE Online"), None)
    elif isinstance(aud, str) and aud != "EVE Online":
        client_id = aud
    return Identity(
        character_id=character_id,
        name=str(claims.get("name", f"Character {character_id}")),
        scopes=sorted(scopes),
        owner_hash=claims.get("owner"),
        expires_at=float(claims.get("exp", time.time())),
        client_id=client_id or claims.get("azp"),
        claims=claims,
    )
