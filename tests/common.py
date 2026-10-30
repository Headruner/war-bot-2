"""Constants and helpers shared by the unit tests (importable as ``common``)."""

from __future__ import annotations

import httpx

from fake_eve import ALL_SCOPES

FAKE = "http://fake-eve"
JITA, PERIMETER, AMARR, TAMA, THERA = 30000142, 30000144, 30002187, 30002813, 31000005


async def mint(http: httpx.AsyncClient, character_id: int, **kw) -> dict:  # noqa: ANN003
    resp = await http.post("/_admin/mint", json={"character_id": character_id, **kw})
    return resp.json()


async def add_character(services, character_id: int, scopes=None):  # noqa: ANN001, ANN201
    tokens = await mint(services["http"], character_id, scopes=scopes or ALL_SCOPES)
    ts = await services["auth"].refresh(tokens["refresh_token"])
    services["chars"].store(ts)
    return tokens
