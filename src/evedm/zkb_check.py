"""zKillboard confirmation of an asset safety notification (NTF-27..NTF-33).

When a structure is destroyed its items go to asset safety and the owners get "items moved to safety". The killmail
usually follows within minutes, but zKillboard may publish it late. The question asked here: is there a killmail whose
victim is a structure of this type, in this system, within ``window_s`` of the notification's own timestamp? It is
asked again every ``interval_s`` until ``duration_s`` after the notification was stored, in two places:

1. the live killmail feed that the alerts page already follows (``structure_alerts``, free), then
2. zKillboard's API (``/api/kills/systemID/{system}/shipTypeID/{type}/pastSeconds/{n}/``), at most one request per
   ``min_api_gap_s`` for all checks together, paused after a 403/429 and never fatal: an error is recorded and the
   check goes on with the feed alone.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger("evedm.zkb")
ZKILL = "https://zkillboard.com"
BLOCKED_BACKOFF_S = 600.0  # as the R2Z2 feed: a 403/429 means slow down
MAX_PAST_S = 7 * 86400  # the longest pastSeconds zKillboard accepts


class ZkbError(Exception):
    """The zKillboard API could not answer this time (the check goes on without it)."""


def parse_time(text: Any) -> float | None:
    """Epoch seconds of a killmail or notification time (ISO 8601 with Z), None when it cannot be read."""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def kill_url(killmail_id: int) -> str:
    return f"{ZKILL}/kill/{killmail_id}/"


def minutes(seconds: float) -> str:
    return f"{seconds / 60:g}"


def line(check: Any, system: str, type_name: str, window_s: float) -> dict[str, Any] | None:
    """The one-line state of a check, worded the same on the page and in Discord: {status, text, url, ...}.
    ``text`` ends where the link goes (" — <link>"); None for a check that was not made (skipped)."""
    status = check["status"]
    where = system or "the system"
    what = type_name or "structure"
    if status == "found":
        stamp = parse_time(check["killmail_time"])
        at = datetime.fromtimestamp(stamp, UTC).strftime("%H:%M") if stamp is not None else "?"
        text = f"zKillboard: {what} destroyed in {where} at {at}"
        return {"status": status, "text": text, "url": check["zkb_url"], "source": check["source"]}
    if status == "not_found":
        text = f"zKillboard: no {what} kill in {where} within {minutes(window_s)} min"
        return {"status": status, "text": text, "url": None, "source": None}
    if status == "pending":
        text = f"zKillboard: checking… ({check['checks']} of {check['planned']})"
        return {"status": status, "text": text, "url": None, "source": None}
    return None


def match_feed(rows: list[Any], event_at: float, window_s: float) -> Any | None:
    """The killmail of the live feed closest in time to the notification, inside the window."""
    best, best_gap = None, None
    for r in rows:
        stamp = parse_time(r["killmail_time"])
        if stamp is None:
            continue
        gap = abs(stamp - event_at)
        if gap <= window_s and (best_gap is None or gap < best_gap):
            best, best_gap = r, gap
    return best


class ZkbApi:
    """zKillboard's kill list API. One shared instance: the gap between requests and the 403/429 pause hold for
    every check."""

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        *,
        min_gap_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base = base_url.rstrip("/")
        self.min_gap_s = min_gap_s
        self.blocked_until = 0.0
        self.last_request_at = 0.0
        self.stats = {"requests": 0, "blocked": 0, "errors": 0}
        self.http = httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.http.aclose()

    def ready_in(self, now: float | None = None) -> float:
        """Seconds until a request is allowed (0: now): the gap since the last one and any 403/429 pause."""
        now = time.time() if now is None else now
        return max(0.0, self.last_request_at + self.min_gap_s - now, self.blocked_until - now)

    def url(self, system_id: int, type_id: int, past_s: float) -> str:
        past = int(min(MAX_PAST_S, max(60, past_s)))
        return f"{self.base}/kills/systemID/{system_id}/shipTypeID/{type_id}/pastSeconds/{past}/"

    async def kills(
        self, system_id: int, type_id: int, past_s: float, now: float | None = None
    ) -> list[dict[str, Any]]:
        """The kills of that ship type in that system during the last ``past_s`` seconds (newest first)."""
        now = time.time() if now is None else now
        self.last_request_at = now  # the gap counts from the attempt: a failing server is not hammered either
        self.stats["requests"] += 1
        try:
            r = await self.http.get(self.url(system_id, type_id, past_s))
        except httpx.HTTPError as exc:
            self.stats["errors"] += 1
            raise ZkbError(f"zKillboard API: {type(exc).__name__}") from exc
        if r.status_code in (403, 429):
            self.blocked_until = now + BLOCKED_BACKOFF_S
            self.stats["blocked"] += 1
            log.warning("zKillboard API refused the request (%s): pausing %.0f s", r.status_code, BLOCKED_BACKOFF_S)
            raise ZkbError(f"zKillboard API: HTTP {r.status_code} (paused {BLOCKED_BACKOFF_S:.0f} s)")
        if r.status_code != 200:
            self.stats["errors"] += 1
            raise ZkbError(f"zKillboard API: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as exc:
            self.stats["errors"] += 1
            raise ZkbError("zKillboard API: not JSON") from exc
        if not isinstance(body, list):
            self.stats["errors"] += 1
            raise ZkbError("zKillboard API: unexpected answer")
        return [k for k in body if isinstance(k, dict)]


def listed(kill: dict[str, Any]) -> tuple[int | None, str | None, float | None, int | None, int | None]:
    """(killmail_id, hash, time, victim ship type, system) of one list entry; what the list does not carry is None.
    zKillboard lists carry ``killmail_id`` and ``zkb.hash``; full killmails also carry time, victim and system."""
    km = kill.get("killmail") if isinstance(kill.get("killmail"), dict) else kill
    zkb = kill.get("zkb") if isinstance(kill.get("zkb"), dict) else {}
    kid = kill.get("killmail_id") or km.get("killmail_id")
    victim = km.get("victim") if isinstance(km.get("victim"), dict) else {}
    return (
        int(kid) if kid else None,
        zkb.get("hash") or km.get("killmail_hash") or kill.get("killmail_hash"),
        parse_time(km.get("killmail_time")),
        victim.get("ship_type_id"),
        km.get("solar_system_id"),
    )
