"""ESI client: Expires/ETag caching (RNK-10), rate-limit handling (NFR-03), bounded concurrency."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

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
            expires = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError):
            expires = None
        if expires is not None:
            # measure the lifetime against the server's own Date, not our clock: a host clock that runs behind
            # (a VM after sleep) would otherwise keep the copy too long, one that runs ahead would ask too early
            try:
                served = parsedate_to_datetime(resp.headers["Date"]).timestamp()
            except (KeyError, TypeError, ValueError):
                return expires
            return time.time() + max(0.0, expires - served)
    cc = resp.headers.get("Cache-Control", "")
    for part in cc.split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                return time.time() + float(part[8:])
            except ValueError:
                break
    return time.time() + default_s


ERROR_LIMIT_RESERVE = 5  # with this few errors left in ESI's window, every caller stops sending until it resets


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
        # ESI's error limit (~100 4xx/5xx per minute per IP and app): once exhausted every request answers 420 and
        # each further request prolongs the ban, so remember the window and stop sending until it resets
        self._limit_remain: int | None = None
        self._limit_reset_at = 0.0
        self._blocked_until = 0.0
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self.headers = {
            "Accept": "application/json",
            "User-Agent": settings.esi_user_agent(),
            "X-Compatibility-Date": settings.compatibility_date,
        }

    def blocked_for(self, reserve: int = ERROR_LIMIT_RESERVE) -> float:
        """Seconds until ESI may be called again (0 = now): after a 420, or when <= ``reserve`` errors are left.

        Background jobs pass a bigger reserve so they stop early and leave room for the character refresh.
        """
        now = time.time()
        if self._blocked_until > now:
            return self._blocked_until - now
        if self._limit_remain is not None and self._limit_remain <= reserve and self._limit_reset_at > now:
            return self._limit_reset_at - now
        return 0.0

    def _note_error_limit(self, resp: httpx.Response) -> None:
        remain, reset = resp.headers.get("X-Esi-Error-Limit-Remain"), resp.headers.get("X-Esi-Error-Limit-Reset")
        try:
            if remain is not None:
                self._limit_remain = int(remain)
                self._limit_reset_at = time.time() + (float(reset) if reset else 60.0)
            if resp.status_code == 420:
                pause = max(float(reset or 60), 1.0) + 1.0
                self._blocked_until = time.time() + pause
                log.warning("ESI error limit reached: pausing all ESI calls for %.0f s", pause)
        except ValueError:
            if resp.status_code == 420:
                self._blocked_until = time.time() + 61.0

    def cached_expiry(self, path: str) -> float | None:
        """When the cached copy of ``path`` stops being fresh (epoch seconds), or None when it is not cached."""
        entry = self._cache.get(path)
        return entry.expires if entry is not None else None

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

    async def get_paged(self, path: str, token: str, max_pages: int = 20) -> tuple[list[Any], bool]:
        """Every page of a paged authenticated list (X-Pages), uncached. Returns (items, truncated)."""
        items: list[Any] = []
        pages = 1
        async for chunk, total in self.iter_pages(path, token, max_pages):
            items.extend(chunk)
            pages = total
        return items, pages > max_pages

    async def iter_pages(self, path: str, token: str, max_pages: int = 20) -> AsyncIterator[tuple[list[Any], int]]:
        """A paged authenticated list one page at a time: yields (items of the page, total pages ESI reports).

        Nothing is kept between pages, so a list of any length is read in constant memory. The read ends after
        ``max_pages`` pages or the last one; the caller sees ``total pages > max_pages`` when it was cut short.
        """
        headers = dict(self.headers) | {"Authorization": f"Bearer {token}"}
        pages, page = 1, 1
        while page <= min(pages, max_pages):
            resp = await self._send("GET", f"{path}?page={page}", headers)
            if resp.status_code != 200:
                self.stats["errors"] += 1
                detail = ""
                try:
                    detail = str(resp.json().get("error", ""))
                except ValueError:
                    pass
                raise EsiError(resp.status_code, detail)
            try:
                pages = max(1, int(resp.headers.get("X-Pages", "1") or 1))
            except ValueError:
                pages = 1
            yield resp.json(), pages
            page += 1

    async def names_categories(self, ids: list[int]) -> dict[int, tuple[str, str]]:
        """POST /universe/names in chunks of 1000; an invalid ID (404 for the chunk) is bisected out."""
        out: dict[int, tuple[str, str]] = {}

        async def resolve(chunk: list[int]) -> None:
            try:
                rows = await self.post("/universe/names", chunk)
            except EsiError as exc:
                if exc.status != 404:
                    raise
                if len(chunk) > 1:
                    await resolve(chunk[: len(chunk) // 2])
                    await resolve(chunk[len(chunk) // 2 :])
                return
            for r in rows:
                out[int(r["id"])] = (str(r["name"]), str(r.get("category", "")))

        unique = sorted({int(i) for i in ids if i})
        for start in range(0, len(unique), 1000):
            await resolve(unique[start : start + 1000])
        return out

    async def post_auth(self, path: str, token: str, params: dict[str, Any] | None = None) -> int:
        """Authenticated POST without a body (e.g. /ui/autopilot/waypoint); returns the status (204 = done)."""
        headers = dict(self.headers) | {"Authorization": f"Bearer {token}"}
        resp = await self._send("POST", path, headers, params=params or {})
        if resp.status_code >= 400:
            self.stats["errors"] += 1
            detail = ""
            try:
                detail = str(resp.json().get("error", ""))
            except ValueError:
                pass
            raise EsiError(resp.status_code, detail)
        return resp.status_code

    async def post(self, path: str, payload: Any) -> Any:
        resp = await self._send("POST", path, dict(self.headers), json=payload)
        if resp.status_code != 200:
            self.stats["errors"] += 1
            raise EsiError(resp.status_code)
        return resp.json()

    async def _send(self, method: str, path: str, headers: dict[str, str], **kw: Any) -> httpx.Response:
        url = f"{self.base}{path}"
        wait = self.blocked_for()
        if wait > 0:
            self.stats["error_limited"] = self.stats.get("error_limited", 0) + 1
            raise EsiError(420, f"error limited, retry in {wait:.0f} s")
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
            self._note_error_limit(resp)
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


async def search_structure_ids(esi: Any, character_id: int, token: str, term: str) -> list[int]:
    """Structure IDs the character may dock at whose name contains ``term`` (scope ``esi-search.search_structures.v1``).

    ``esi`` is anything with ``get(path, token)``; ESI wants at least three characters in the term. Shared by the
    Ansiblex reader (term " » ") and the scout's dock search (term " - ").
    """
    found = await esi.get(
        f"/characters/{character_id}/search/?categories=structure&search={quote(term)}&strict=false", token
    )
    return [int(i) for i in (found or {}).get("structure", [])]
