"""War watcher: poll ESI for new war declarations, build reports and structure summaries, show them on the Wars
page and (optionally) post them to Discord. The polling logic is the one of eve-war-bot-2."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import __version__
from ..config import Settings
from ..crypto import redact
from ..db import Database
from ..universe import GateGraph
from .census import WarCensus
from .config import WarsSettings
from .discord import DiscordNotifier, NotifyError, WebhookInvalid
from .embeds import MAX_EMBEDS_PER_MESSAGE, build_embed, build_payload
from .esi import WarEsi, WarEsiError
from .report import build_report
from .source import EsiWarSource, War, WarHeader
from .store import WarStore
from .structures import StructureIndex, systems_of

log = logging.getLogger("evedm.wars")

# Clean-database bootstrap: newest IDs to inspect at most, and stop after this many consecutive wars declared
# before the window (IDs are not date-ordered: ESI re-issues old wars under new IDs).
BOOTSTRAP_MAX_LOOKUPS = 300
BOOTSTRAP_STOP_AFTER_OLD = 25
MAX_STRUCTURES_KEPT = 200  # per side in the stored summary


def _utcnow() -> datetime:
    return datetime.now(UTC)


def public_base(settings: Settings, ws: WarsSettings | None = None) -> str | None:
    """The address users open the site at: wars.public_url, else derived from the SSO callback (…/sso/callback)."""
    if ws is not None and ws.public_url:
        return ws.public_url.rstrip("/")
    url = settings.eve.callback_url
    return url[: -len("/sso/callback")] if url.endswith("/sso/callback") else None


class WarService:
    def __init__(
        self,
        ws: WarsSettings,
        db: Database,
        graph: GateGraph,
        esi: WarEsi,
        *,
        notifier: DiscordNotifier | None = None,
        structures: StructureIndex | None = None,
        page_url: str | None = None,
    ):
        self.s = ws
        self.store = WarStore(db)
        self.g = graph
        self.esi = esi
        self.source = EsiWarSource(esi, ws.alliance_member_count_max_corps)
        self.notifier = notifier
        self.structures = structures
        self.census = WarCensus(ws, db, graph, esi, structures) if ws.census_enabled else None
        self.page_url = page_url
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.state: dict[str, Any] = {"cycles": 0, "last_cycle_at": None, "last_error": None, "running": False}

    @classmethod
    def create(cls, ws: WarsSettings, settings: Settings, db: Database, graph: GateGraph, **kw: Any) -> WarService:
        contact = f"; {ws.contact}" if ws.contact else ""
        user_agent = f"EVE-Distance-Meter/{__version__} (wars{contact})"
        esi = WarEsi(settings.eve.esi_base, user_agent, settings.eve.compatibility_date, transport=kw.get("transport"))
        notifier = None
        if ws.discord.enabled:
            notifier = DiscordNotifier(
                ws.discord.webhook_url,
                ws.discord.min_seconds_between_posts,
                dry_run=ws.discord.dry_run,
                transport=kw.get("discord_transport"),
            )
        structures = None
        if ws.structures_enabled:
            structures = StructureIndex(
                graph, ws.structures_url, user_agent, ws.structures_refresh_hours, transport=kw.get("transport")
            )
        base = public_base(settings, ws)
        return cls(
            ws, db, graph, esi, notifier=notifier, structures=structures, page_url=f"{base}/wars" if base else None
        )

    # -- classification -----------------------------------------------------------------------------
    def wanted(self, war_type: str) -> bool:
        return not self.s.statuses or war_type in self.s.statuses

    def classify(self, header: WarHeader, max_age_hours: float) -> str:
        """'pending' (process it), 'filtered' (type not wanted) or 'stale' (declared too long ago).

        A new war ID is not always a new war: ESI re-issues old wars under new IDs, so the declaration time decides.
        """
        if max_age_hours > 0 and header.declared and header.declared < _utcnow() - timedelta(hours=max_age_hours):
            return "stale"
        return "pending" if self.wanted(header.status) else "filtered"

    def _add(self, header: WarHeader, status: str) -> int:
        return self.store.add_if_new(header.war_id, status, header.declared_raw, header.status)

    # -- collecting ---------------------------------------------------------------------------------
    async def bootstrap(self, ids: list[int], hours: float) -> int:
        """Queue wars declared in the last `hours` on a clean database; return the new watermark."""
        queued = older_run = 0
        if hours > 0:
            for wid in reversed(ids[-BOOTSTRAP_MAX_LOOKUPS:]):
                if self._stop.is_set():
                    break
                if self.store.has(wid):
                    continue
                header = await self.source.header(wid)
                if header is None:
                    self.store.mark_marker(wid, "skipped")
                    continue
                status = self.classify(header, hours)
                self._add(header, status)
                queued += status == "pending"
                older_run = older_run + 1 if status == "stale" else 0
                if older_run >= BOOTSTRAP_STOP_AFTER_OLD:
                    break
        log.info("bootstrap: %d war(s) declared in the last %gh queued", queued, hours)
        return ids[-1]

    async def resolve_watermark(self, ids: list[int]) -> int:
        """Highest war ID already handled, bootstrapping or healing it if needed."""
        newest = ids[-1]
        raw = self.store.get_meta("watermark_war_id")
        wm = int(raw) if raw else 0
        if wm > newest:  # ESI never issues IDs above its newest war: stale or test data
            purged = self.store.purge_wars_above(newest)
            log.warning(
                "stored watermark %s is ahead of the newest ESI war %s; removed %d row(s), bootstrapping again",
                wm,
                newest,
                purged,
            )
            wm = 0
        if wm:
            return wm
        last = self.store.max_war_id()
        if self.store.bootstrapped() and 0 < last <= newest:
            wm = last
            log.info("continuing after the last stored war %s", wm)
        else:
            wm = await self.bootstrap(ids, float(self.s.bootstrap_hours))
        self.store.set_meta("watermark_war_id", str(wm))
        self.store.set_bootstrapped()
        return wm

    async def collect_new_wars(self) -> int:
        ids = await self.source.newest_war_ids()
        if not ids:
            log.warning("ESI returned no war IDs")
            return 0
        wm = await self.resolve_watermark(ids)
        candidates = [i for i in ids if i > wm and not self.store.has(i)]
        todo = candidates[: self.s.max_new_per_cycle]
        max_age = float(self.s.max_war_age_hours)
        new = done = 0
        for wid in todo:
            if self._stop.is_set():
                break
            try:
                header = await self.source.header(wid)
            except WarEsiError as e:
                log.warning("could not read war %s from ESI (%s) - retry next cycle", wid, e)
                break
            if header is None:
                self.store.mark_marker(wid, "skipped")
            else:
                status = self.classify(header, max_age)
                if status == "stale":
                    log.info("war %s was declared %s - re-issued old war, skipped", wid, header.declared)
                added = self._add(header, status)
                if status == "pending":
                    new += added
            done += 1
        if done:
            self.store.set_meta("watermark_war_id", str(max(wm, todo[done - 1])))
        rest = len(candidates) - len(todo)
        log.info("ESI: %d war IDs listed, %d new%s", len(ids), new, f", {rest} more next cycle" if rest > 0 else "")
        return new

    # -- building -----------------------------------------------------------------------------------
    async def structure_summaries(self, war: War) -> dict[str, Any] | None:
        """Public structures of both sides, with type and corporation names resolved."""
        if self.structures is None or not self.structures.loaded:
            return None
        result: dict[str, Any] = {
            "aggressor": await self.structures.summarize(self.esi, war.aggressor),
            "defender": await self.structures.summarize(self.esi, war.defender),
        }
        ids: set[int] = set()
        for side in ("aggressor", "defender"):
            summary = result[side]
            if summary:
                ids.update(int(t) for t in summary["by_type"])
                ids.update(int(c) for c, _ in summary["corps"])
        try:
            result["names"] = {i: n for i, (n, _) in (await self.esi.names(ids)).items()}
        except WarEsiError as e:
            log.debug("structure name lookup failed: %s", e)
            result["names"] = {}
        return result

    def _targets(self, structures: dict[str, Any] | None, report: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Systems worth ranking characters against: structure systems of both sides, then hot systems."""
        out: dict[int, dict[str, Any]] = {}
        for side in ("defender", "aggressor"):
            for sysinfo in systems_of((structures or {}).get(side)):
                entry = out.setdefault(sysinfo["id"], sysinfo | {"why": []})
                entry["why"].append(f"{sysinfo['structures']} {side} structure(s)")
        for name, kills in (report or {}).get("top_systems", []):
            name = str(name)
            idx = self.g.index.get(int(name)) if name.isdigit() else self.g.find(name)  # unresolved name = ID
            if idx is None:
                continue
            d = self.g.describe(idx)
            entry = out.setdefault(d["id"], d | {"why": []})
            entry["why"].append(f"{kills} recent kill(s)")
        return list(out.values())

    def summary(self, war: War, report: dict | None, structures: dict | None) -> dict[str, Any]:
        names = (structures or {}).get("names", {})
        sides: dict[str, Any] = {}
        for side in ("aggressor", "defender"):
            s = (structures or {}).get(side)
            if s is None:
                sides[side] = None
                continue
            items = [i | {"type": names.get(int(i["type_id"]), str(i["type_id"]))} for i in s["structures"]]
            sides[side] = {
                "total": s["total"],
                "by_type": {names.get(int(t), str(t)): n for t, n in s["by_type"].items()},
                "corps": [[names.get(int(c), str(c)), n] for c, n in s["corps"]],
                "corp_count": s["corp_count"],
                "structures": items[:MAX_STRUCTURES_KEPT],
                "systems": systems_of(s),
            }
        return {
            "war_id": war.war_id,
            "declared": war.declared_at,
            "type": war.status,
            "report_url": war.report_url,
            "aggressor": war.aggressor.as_dict(),
            "defender": war.defender.as_dict(),
            "report": report,
            "structures": sides if structures else None,
            "targets": self._targets(structures, report),
            "built_at": time.time(),
        }

    async def process_pending(self) -> int:
        """Build every pending war; post to Discord when enabled. Returns the number of wars finished."""
        ready: list[tuple[int, dict]] = []
        done = 0
        for wid in self.store.pending_ids(limit=self.s.max_new_per_cycle):
            if self._stop.is_set():
                break
            try:
                war = await self.source.war(wid)
            except WarEsiError as e:
                log.warning("war %s: ESI unavailable (%s) - retry next cycle", wid, e)
                continue
            if war is None:
                self.store.mark_marker(wid, "skipped")
                continue
            report = None
            if self.s.report_enabled:
                report = await build_report(
                    self.esi,
                    wid,
                    self.s.report_recent_kills,
                    self.s.report_kill_details_limit,
                    self.s.report_max_killmail_pages,
                )
            structures = await self.structure_summaries(war)
            try:
                summary = self.summary(war, report, structures)
            except Exception as e:  # one bad war must not block the others
                log.exception("war %s: could not build the summary", wid)
                self.store.mark_failed_attempt([wid], f"summary: {e!r}")
                continue
            if self.notifier is None:
                self.store.save_summary(wid, summary, "ready")
                done += 1
                continue
            self.store.save_summary(wid, summary)
            try:
                url = f"{self.page_url}#war-{wid}" if self.page_url else None
                ready.append((wid, build_embed(war, report, structures, url)))
            except Exception as e:
                log.exception("war %s: could not build the embed", wid)
                self.store.mark_failed_attempt([wid], f"embed: {e}")
        if self.notifier is not None:
            done += await self._post(ready)
        return done

    async def _post(self, ready: list[tuple[int, dict]]) -> int:
        assert self.notifier is not None
        size = max(1, min(int(self.s.discord.batch_embeds), MAX_EMBEDS_PER_MESSAGE))
        posted = 0
        for i in range(0, len(ready), size):
            if self._stop.is_set():
                break
            batch = ready[i : i + size]
            ids = [wid for wid, _ in batch]
            payload = build_payload([e for _, e in batch], self.s.discord.mention_role_id)
            try:
                msg = await self.notifier.send(payload)
            except WebhookInvalid:
                raise
            except NotifyError as e:
                self.store.mark_failed_attempt(ids, str(e))
                continue
            self.store.mark_sent(ids, msg.get("id"))
            log.info("posted war(s) %s -> message %s", ids, msg.get("id"))
            posted += len(ids)
        return posted

    async def run_cycle(self) -> None:
        await self.collect_new_wars()
        if self.structures is not None:
            await self.structures.refresh_if_due()
        try:
            await self.process_pending()
        finally:  # the census does not depend on Discord: it runs even when posting failed
            await self.run_census()

    async def run_census(self) -> None:
        if self.census is None:
            return
        try:
            await self.census.run()
            self.state["census_error"] = None
        except WarEsiError as e:
            self.state["census_error"] = redact(f"ESI: {e}")
            log.warning("census step failed (retried next cycle): %s", e)

    # -- background worker ---------------------------------------------------------------------------
    async def _loop(self) -> None:
        self.state["running"] = True
        try:
            while not self._stop.is_set():
                try:
                    await self.run_cycle()
                    self.state["last_error"] = None
                except WebhookInvalid as e:
                    self.state["last_error"] = redact(f"Discord webhook invalid: {e}")
                    log.critical("Discord webhook invalid: %s - fix the wars config and restart", e)
                except WarEsiError as e:
                    self.state["last_error"] = redact(f"ESI: {e}")
                    log.error("ESI error: %s", e)
                except Exception as e:
                    self.state["last_error"] = redact(repr(e))
                    log.exception("war cycle failed")
                self.state["cycles"] += 1
                self.state["last_cycle_at"] = time.time()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.s.poll_interval_min * 60)
                except TimeoutError:
                    pass
        finally:
            self.state["running"] = False

    def start(self) -> None:
        if self._task is None:
            log.info("war watcher started: %s", self.s.summary())
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except TimeoutError:
                self._task.cancel()  # a cycle stuck in ESI retries: cancel it and wait for it to unwind
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        await self.esi.close()

    def status(self) -> dict[str, Any]:
        return {
            **self.state,
            "settings": self.s.summary(),
            "stored": self.store.stats(),
            "structures_loaded": bool(self.structures and self.structures.loaded),
            "structure_owners": self.structures.owners if self.structures else 0,
            "census": self.census.last_run if self.census else None,
        }
