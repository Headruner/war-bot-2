"""CCP ESI Client with Async support, Caching, and Retries."""
from __future__ import annotations
import logging
import asyncio
import httpx
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

class EsiError(Exception):
    """Exception raised for ESI errors."""
    pass

class EsiClient:
    def __init__(self, base_url: str, user_agent: str, timeout: float = 15.0,
                 compatibility_date: str = "2025-08-26",
                 names_get: Optional[callable] = None,
                 names_put: Optional[callable] = None):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.compatibility_date = compatibility_date
        self.names_get = names_get
        self.names_put = names_put
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.cache_expiry: Dict[str, float] = {}

    def _get_cache_key(self, entity_type: str, entity_id: int) -> str:
        return f"{entity_type}:{entity_id}"

    async def _request(self, method: str, endpoint: str, params: Dict = None, 
                        json: Dict = None, headers: Dict = None) -> Dict:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = headers or {"User-Agent": self.user_agent}
        if params:
            params["X-Compatibility-Date"] = self.compatibility_date
            params["X-Cache-Purge"] = "true"

        # Retry Logic
        max_retries = 3
        backoff = 2.0
        
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.request(method, url, params=params, json=json, headers=headers)
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 or e.response.status_code >= 500:
                    if attempt == max_retries - 1:
                        raise EsiError(f"ESI Server Error: {e.response.status_code}")
                    wait_time = e.response.headers.get("Retry-After", backoff)
                    log.warning("ESI Rate Limit / Server Error. Retrying in %.2fs...", float(wait_time))
                    await asyncio.sleep(float(wait_time))
                    backoff *= 2
                else:
                    log.error("ESI Request failed with %s: %s", e.response.status_code, url)
                    raise EsiError(f"ESI Request failed with {e.response.status_code}")
            except Exception as e:
                if attempt == max_retries - 1:
                    log.exception("Unexpected ESI Error: %s", e)
                    raise EsiError(str(e))
                await asyncio.sleep(backoff)
                backoff *= 2

    async def wars(self) -> List[Dict]:
        return await self._request("GET", "wars/")

    async def war(self, war_id: int) -> Dict:
        return await self._request("GET", f"wars/{war_id}/")

    async def alliance(self, alliance_id: int) -> Dict:
        return await self._request("GET", f"alliances/{alliance_id}/")

    async def corporation(self, corp_id: int) -> Dict:
        return await self._request("GET", f"corporations/{corp_id}/")

    async def alliance_corporations(self, alliance_id: int) -> List[int]:
        return await self._request("GET", f"alliances/{alliance_id}/corporations/")

    async def names(self, ids: List[int]) -> Dict:
        if not ids: return {}
        cache_key = f"names:{','.join(map(str, ids))}"
        
        # Check Cache
        if cache_key in self.cache and time.time() < self.cache_expiry.get(cache_key, 0):
            return self.cache[cache_key]
        
        data = await self._request("POST", "universe/names/", json={"ids": ids})
        self.cache[cache_key] = data
        self.cache_expiry[cache_key] = time.time() + 3600 # Cache for 1 hour
        return data

    async def killmails(self, war_id: int, max_pages: int = 1) -> List[Dict]:
        data = await self._request("GET", f"war/{war_id}/killmails/", params={"max_pages": max_pages})
        return data.get("killmails", [])

    async def killmail(self, km_id: int, km_hash: str) -> Dict:
        return await self._request("GET", f"killmails/{km_id}/{km_hash}")
