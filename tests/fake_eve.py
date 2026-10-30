"""Fake EVE SSO + ESI server for unit, integration and performance tests.

Run standalone:  python tests/fake_eve.py --port 9100 --characters 50
In-process:      app = create_fake_app(...); httpx.AsyncClient(transport=httpx.ASGITransport(app))

It signs real RS256 JWTs and publishes a JWKS, so evedm's validation code runs unchanged.
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import json
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
ALL_SCOPES = ["esi-location.read_location.v1", "esi-location.read_ship_type.v1", "esi-location.read_online.v1"]
SHIP_NAMES = {
    587: "Rifter",
    22456: "Sabre",
    29990: "Loki",
    24702: "Hurricane",
    17478: "Retriever",
    670: "Capsule",
    99999: "Prototype Hull",
}
SYSTEMS = [30000142, 30000144, 30002187, 30002659, 30002510, 30001161, 30004759, 30000163, 30002053]


@dataclass
class FakeCharacter:
    character_id: int
    name: str
    solar_system_id: int
    ship_type_id: int = 587
    ship_name: str = "Fake Ship"
    station_id: int | None = None
    online: bool = False


@dataclass
class FakeState:
    issuer: str
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    rotate_refresh: bool = False
    access_ttl: int = 1199
    characters: dict[int, FakeCharacter] = field(default_factory=dict)
    codes: dict[str, tuple[int, list[str]]] = field(default_factory=dict)
    refresh: dict[str, tuple[int, list[str]]] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)
    calls: dict[str, int] = field(default_factory=dict)
    esi_delay_s: float = 0.0
    # structure scout: assets per character, structures with the characters on their access list
    assets: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    structures: dict[int, dict[str, Any]] = field(default_factory=dict)
    corp_names: dict[int, str] = field(default_factory=dict)
    asset_page_size: int = 1000


def make_characters(n: int, seed: int = 7, systems: list[int] | None = None) -> dict[int, FakeCharacter]:
    rnd = random.Random(seed)
    pool = systems or SYSTEMS
    out = {}
    for k in range(n):
        cid = 2_100_000_000 + k
        out[cid] = FakeCharacter(
            cid,
            f"Pilot {k:03d}",
            rnd.choice(pool),
            rnd.choice(list(SHIP_NAMES)),
            f"Ship {k}",
            60003760 if rnd.random() < 0.6 else None,
            rnd.random() < 0.2,
        )
    return out


def create_fake_app(
    issuer: str = "http://fake-eve", characters: dict[int, FakeCharacter] | None = None, rotate_refresh: bool = False
) -> FastAPI:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "JWT-Signature-Key", "alg": "RS256", "use": "sig"})
    st = FakeState(
        issuer=issuer.rstrip("/"),
        rotate_refresh=rotate_refresh,
        characters=characters if characters is not None else make_characters(5),
    )
    app = FastAPI()
    app.state.fake = st

    def count(name: str) -> None:
        st.calls[name] = st.calls.get(name, 0) + 1

    def mint(cid: int, scopes: list[str], ttl: int | None = None, client_id: str | None = None) -> str:
        now = int(time.time())
        claims = {
            "scp": scopes,
            "jti": secrets.token_hex(8),
            "kid": "JWT-Signature-Key",
            "sub": f"CHARACTER:EVE:{cid}",
            "azp": client_id or st.client_id,
            "tenant": "tranquility",
            "tier": "live",
            "region": "world",
            "aud": [client_id or st.client_id, "EVE Online"],
            "name": st.characters[cid].name,
            "owner": hashlib.sha1(str(cid).encode()).hexdigest(),  # noqa: S324
            "exp": now + (st.access_ttl if ttl is None else ttl),
            "iat": now,
            "iss": st.issuer,
        }
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "JWT-Signature-Key"})

    def issue(cid: int, scopes: list[str]) -> dict[str, Any]:
        refresh = secrets.token_urlsafe(24)
        st.refresh[refresh] = (cid, scopes)
        return {
            "access_token": mint(cid, scopes),
            "expires_in": st.access_ttl,
            "token_type": "Bearer",
            "refresh_token": refresh,
        }

    # ------------------------------------------------------------------ SSO
    @app.get("/.well-known/oauth-authorization-server")
    async def metadata() -> dict[str, Any]:
        return {
            "issuer": st.issuer,
            "jwks_uri": f"{st.issuer}/oauth/jwks",
            "authorization_endpoint": f"{st.issuer}/v2/oauth/authorize",
            "token_endpoint": f"{st.issuer}/v2/oauth/token",
        }

    @app.get("/oauth/jwks")
    async def jwks() -> dict[str, Any]:
        count("jwks")
        return {"keys": [jwk], "SkipUnresolvedJsonWebKeys": True}

    @app.get("/v2/oauth/authorize")
    async def authorize(
        redirect_uri: str, client_id: str, state: str, scope: str = "", character: int | None = None
    ) -> Response:
        if client_id != st.client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        cid = character or next(iter(st.characters))
        code = secrets.token_urlsafe(16)
        st.codes[code] = (cid, [s for s in scope.split() if s])
        return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)

    def check_client(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            return False
        return base64.b64decode(auth[6:]).decode() == f"{st.client_id}:{st.client_secret}"

    @app.post("/v2/oauth/token")
    async def token(request: Request) -> Response:
        count("token")
        if not check_client(request):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        form = await request.form()
        grant = form.get("grant_type")
        if grant == "authorization_code":
            item = st.codes.pop(str(form.get("code")), None)
            if item is None:
                return JSONResponse({"error": "invalid_grant", "error_description": "bad code"}, status_code=400)
            return JSONResponse(issue(*item))
        if grant == "refresh_token":
            rt = str(form.get("refresh_token"))
            if rt in st.revoked or rt not in st.refresh:
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "Invalid refresh token"}, status_code=400
                )
            cid, scopes = st.refresh[rt]
            if st.rotate_refresh:
                st.revoked.add(rt)
                return JSONResponse(issue(cid, scopes))
            return JSONResponse(
                {
                    "access_token": mint(cid, scopes),
                    "expires_in": st.access_ttl,
                    "token_type": "Bearer",
                    "refresh_token": rt,
                }
            )
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    @app.post("/v2/oauth/revoke")
    async def revoke(request: Request) -> Response:
        form = await request.form()
        st.revoked.add(str(form.get("token")))
        return Response(status_code=200)

    # ------------------------------------------------------------------ ESI
    def esi_auth(request: Request, cid: int, scope: str) -> Response | None:
        if cid not in st.characters:
            return JSONResponse({"error": "Character not found"}, status_code=404)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        try:
            claims = jwt.decode(auth[7:], key.public_key(), algorithms=["RS256"], audience=st.client_id)
        except jwt.PyJWTError:
            return JSONResponse({"error": "token is not valid"}, status_code=401)
        if claims["sub"] != f"CHARACTER:EVE:{cid}":
            return JSONResponse({"error": "token not valid for character"}, status_code=403)
        if scope not in claims.get("scp", []):
            return JSONResponse({"error": "token not valid for scope"}, status_code=403)
        if request.headers.get("x-compatibility-date") is None:
            return JSONResponse({"error": "missing compatibility date"}, status_code=400)
        return None

    def esi_json(request: Request, body: Any, ttl: int) -> Response:
        payload = json.dumps(body).encode()
        etag = '"' + hashlib.md5(payload).hexdigest() + '"'  # noqa: S324
        headers = {
            "ETag": etag,
            "Expires": email.utils.formatdate(time.time() + ttl, usegmt=True),
            "X-Ratelimit-Group": "char-location",
            "X-Ratelimit-Limit": "1200/15m",
            "X-Ratelimit-Remaining": "1150",
            "X-Ratelimit-Used": "2",
        }
        if request.headers.get("if-none-match") == etag:
            count("esi_304")
            return Response(status_code=304, headers=headers)
        return Response(payload, media_type="application/json", headers=headers)

    async def maybe_delay() -> None:
        if st.esi_delay_s:
            import asyncio

            await asyncio.sleep(st.esi_delay_s)

    @app.get("/characters/{cid}/location")
    async def location(request: Request, cid: int) -> Response:
        count("location")
        await maybe_delay()
        if (err := esi_auth(request, cid, "esi-location.read_location.v1")) is not None:
            return err
        c = st.characters[cid]
        body: dict[str, Any] = {"solar_system_id": c.solar_system_id}
        if c.station_id:
            body["station_id"] = c.station_id
        return esi_json(request, body, 5)

    @app.get("/characters/{cid}/ship")
    async def ship(request: Request, cid: int) -> Response:
        count("ship")
        await maybe_delay()
        if (err := esi_auth(request, cid, "esi-location.read_ship_type.v1")) is not None:
            return err
        c = st.characters[cid]
        return esi_json(
            request,
            {"ship_type_id": c.ship_type_id, "ship_item_id": 1_000_000_000_000 + cid, "ship_name": c.ship_name},
            5,
        )

    @app.get("/characters/{cid}/online")
    async def online(request: Request, cid: int) -> Response:
        count("online")
        await maybe_delay()
        if (err := esi_auth(request, cid, "esi-location.read_online.v1")) is not None:
            return err
        c = st.characters[cid]
        return esi_json(
            request,
            {
                "online": c.online,
                "last_login": "2026-10-28T20:00:00Z",
                "last_logout": "2026-10-28T22:00:00Z",
                "logins": 42,
            },
            60,
        )

    @app.post("/universe/names")
    async def names(request: Request) -> Response:
        ids = await request.json()
        out = []
        for i in ids:
            if i in st.corp_names:
                out.append({"id": i, "name": st.corp_names[i], "category": "corporation"})
            else:
                out.append({"id": i, "name": SHIP_NAMES.get(i, f"Type {i}"), "category": "inventory_type"})
        return JSONResponse(out)

    def token_claims(request: Request, scope: str) -> tuple[dict[str, Any] | None, Response | None]:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None, JSONResponse({"error": "authentication required"}, status_code=401)
        try:
            claims = jwt.decode(auth[7:], key.public_key(), algorithms=["RS256"], audience=st.client_id)
        except jwt.PyJWTError:
            return None, JSONResponse({"error": "token is not valid"}, status_code=401)
        if scope not in claims.get("scp", []):
            return None, JSONResponse({"error": "token not valid for scope"}, status_code=403)
        return claims, None

    @app.get("/characters/{cid}/assets")
    async def assets(request: Request, cid: int, page: int = 1) -> Response:
        count("assets")
        if (err := esi_auth(request, cid, "esi-assets.read_assets.v1")) is not None:
            return err
        items = st.assets.get(cid, [])
        size = st.asset_page_size
        pages = max(1, -(-len(items) // size))
        if page > pages:
            return JSONResponse({"error": "Requested page does not exist!"}, status_code=404)
        body = items[(page - 1) * size : page * size]
        return Response(json.dumps(body), media_type="application/json", headers={"X-Pages": str(pages)})

    @app.get("/universe/structures/{sid}")
    async def structure(request: Request, sid: int) -> Response:
        count("structure")
        claims, err = token_claims(request, "esi-universe.read_structures.v1")
        if err is not None:
            return err
        st_ = st.structures.get(sid)
        if st_ is None:
            return JSONResponse({"error": "Structure not found"}, status_code=404)
        cid = int(str(claims["sub"]).rsplit(":", 1)[1])
        if st_.get("acl") is not None and cid not in st_["acl"]:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        body = {k: v for k, v in st_.items() if k != "acl"}
        return esi_json(request, body, 3600)

    # ------------------------------------------------------------------ test admin
    @app.post("/_admin/mint")
    async def admin_mint(request: Request) -> dict[str, Any]:
        body = await request.json()
        cid = int(body["character_id"])
        scopes = body.get("scopes", ALL_SCOPES)
        tokens = issue(cid, scopes)
        if body.get("expired"):
            tokens["access_token"] = mint(cid, scopes, ttl=-600)
        if body.get("client_id"):
            tokens["access_token"] = mint(cid, scopes, client_id=body["client_id"])
        return tokens

    @app.post("/_admin/revoke/{cid}")
    async def admin_revoke(cid: int) -> dict[str, Any]:
        n = 0
        for rt, (owner, _) in st.refresh.items():
            if owner == cid:
                st.revoked.add(rt)
                n += 1
        return {"revoked": n}

    @app.post("/_admin/move/{cid}")
    async def admin_move(request: Request, cid: int) -> dict[str, Any]:
        body = await request.json()
        c = st.characters[cid]
        c.solar_system_id = int(body.get("solar_system_id", c.solar_system_id))
        c.ship_type_id = int(body.get("ship_type_id", c.ship_type_id))
        return {"ok": True}

    @app.post("/_admin/config")
    async def admin_config(request: Request) -> dict[str, Any]:
        body = await request.json()
        if "esi_delay_s" in body:
            st.esi_delay_s = float(body["esi_delay_s"])
        if "access_ttl" in body:
            st.access_ttl = int(body["access_ttl"])
        return {"ok": True}

    @app.post("/_admin/structures")
    async def admin_structures(request: Request) -> dict[str, Any]:
        body = await request.json()
        for sid, data in (body.get("structures") or {}).items():
            st.structures[int(sid)] = data
        for cid, items in (body.get("assets") or {}).items():
            st.assets[int(cid)] = items
        for oid, name in (body.get("corp_names") or {}).items():
            st.corp_names[int(oid)] = name
        return {"ok": True}

    @app.get("/_admin/stats")
    async def admin_stats() -> dict[str, Any]:
        return {"calls": st.calls, "characters": len(st.characters)}

    @app.get("/_admin/characters")
    async def admin_characters() -> list[dict[str, Any]]:
        return [
            {"character_id": c.character_id, "name": c.name, "solar_system_id": c.solar_system_id}
            for c in st.characters.values()
        ]

    return app


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--characters", type=int, default=5)
    p.add_argument("--rotate-refresh", action="store_true")
    p.add_argument("--systems", help="comma separated solar system ids to place characters in")
    args = p.parse_args()
    systems = [int(s) for s in args.systems.split(",")] if args.systems else None
    app = create_fake_app(
        f"http://{args.host}:{args.port}", make_characters(args.characters, systems=systems), args.rotate_refresh
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
