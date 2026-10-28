"""EVE War Bot v2 - Entry point."""
from __future__ import annotations
import argparse
import logging
import signal
import sys
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, load_credentials, load_settings
from .discord_notifier import DiscordNotifier, WebhookInvalid, NotifyError
from .embeds import build_embed, build_payload
import requests

from .esi import EsiClient, EsiError
from .models import War
from .report import build_report, is_over
from .source import EsiWarSource
from .store import WarStore
from .structures import StructureIndex, make_structures

PROJECT_URL = "https://github.com/Headruner/eve-war-bot-2"

log = logging.getLogger("eve_war_bot")
_stop = asyncio.Event()

def user_agent(creds: Dict) -> str:
    parts = [creds.get("contact"), f"+{PROJECT_URL}"]
    return f"eve-war-bot/{__version__} ({'; '.join(filter(None, parts))}) python-requests/{requests.__version__}"

def make_esi(s: Dict, creds: Dict, store: Optional[WarStore] = None) -> EsiClient:
    return EsiClient(s.get("esi_base_url"), user_agent(creds), 15.0,
                     s.get("esi_compatibility_date"),
                     store.names_get if store else None, store.names_put if store else None)

async def collect_new_wars(s: Dict, store: WarStore, source: EsiWarSource) -> int:
    ids = await source.newest_war_ids()
    if not ids:
        log.warning("ESI returned no war IDs")
        return 0
    
    wm = await store.get_meta("watermark_war_id")
    wm = int(wm) if wm is not None else 0
    
    if wm == 0:
        if await store.bootstrapped:
            wm = await store.max_war_id() or ids[-1]
            log.info("Upgrade from 1.x: continuing after war %s", wm)
        elif s.get("bootstrap_silent"):
            wm = ids[-1]
            log.info("Bootstrap: newest war ID is %s - wars declared from now on will be posted.", wm)
        else:
            wm = 0
        await store.set_meta("watermark_war_id", str(wm))
        if not await store.bootstrapped:
            await store.set_meta("bootstrapped_at", datetime.now(timezone.utc).isoformat())

    candidates = [i for i in ids if i > wm and not await store.has(i)]
    todo = candidates[: s.get("max_new_per_cycle", 50)]
    new, done = 0, 0
    
    for wid in todo:
        if _stop.is_set(): break
        try:
            w = await source.war(wid)
            if w is None:
                await store.add_marker(wid, "skipped")
            else:
                # Filter logic
                count = await store.add_if_new(w, "pending" if w.status in s.get("statuses", []) else "filtered")
                new += count
        except EsiError as e:
            log.warning("Could not read war %s from ESI (%s) - retry next cycle", wid, e)
            break
        done += 1
    
    if done:
        await store.set_meta("watermark_war_id", str(max(wm, todo[done - 1])))
    
    rest = len(candidates) - len(todo)
    log.info("ESI: %d war IDs listed, %d new%s.", len(ids), new,
             f", {rest} more next cycle" if rest > 0 else "")
    return new

async def run_cycle(s: Dict, creds: Dict, store: WarStore, notifier: DiscordNotifier,
                    esi: EsiClient, source: Optional[EsiWarSource] = None,
                    structures: Optional[StructureIndex] = None) -> None:
    ua = user_agent(creds)
    source = source or EsiWarSource(esi, s.get("alliance_member_count_max_corps", 20))

    await collect_new_wars(s, store, source)
    if structures:
        await structures.refresh_if_due()

    pending = await store.pending()
    for i in range(0, len(pending), s.get("batch_embeds", 5)):
        if _stop.is_set(): break
        batch = pending[i:i + s.get("batch_embeds", 5)]
        embeds, files = [], []
        for w in batch:
            # Banner logic
            fname = None
            if s.get("banner_enabled"):
                # from app.banner import render_banner
                # png = render_banner(w, s, ua)
                # if png:
                #     fname = f"war_{w.war_id}.png"
                #     files.append((fname, png))
                pass
            
            report = await build_report(esi, w.war_id, s.get("report_recent_kills", 5),
                                       s.get("report_kill_details_limit", 25),
                                       s.get("report_max_killmail_pages", 1))
            
            # Fetch structure summaries
            struct_summary = {}
            if structures:
                struct_summary = await structures.summarize(esi, w)

            embeds.append(build_embed(w, s, fname, report, structures=struct_summary))
        
        payload = build_payload(embeds, s, creds.get("mention_role_id", ""))
        ids = [w.war_id for w in batch]
        try:
            msg = await notifier.send(payload, files or None)
            await store.mark_sent(ids, msg.get("id"), [a["id"] for a in msg.get("attachments", [])])
            log.info("Notified war(s) %s -> message %s", ids, msg.get("id"))
        except WebhookInvalid:
            raise
        except NotifyError as e:
            await store.mark_failed_attempt(ids, str(e))

    # Live updates...
    if s.get("report_enabled") and s.get("live_updates"):
        # Placeholder for update_live_reports
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

    store = WarStore(s.get("sqlite_path"))
    await store.connect()
    
    notifier = DiscordNotifier(creds.get("webhook_url", ""), s.get("min_seconds_between_posts", 2),
                               dry_run=s.get("dry_run", False))
    esi = make_esi(s, creds, store)
    
    if s.get("structures_enabled"):
        structures = make_structures(s, creds, store)
    else:
        structures = None

    source = EsiWarSource(esi, s.get("alliance_member_count_max_corps", 20))

    logging.info("eve-war-bot %s started (source=ESI %s, compatibility date %s, interval %d min, "
             "report=%s, live=%s, dry_run=%s, stored=%s)",
             __version__, s.get("esi_base_url"), s.get("esi_compatibility_date"), s.get("poll_interval_min"),
             s.get("report_enabled"), s.get("live_updates"), s.get("dry_run"), store.stats())
    
    try:
        while not _stop.is_set():
            try:
                await run_cycle(s, creds, store, notifier, esi, source, structures=structures)
            except EsiError as e:
                logging.error("ESI error: %s", e)
            except Exception as e:
                logging.exception("Cycle failed: %s", e)
            
            await asyncio.sleep(s.get("poll_interval_min", 15) * 60)
            if args.once:
                break
    finally:
        await store.close()

if __name__ == "__main__":
    asyncio.run(main())
