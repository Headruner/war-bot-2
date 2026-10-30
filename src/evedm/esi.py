"""ESI client: Expires/ETag caching (RNK-10), rate-limit handling (NFR-03), bounded concurrency."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import __version__
from .config import EveSettings

log = logging.getLogger("evedm.esi")


class EsiError(Exception):
    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"ESI {status} {detail}".strip())
        self.status = status
        self.detail = detail


@dataclass
class _Entry:
    body: Any
    etag: str | None
    expires: float


def _expires(resp: httpx.Response, default_s: float = 5.0) -> float:
    value = resp.headers.get("Expires")
    if value:
        try:
            return parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError):
            pass
    cc = resp.headers.get("Cache-Control", "")
    for part in cc.split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                return time.time() + float(part[8:])
            except ValueError:
                break
    return time.time() + default_s


class EsiClient:
    def __init__(self, settings: EveSettings, client: httpx.AsyncClient, cache_size: int = 4096):
        self.s = settings
        self.http = client
        self.base = settings.esi_base.rstrip("/")
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        self._cache_size = cache_size
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrency))
        self.stats = {"requests": 0, "cache_hits": 0, "not_modified": 0, "errors": 0, "rate_limited": 0}
        self.ratelimit: dict[str, str] = {}
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self.headers = {
            "Accept": "application/json",
            "User-Agent": settings.user_agent or f"EVE-Distance-Meter/{__version__}",
            "X-Compatibility-Date": settings.compatibility_date,
        }

    async def get(self, path: str, token: str | None = None) -> Any:
        """GET with Expires/ETag caching; identical concurrent requests share one HTTP call."""
        key = path
        entry = self._cache.get(key)
        if entry is not None and entry.expires > time.time():
            self._cache.move_to_end(key)
            self.stats["cache_hits"] += 1
            return entry.body
        pending = self._inflight.get(key)
        if pending is not None:
            self.stats["coalesced"] = self.stats.get("coalesced", 0) + 1
            return await asyncio.shield(pending)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            body = await self._get(key, entry, token)
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved when nobody else waits
            raise
        else:
            fut.set_result(body)
            return body
        finally:
            self._inflight.pop(key, None)

    async def _get(self, key: str, entry: _Entry | None, token: str | None) -> Any:
        path = key
        headers = dict(self.headers)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if entry is not None and entry.etag:
            headers["If-None-Match"] = entry.etag
        resp = await self._send("GET", path, headers)
        if resp.status_code == 304 and entry is not None:
            self.stats["not_modified"] += 1
            entry.expires = _expires(resp)
            self._cache.move_to_end(key)
            return entry.body
        if resp.status_code == 200:
            body = resp.json()
            self._cache[key] = _Entry(body, resp.headers.get("ETag"), _expires(resp))
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return body
        self.stats["errors"] += 1
        detail = ""
        try:
            detail = str(resp.json().get("error", ""))
        except ValueError:
            pass
        raise EsiError(resp.status_code, detail)

    async def post(self, path: str, payload: Any) -> Any:
        resp = await self._send("POST", path, dict(self.headers), json=payload)
        if resp.status_code != 200:
            self.stats["errors"] += 1
            raise EsiError(resp.status_code)
        return resp.json()

    async def _send(self, method: str, path: str, headers: dict[str, str], **kw: Any) -> httpx.Response:
        url = f"{self.base}{path}"
        for attempt in range(3):
            async with self._sem:
                self.stats["requests"] += 1
                try:
                    resp = await self.http.request(method, url, headers=headers, **kw)
                except httpx.HTTPError as exc:
                    if attempt == 2:
                        raise EsiError(0, type(exc).__name__) from exc
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            group = resp.headers.get("X-Ratelimit-Group")
            if group:
                self.ratelimit[group] = resp.headers.get("X-Ratelimit-Remaining", "")
            if resp.status_code == 429:
                self.stats["rate_limited"] += 1
                wait = min(float(resp.headers.get("Retry-After", "1") or 1), 10.0)
                log.warning("ESI rate limited on %s, retrying in %.1fs", path.split("/")[1], wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 420:
                raise EsiError(420, "error limited")
            if resp.status_code >= 500 and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return resp
        return resp

    # -- convenience ----------------------------------------------------------------------------
    async def character_state(self, character_id: int, token: str, scopes: list[str]) -> dict[str, Any]:
        """Location, ship and online state; endpoints whose scope is missing are skipped (SSO-05)."""
        calls: dict[str, Any] = {}
        if "esi-location.read_location.v1" in scopes:
            calls["location"] = self.get(f"/characters/{character_id}/location", token)
        if "esi-location.read_ship_type.v1" in scopes:
            calls["ship"] = self.get(f"/characters/{character_id}/ship", token)
        if "esi-location.read_online.v1" in scopes:
            calls["online"] = self.get(f"/characters/{character_id}/online", token)
        results = await asyncio.gather(*calls.values())
        return dict(zip(calls.keys(), results, strict=True))

    async def names(self, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        rows = await self.post("/universe/names", ids)
        return {int(r["id"]): str(r["name"]) for r in rows}
