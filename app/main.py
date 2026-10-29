"""EVE War Bot v2 - Entry point."""
from __future__ import annotations
import argparse
import logging
import signal
import sys
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

from .config import ConfigError, load_credentials, load_settings
from .discord_notifier import DiscordNotifier, WebhookInvalid, NotifyError
from .embeds import MAX_EMBEDS_PER_MESSAGE, build_embed, build_payload
from .esi import EsiClient, EsiError
from .report import build_report
from .source import EsiWarSource, WarHeader
from .store import WarStore
from .structures import StructureIndex, make_structures

PROJECT_URL = "https://github.com/Headruner/eve-war-bot-2"
__version__ = "2.0.0"

log = logging.getLogger("eve_war_bot")
_stop = asyncio.Event()

# EVE shows security rounded to one decimal; 0.45 and up is high-sec
HIGHSEC_MIN = 0.45

# Clean-database bootstrap: newest IDs to inspect at most, and stop after this
# many consecutive wars declared before the window (IDs are not date-ordered:
# ESI re-issues old wars under new IDs).
BOOTSTRAP_MAX_LOOKUPS = 300
BOOTSTRAP_STOP_AFTER_OLD = 25


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def user_agent(creds: Dict) -> str:
    parts = [creds.get("contact"), f"+{PROJECT_URL}"]
    return f"eve-war-bot/{__version__} ({'; '.join(filter(None, parts))}) python-requests/{requests.__version__}"


def make_esi(s: Dict, creds: Dict) -> EsiClient:
    return EsiClient(s.get("esi_base_url", "https://esi.evetech.net"), user_agent(creds), 15.0,
                     s.get("esi_compatibility_date", "2025-08-26"))


def wanted(s: Dict, status: str) -> bool:
    """Post every war unless config.yaml narrows it with a 'statuses' list."""
    statuses = s.get("statuses")
    return not statuses or status in statuses


def classify(s: Dict, header: WarHeader, max_age_hours: float) -> str:
    """'pending' (post it), 'filtered' (type not wanted) or 'stale' (declared too long ago).

    A new war ID is not always a new war: ESI re-issues old wars under new IDs
    (often already retracted), so the declaration time decides.
    """
    if max_age_hours > 0 and header.declared and header.declared < _utcnow() - timedelta(hours=max_age_hours):
        return "stale"
    return "pending" if wanted(s, header.status) else "filtered"


async def bootstrap(s: Dict, store: WarStore, source: EsiWarSource, ids: List[int], hours: float) -> int:
    """Queue wars declared in the last `hours` on a clean database; return the new watermark.

    Walks down from the newest ID and classifies each war by its declaration
    time, stopping after a run of older wars. hours <= 0 queues nothing.
    """
    queued = older_run = 0
    if hours > 0:
        for wid in reversed(ids[-BOOTSTRAP_MAX_LOOKUPS:]):
            if _stop.is_set():
                break
            if await store.has(wid):
                continue
            header = await source.header(wid)
            if header is None:
                await store.mark_marker(wid, "skipped")
                continue
            status = classify(s, header, hours)
            await store.add_if_new(header, status)
            queued += status == "pending"
            older_run = older_run + 1 if status == "stale" else 0
            if older_run >= BOOTSTRAP_STOP_AFTER_OLD:
                break
    log.info("Bootstrap: %d war(s) declared in the last %gh queued for posting", queued, hours)
    return ids[-1]


async def resolve_watermark(s: Dict, store: WarStore, source: EsiWarSource, ids: List[int]) -> int:
    """Return the highest war ID already handled, bootstrapping or healing it if needed."""
    newest = ids[-1]
    raw = await store.get_meta("watermark_war_id")
    wm = int(raw) if raw else 0

    if wm > newest:
        # ESI never issues IDs above its newest war: this is stale or test data
        purged = await store.purge_wars_above(newest)
        log.warning("Stored watermark %s is ahead of the newest ESI war %s (stale or test data); "
                    "removed %d bogus row(s), bootstrapping again", wm, newest, purged)
        wm = 0
    if wm:
        return wm

    last = await store.max_war_id()
    if await store.bootstrapped() and 0 < last <= newest:
        wm = last
        log.info("Continuing after the last stored war %s", wm)
    else:
        hours = 0.0 if s.get("bootstrap_silent") else float(s.get("bootstrap_hours", 24))
        wm = await bootstrap(s, store, source, ids, hours)
    await store.set_meta("watermark_war_id", str(wm))
    await store.set_bootstrapped()
    return wm


async def collect_new_wars(s: Dict, store: WarStore, source: EsiWarSource) -> int:
    ids = await source.newest_war_ids()
    if not ids:
        log.warning("ESI returned no war IDs")
        return 0

    wm = await resolve_watermark(s, store, source, ids)

    candidates = [i for i in ids if i > wm and not await store.has(i)]
    todo = candidates[: s.get("max_new_per_cycle", 50)]
    max_age = float(s.get("max_war_age_hours", 48))
    new, done = 0, 0

    for wid in todo:
        if _stop.is_set():
            break
        try:
            header = await source.header(wid)
        except EsiError as e:
            log.warning("Could not read war %s from ESI (%s) - retry next cycle", wid, e)
            break
        if header is None:
            await store.mark_marker(wid, "skipped")
        else:
            status = classify(s, header, max_age)
            if status == "stale":
                log.info("War %s was declared %s - re-issued old war, not posting", wid, header.declared)
            added = await store.add_if_new(header, status)
            if status == "pending":
                new += added
        done += 1

    if done:
        await store.set_meta("watermark_war_id", str(max(wm, todo[done - 1])))

    rest = len(candidates) - len(todo)
    log.info("ESI: %d war IDs listed, %d new%s.", len(ids), new,
             f", {rest} more next cycle" if rest > 0 else "")
    return new


async def highsec_structures(esi: EsiClient, structures) -> List[Dict]:
    """High-sec structures as {system, security, name, type_id}, sorted by system."""
    out = []
    for st in structures:
        if not st.system_id:
            continue
        try:
            system = await esi.system(st.system_id)
        except EsiError as e:
            log.debug("system %s lookup failed: %s", st.system_id, e)
            continue
        security = float(system.get("security_status", -1))
        if security < HIGHSEC_MIN:
            continue
        sys_name = system.get("name", str(st.system_id))
        # dataset names are "System - Structure"; keep only the structure part
        name = st.name.split(" - ", 1)[1] if st.name.startswith(f"{sys_name} - ") else st.name
        out.append({"system": sys_name, "security": round(security, 1), "name": name, "type_id": st.type_id})
    return sorted(out, key=lambda x: (x["system"], x["name"]))


async def structure_summaries(esi: EsiClient, structures: StructureIndex, war) -> Optional[Dict]:
    """Public structure counts for both sides, with type and corp names resolved."""
    if not structures.loaded:
        return None
    result = {"aggressor": await structures.summarize(esi, war.aggressor),
              "defender": await structures.summarize(esi, war.defender)}
    ids = set()
    for summary in result.values():
        if summary:
            ids.update(int(t) for t in summary["by_type"])
            ids.update(int(c) for c, _ in summary["corps"])
            summary["highsec"] = await highsec_structures(esi, summary["structures"])
    try:
        result["names"] = {i: n for i, (n, _) in (await esi.names(ids)).items()}
    except EsiError as e:
        log.debug("structure name lookup failed: %s", e)
        result["names"] = {}
    return result


async def post_pending(s: Dict, creds: Dict, store: WarStore, notifier: DiscordNotifier,
                       esi: EsiClient, source: EsiWarSource,
                       structures: Optional[StructureIndex] = None) -> int:
    """Build embeds for pending wars and post them in batches. Returns wars posted."""
    ready: List[Tuple[int, Dict]] = []
    for wid in await store.pending_ids(limit=s.get("max_new_per_cycle", 50)):
        if _stop.is_set():
            break
        try:
            war = await source.war(wid)
        except EsiError as e:
            log.warning("War %s: ESI unavailable (%s) - retry next cycle", wid, e)
            continue
        if war is None:
            await store.mark_marker(wid, "skipped")
            continue
        report = None
        if s.get("report_enabled", True):
            report = await build_report(esi, wid, s.get("report_recent_kills", 5),
                                        s.get("report_kill_details_limit", 25),
                                        s.get("report_max_killmail_pages", 1))
            if report:
                await store.save_report(wid, report)
        struct = await structure_summaries(esi, structures, war) if structures else None
        try:
            ready.append((wid, build_embed(war, s, None, report, structures=struct)))
        except Exception as e:  # one bad war must not block the others
            log.exception("War %s: could not build embed", wid)
            await store.mark_failed_attempt([wid], f"embed: {e}")

    batch_size = max(1, min(int(s.get("batch_embeds", 5)), MAX_EMBEDS_PER_MESSAGE))
    posted = 0
    for i in range(0, len(ready), batch_size):
        if _stop.is_set():
            break
        batch = ready[i:i + batch_size]
        ids = [wid for wid, _ in batch]
        payload = build_payload([embed for _, embed in batch], s, creds.get("mention_role_id", ""))
        try:
            msg = await notifier.send(payload)
        except NotifyError as e:
            await store.mark_failed_attempt(ids, str(e))
            continue
        await store.mark_sent(ids, msg.get("id"), [a.get("id") for a in msg.get("attachments", [])])
        log.info("Posted war(s) %s -> message %s", ids, msg.get("id"))
        posted += len(ids)
    return posted


async def run_cycle(s: Dict, creds: Dict, store: WarStore, notifier: DiscordNotifier,
                    esi: EsiClient, source: Optional[EsiWarSource] = None,
                    structures: Optional[StructureIndex] = None) -> None:
    source = source or EsiWarSource(esi, s.get("alliance_member_count_max_corps", 20))
    await collect_new_wars(s, store, source)
    if structures:
        await structures.refresh_if_due()
    await post_pending(s, creds, store, notifier, esi, source, structures)


async def _sleep(seconds: float) -> None:
    """Sleep, but wake up immediately on shutdown."""
    try:
        await asyncio.wait_for(_stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def main():
    parser = argparse.ArgumentParser(prog="eve-war-bot")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()

    try:
        s = load_settings()
        logging.basicConfig(level=s.get("log_level", "INFO").upper(),
                            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        creds = load_credentials(required=not s.get("dry_run"))
    except ConfigError as e:
        logging.basicConfig()
        logging.critical("Configuration error: %s", e)
        sys.exit(2)

    if args.check_config:
        logging.info("Configuration OK.")
        return

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _stop.set)

    store = WarStore(s.get("sqlite_path"))
    await store.connect()

    notifier = DiscordNotifier(creds.get("webhook_url", ""), s.get("min_seconds_between_posts", 2),
                               dry_run=s.get("dry_run", False))
    esi = make_esi(s, creds)
    structures = make_structures(s, creds, store) if s.get("structures_enabled") else None
    source = EsiWarSource(esi, s.get("alliance_member_count_max_corps", 20))

    log.info("eve-war-bot %s started (source=ESI %s, compatibility date %s, interval %d min, "
             "report=%s, statuses=%s, dry_run=%s, stored=%s)",
             __version__, s.get("esi_base_url"), s.get("esi_compatibility_date"),
             s.get("poll_interval_min", 15), s.get("report_enabled"), s.get("statuses") or "all",
             s.get("dry_run", False), await store.stats())

    try:
        while not _stop.is_set():
            try:
                await run_cycle(s, creds, store, notifier, esi, source, structures=structures)
            except WebhookInvalid as e:
                log.critical("Discord webhook invalid: %s - fix config/credentials.json and restart", e)
            except EsiError as e:
                log.error("ESI error: %s", e)
            except Exception as e:
                log.exception("Cycle failed: %s", e)
            if args.once:
                break
            await _sleep(s.get("poll_interval_min", 15) * 60)
    finally:
        await esi.close()
        await store.close()
        log.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
