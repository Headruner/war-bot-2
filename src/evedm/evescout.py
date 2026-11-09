"""EVE-Scout's Thera / Turnur wormhole list and the two-leg routes it allows (UC-15, EVS-01..EVS-10).

EVE-Scout publishes the k-space systems that currently hold a wormhole to Thera or Turnur. For a character and a
target the route through such a hub is: gates from the character to the **listed system closest to it**, the
wormhole into the hub, the wormhole out to the **listed system closest to the target**, gates on to the target.
Among equally close listed systems the one with the higher security wins. The list is read on demand, cached for
``refresh_s`` and, when EVE-Scout cannot be reached, the last list is kept.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .config import EveScoutSettings
from .universe import GateGraph

log = logging.getLogger("evedm.evescout")

HUB_IDS = {"thera": 31000005, "turnur": 30002086}  # fallbacks when the feed has no id
WORMHOLE_JUMPS = 2  # system -> hub, hub -> system


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class EveScout:
    def __init__(
        self,
        settings: EveScoutSettings,
        graph: GateGraph,
        http: httpx.AsyncClient | None,
        *,
        user_agent: str = "",
        demo: bool = False,
    ):
        self.s = settings
        self.g = graph
        self.http = http
        self.user_agent = user_agent
        self.demo = demo
        self.hubs: dict[str, dict[str, Any]] = {}  # hub name -> {id, systems: [idx best first], links: {idx: info}}
        self.version = 0
        self.fetched_at = 0.0
        self.error: str | None = None
        self._lock = asyncio.Lock()
        self._searches: dict[tuple[str, tuple[int, ...]], tuple[int, Any]] = {}
        if demo:
            self.load(self.demo_payload())
            self.fetched_at = time.time()

    # -- the list ---------------------------------------------------------------------------------------
    def demo_payload(self) -> list[dict[str, Any]]:
        """Sample connections for demo mode (no network): a few high-sec hubs of trade and travel."""
        sample = {"Thera": ["Jita", "Amarr", "Rens", "Amamake"], "Turnur": ["Tama", "Old Man Star"]}
        out = []
        for hub, names in sample.items():
            for n, name in enumerate(names):
                out.append(
                    {
                        "out_system_name": hub,
                        "out_system_id": HUB_IDS[hub.lower()],
                        "in_system_name": name,
                        "wh_type": "K162",
                        "max_ship_size": "large",
                        "remaining_hours": 20 - n,
                        "signature_type": "wormhole",
                    }
                )
        return out

    def load(self, payload: Any) -> int:
        """Parse EVE-Scout's signature list; returns how many k-space systems it lists for the hubs."""
        wanted = {h.lower(): h for h in self.s.hubs}
        found: dict[str, dict[str, Any]] = {}
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            hub = wanted.get(str(item.get("out_system_name") or "").lower())
            if hub is None:
                continue
            idx = self.g.index.get(_int(item.get("in_system_id")) or -1)
            if idx is None:
                idx = self.g.find(str(item.get("in_system_name") or ""))
            if idx is None or self.g.is_wspace(idx):
                continue  # J-space and unknown systems have no gates to walk
            remaining = item.get("remaining_hours")
            hub_id = _int(item.get("out_system_id")) or HUB_IDS.get(hub.lower())
            entry = found.setdefault(hub, {"id": hub_id, "links": {}})
            info = {
                "system_index": idx,
                "wh_type": item.get("wh_type"),
                "max_ship_size": item.get("max_ship_size"),
                "remaining_hours": remaining,
                "expires_at": item.get("expires_at"),
            }
            old = entry["links"].get(idx)
            if old is None or (info["remaining_hours"] or 0) > (old["remaining_hours"] or 0):
                entry["links"][idx] = info
        hubs: dict[str, dict[str, Any]] = {}
        for name in self.s.hubs:  # configured order
            entry = found.get(name)
            if not entry:
                continue
            # best first: higher security, then name, so the multi-source search prefers them on equal distance
            order = sorted(entry["links"], key=lambda i: (-self.g.sec[i], self.g.names[i]))
            hubs[name] = {"id": entry["id"], "systems": order, "links": entry["links"]}
        self.hubs = hubs
        self.version += 1
        self._searches.clear()
        return sum(len(h["systems"]) for h in hubs.values())

    async def ensure_fresh(self, force: bool = False) -> None:
        """Read the list when older than ``refresh_s`` (one request at a time); keep the old list on failure."""
        if self.demo or self.http is None:
            return
        if not force and time.time() - self.fetched_at < self.s.refresh_s:
            return
        async with self._lock:
            if not force and time.time() - self.fetched_at < self.s.refresh_s:
                return
            try:
                payload = await self._get(self.s.url)
                if isinstance(payload, list):
                    # a custom URL may list one hub only: ask for each missing hub by name (same 15-minute cadence)
                    present = {str(i.get("out_system_name") or "").lower() for i in payload if isinstance(i, dict)}
                    for hub in self.s.hubs:
                        if hub.lower() not in present:
                            sep = "&" if "?" in self.s.url else "?"
                            extra = await self._get(f"{self.s.url}{sep}system_name={hub.lower()}")
                            payload += extra if isinstance(extra, list) else []
                count = self.load(payload)
                self.error = None
                log.info("EVE-Scout: %s listed systems for %s", count, ", ".join(self.hubs) or "no hub")
            except (httpx.HTTPError, ValueError) as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("EVE-Scout list not read (keeping the last one): %s", self.error)
            self.fetched_at = time.time()  # also after a failure: do not hammer a server that is down

    async def _get(self, url: str) -> Any:
        assert self.http is not None
        resp = await self.http.get(
            url,
            headers={"User-Agent": self.user_agent or "EVE-Distance-Meter", "Accept": "application/json"},
            timeout=self.s.timeout_s,
        )
        resp.raise_for_status()
        return resp.json()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "fetched_at": self.fetched_at or None,
            "error": self.error,
            "demo": self.demo,
            "configured": list(self.s.hubs),
            "hubs": {name: len(h["systems"]) for name, h in self.hubs.items()},
            "connections": sum(len(h["systems"]) for h in self.hubs.values()),
        }

    def listing(self) -> list[dict[str, Any]]:
        """Every listed connection with its system, for the API."""
        out = []
        for hub, h in self.hubs.items():
            for idx in h["systems"]:
                info = h["links"][idx]
                out.append(
                    {
                        "hub": hub,
                        "system": self.g.describe(idx),
                        **{k: info[k] for k in ("wh_type", "max_ship_size", "remaining_hours", "expires_at")},
                    }
                )
        return out

    # -- routes ---------------------------------------------------------------------------------------------
    def _search(self, hub: str, avoid: tuple[int, ...]) -> Any:
        key = (hub, avoid)
        cached = self._searches.get(key)
        if cached is not None and cached[0] == self.version:
            return cached[1]
        result = self.g.multi_bfs(self.hubs[hub]["systems"], avoid)
        self._searches[key] = (self.version, result)
        return result

    def hub_name(self, value: str) -> str | None:
        """The configured spelling of a hub (``thera`` -> ``Thera``), or None when it is not configured."""
        wanted = value.strip().lower()
        return next((h for h in self.s.hubs if h.lower() == wanted), None)

    def via(
        self,
        origin: int,
        target: int,
        avoid: list[int] | tuple[int, ...] = (),
        hubs: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        """The shortest route through Thera or Turnur, or None when no hub offers one.

        ``hubs`` limits the search to the named hubs (one radio button = one mid-system); None allows all.

        ``origin`` -> gates -> closest listed system -> hub -> closest listed system to ``target`` -> gates ->
        ``target``. Higher security wins among equally close listed systems. A hub whose two nearest listed
        systems are the same one is skipped: the plain gate route through it is shorter.
        """
        avoid_key = tuple(sorted(avoid))
        best: dict[str, Any] | None = None
        allowed = None if hubs is None else {x.lower() for x in hubs}
        for hub, h in self.hubs.items():
            if allowed is not None and hub.lower() not in allowed:
                continue
            dist, orig, toward = self._search(hub, avoid_key)
            if dist[origin] < 0 or dist[target] < 0:
                continue
            entry, exit_ = orig[origin], orig[target]
            if entry == exit_:
                continue
            to_entry = self.g.walk(toward, origin)  # origin ... entry
            from_exit = list(reversed(self.g.walk(toward, target)))  # exit ... target
            jumps = (len(to_entry) - 1) + WORMHOLE_JUMPS + (len(from_exit) - 1)
            plan = {
                "hub": hub,
                "hub_id": h["id"],
                "entry": entry,
                "exit": exit_,
                "to_entry": to_entry,
                "from_exit": from_exit,
                "jumps": jumps,
                "entry_link": h["links"][entry],
                "exit_link": h["links"][exit_],
            }
            if best is None or jumps < best["jumps"]:
                best = plan
        return best
