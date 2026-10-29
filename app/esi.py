"""CCP ESI client: async, retries, and an in-memory name cache."""
from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

# 420 = ESI error limit, 429 = rate limit, 5xx = transient server errors
RETRY_STATUSES = {420, 429, 500, 502, 503, 504}
KILLMAIL_PAGE_SIZE = 2000
NAMES_CHUNK = 1000


class EsiError(Exception):
    """ESI failure; ``status`` is the HTTP status code (None for network errors)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status

    @property
    def not_found(self) -> bool:
        return self.status == 404


class EsiClient:
    def __init__(self, base_url: str, user_agent: str, timeout: float = 15.0,
                 compatibility_date: str = "2025-08-26", retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.compatibility_date = compatibility_date
        self.retries = max(1, retries)
        self._names: Dict[int, Tuple[str, str]] = {}
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "User-Agent": self.user_agent,
                    "X-Compatibility-Date": self.compatibility_date,
                    "Accept": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                       json: Any = None) -> Any:
        path = "/" + endpoint.lstrip("/")
        last: Optional[EsiError] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = await self._http().request(method, path, params=params, json=json)
            except httpx.HTTPError as e:
                last = EsiError(f"{method} {path}: {e!r}")
            else:
                if response.status_code < 400:
                    return response.json()
                last = EsiError(f"{method} {path} -> HTTP {response.status_code}: "
                                f"{response.text[:200]}", response.status_code)
                if response.status_code not in RETRY_STATUSES:
                    raise last
            if attempt < self.retries:
                await asyncio.sleep(2 ** attempt)
        log.warning("ESI request failed after %d attempt(s): %s", self.retries, last)
        raise last

    # --- endpoints -------------------------------------------------------

    async def wars(self, max_war_id: Optional[int] = None) -> List[int]:
        """Newest war IDs (up to 2000), as returned by ESI (descending)."""
        params = {"max_war_id": max_war_id} if max_war_id else None
        return await self._request("GET", "wars/", params=params)

    async def war(self, war_id: int) -> Dict:
        return await self._request("GET", f"wars/{war_id}/")

    async def alliance(self, alliance_id: int) -> Dict:
        return await self._request("GET", f"alliances/{alliance_id}/")

    async def corporation(self, corp_id: int) -> Dict:
        return await self._request("GET", f"corporations/{corp_id}/")

    async def alliance_corporations(self, alliance_id: int) -> List[int]:
        return await self._request("GET", f"alliances/{alliance_id}/corporations/")

    async def names(self, ids: Iterable[Optional[int]]) -> Dict[int, Tuple[str, str]]:
        """Resolve IDs to ``{id: (name, category)}``. Unknown IDs are left out."""
        wanted = {int(i) for i in ids if i}
        missing = sorted(i for i in wanted if i not in self._names)
        for start in range(0, len(missing), NAMES_CHUNK):
            await self._resolve(missing[start:start + NAMES_CHUNK])
        return {i: self._names[i] for i in wanted if i in self._names}

    async def _resolve(self, chunk: List[int]) -> None:
        # ESI expects a bare JSON array and answers 404 if any ID is invalid,
        # so bisect on 404 to still resolve the valid ones.
        try:
            rows = await self._request("POST", "universe/names/", json=chunk)
        except EsiError as e:
            if not e.not_found:
                raise
            if len(chunk) > 1:
                mid = len(chunk) // 2
                await self._resolve(chunk[:mid])
                await self._resolve(chunk[mid:])
            return
        for row in rows:
            self._names[int(row["id"])] = (row["name"], row["category"])

    async def killmails(self, war_id: int, max_pages: int = 1) -> List[Dict]:
        """Killmail refs ``{killmail_id, killmail_hash}`` for a war."""
        refs: List[Dict] = []
        for page in range(1, max(1, max_pages) + 1):
            batch = await self._request("GET", f"wars/{war_id}/killmails/", params={"page": page})
            refs.extend(batch)
            if len(batch) < KILLMAIL_PAGE_SIZE:
                break
        return refs

    async def killmail(self, km_id: int, km_hash: str) -> Dict:
        return await self._request("GET", f"killmails/{km_id}/{km_hash}/")
