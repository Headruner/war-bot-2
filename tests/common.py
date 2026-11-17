"""Constants and helpers shared by the unit tests (importable as ``common``)."""

from __future__ import annotations

import httpx

from fake_eve import ALL_SCOPES

FAKE = "http://fake-eve"
JITA, PERIMETER, AMARR, TAMA, THERA = 30000142, 30000144, 30002187, 30002813, 31000005


async def mint(http: httpx.AsyncClient, character_id: int, **kw) -> dict:  # noqa: ANN003
    resp = await http.post("/_admin/mint", json={"character_id": character_id, **kw})
    return resp.json()


async def add_character(services, character_id: int, scopes=None, owner_id: int = 0):  # noqa: ANN001, ANN201
    tokens = await mint(services["http"], character_id, scopes=scopes or ALL_SCOPES)
    ts = await services["auth"].refresh(tokens["refresh_token"])
    services["chars"].store(ts, owner_id=owner_id)
    return tokens


def embed_lines(payload: dict) -> dict[str, str]:
    """The lines of a notification's Discord embed by label: ``**System** ...`` -> ``{"System": "..."}``; the
    zKillboard line is ``Kill check`` and the time line ``EVE Time``. Takes a payload or the embed itself."""
    embed = payload["embeds"][0] if "embeds" in payload else payload
    out: dict[str, str] = {}
    for line in embed.get("description", "").split("\n"):
        if line.startswith("**") and "** " in line[2:]:
            label, value = line[2:].split("** ", 1)
            out[label] = value
        elif line.startswith("zKillboard:"):
            out["Kill check"] = line
        elif line.startswith("EVE Time:"):
            out["EVE Time"] = line
    return out
