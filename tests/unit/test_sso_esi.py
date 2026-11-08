import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from common import FAKE, mint
from evedm.config import EveSettings
from evedm.esi import EsiClient, EsiError
from evedm.sso import AuthService, SsoError, identity_from_claims

pytestmark = pytest.mark.anyio


async def test_authorize_url_carries_state_scopes_and_callback(services, settings):
    url = services["auth"].authorize_url("st4te")
    q = parse_qs(urlparse(url).query)
    assert url.startswith(f"{FAKE}/v2/oauth/authorize?")
    assert q["state"] == ["st4te"] and q["client_id"] == [settings.eve.client_id]
    assert q["redirect_uri"] == [settings.eve.callback_url]
    assert set(q["scope"][0].split()) == set(settings.eve.required_scopes)


async def test_code_exchange_and_identity(services, http, settings):
    resp = await http.get(
        "/v2/oauth/authorize",
        params={
            "redirect_uri": settings.eve.callback_url,
            "client_id": settings.eve.client_id,
            "state": "x",
            "scope": " ".join(settings.eve.required_scopes),
            "character": 2100000002,
        },
        follow_redirects=False,
    )
    code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
    ts = await services["auth"].exchange_code(code)
    assert ts.identity.character_id == 2100000002
    assert ts.identity.name == "Bravo Two"
    assert set(ts.identity.scopes) == set(settings.eve.required_scopes)
    assert ts.expires_at > time.time() + 600


async def test_verify_rejects_expired_unless_allowed(services, http):
    tokens = await mint(http, 2100000001, expired=True)
    with pytest.raises(SsoError) as err:
        await services["auth"].verify(tokens["access_token"])
    assert err.value.code == "token_expired"
    ident = await services["auth"].verify(tokens["access_token"], allow_expired=True)
    assert ident.character_id == 2100000001


async def test_verify_rejects_token_for_another_application(services, http):
    tokens = await mint(http, 2100000001, client_id="someone-else")
    with pytest.raises(SsoError) as err:
        await services["auth"].verify(tokens["access_token"])
    assert err.value.code == "wrong_client"


async def test_verify_rejects_garbage(services):
    with pytest.raises(SsoError):
        await services["auth"].verify("not-a-jwt")


async def test_refresh_fails_after_revocation(services, http):
    tokens = await mint(http, 2100000001)
    await services["auth"].refresh(tokens["refresh_token"])
    await http.post("/_admin/revoke/2100000001")
    with pytest.raises(SsoError) as err:
        await services["auth"].refresh(tokens["refresh_token"])
    assert err.value.code == "invalid_grant"


async def test_wrong_client_secret_is_reported(settings, http):
    bad = EveSettings(**{**settings.eve.__dict__, "client_secret": "wrong"})
    with pytest.raises(SsoError) as err:
        await AuthService(bad, http).refresh("whatever")
    assert err.value.code == "invalid_client"


def test_identity_from_claims_parses_sub_and_scopes():
    ident = identity_from_claims(
        {"sub": "CHARACTER:EVE:123", "name": "N", "scp": "one", "aud": ["cid", "EVE Online"], "exp": 1}
    )
    assert ident.character_id == 123 and ident.scopes == ["one"] and ident.client_id == "cid"


# --------------------------------------------------------------------------- ESI
async def test_esi_cache_then_etag_revalidation(services, http, fake_app):
    tokens = await mint(http, 2100000001)
    esi: EsiClient = services["esi"]
    path = "/characters/2100000001/location"
    first = await esi.get(path, tokens["access_token"])
    second = await esi.get(path, tokens["access_token"])
    assert first == second == {"solar_system_id": 30000142, "station_id": 60003760}
    assert fake_app.state.fake.calls["location"] == 1 and esi.stats["cache_hits"] == 1
    esi._cache[path].expires = 0  # force revalidation
    assert await esi.get(path, tokens["access_token"]) == first
    assert fake_app.state.fake.calls.get("esi_304") == 1 and esi.stats["not_modified"] == 1
    assert esi.ratelimit["char-location"] == "1150"


async def test_character_state_skips_endpoints_without_scope(services, http, fake_app):
    tokens = await mint(http, 2100000002, scopes=["esi-location.read_location.v1"])
    state = await services["esi"].character_state(2100000002, tokens["access_token"], ["esi-location.read_location.v1"])
    assert set(state) == {"location"}
    assert "ship" not in fake_app.state.fake.calls


async def test_esi_errors_are_raised(services, http):
    tokens = await mint(http, 2100000001)
    with pytest.raises(EsiError) as err:
        await services["esi"].get("/characters/2100000002/location", tokens["access_token"])
    assert err.value.status == 403


async def test_esi_retries_after_429(settings):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True}, headers={"Cache-Control": "max-age=5"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=FAKE) as client:
        esi = EsiClient(settings.eve, client)
        assert await esi.get("/status") == {"ok": True}
        assert calls["n"] == 2 and esi.stats["rate_limited"] == 1


async def test_esi_sends_compatibility_date_and_user_agent(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=FAKE) as client:
        await EsiClient(settings.eve, client).get("/x")
    assert seen["x-compatibility-date"] == settings.eve.compatibility_date
    assert "EVE-Distance-Meter" in seen["user-agent"]
