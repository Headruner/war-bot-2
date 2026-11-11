"""FastAPI application: pages, JSON API, EVE SSO sign-in and character onboarding."""

from __future__ import annotations

import gzip
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import __version__
from ..access import AllianceGate
from ..alerts import AlertService, seed_demo_alerts
from ..ansiblex import AnsiblexService, seed_demo_gates
from ..bundle import BundleRejected, export_bundle, import_bundle, report
from ..characters import REAUTH, CharacterService
from ..config import WAYPOINT_SCOPE, ConfigError, Settings
from ..crypto import TokenCipher
from ..db import Database
from ..demo import seed_demo, seed_demo_notifications, seed_demo_roster
from ..esi import EsiClient, EsiError
from ..evescout import EveScout
from ..logs import LaunchRecorder, peak_rss_mb, rss_mb, setup_logging, trim_memory
from ..looplag import LoopLagMonitor
from ..notifications import NotificationService
from ..ranking import DemoProvider, LiveProvider, RankingService
from ..resolver import TargetResolver
from ..roster import RosterRejected, RosterService
from ..scout import ResolveError, StructureScout, seed_demo_structures
from ..selfcheck import check
from ..sso import AuthService, SsoError
from ..universe import GateGraph
from ..wars.config import WarsSettings, load_wars_settings
from ..wars.demo import DEMO_CENSUS, DEMO_WARS, seed_demo_census, seed_demo_wars
from ..wars.service import WarService

log = logging.getLogger("evedm.web")
SESSION_RENEW_S = 86400
HERE = Path(__file__).parent


def _asset_version() -> str:
    """Version plus a hash of the static files, so a browser fetches changed scripts and styles after an update
    even when the release number is the same (the ``?v=`` of the static links)."""
    import hashlib

    digest = hashlib.sha1(usedforsecurity=False)
    for f in sorted((HERE / "static").rglob("*")):
        if f.is_file():
            digest.update(f.name.encode() + f.read_bytes())
    return f"{__version__}-{digest.hexdigest()[:8]}"


ASSET_V = _asset_version()


@dataclass
class Services:
    settings: Settings
    db: Database
    graph: GateGraph
    resolver: TargetResolver
    chars: CharacterService
    ranking: RankingService
    auth: AuthService | None
    esi: EsiClient | None
    http: httpx.AsyncClient
    launch: LaunchRecorder
    started: float
    wars: WarService | None = None
    scout: StructureScout | None = None
    roster: RosterService | None = None
    notifications: NotificationService | None = None
    alerts: AlertService | None = None
    gate: AllianceGate | None = None  # who may see the Notifications and Structures tabs (D16)
    evescout: EveScout | None = None  # Thera / Turnur wormholes for the Ranking filter (UC-15)
    ansiblex: AnsiblexService | None = None  # Ansiblex jump gates per character for the Ranking filter (UC-16)
    looplag: LoopLagMonitor | None = None  # how late the shared event loop wakes up


class RequestCounter:
    """Pure ASGI middleware (cheap): counts HTTP requests for the launch record."""

    def __init__(self, app: ASGIApp, owner: dict[str, Any]):
        self.app = app
        self.owner = owner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            svc = self.owner.get("svc")
            if svc is not None:
                svc.launch.requests += 1
        await self.app(scope, receive, send)


def build_services(settings: Settings) -> Services:
    db = Database(settings.db_path, settings.sqlite_journal)
    cipher = TokenCipher(settings.secret_key)
    graph = GateGraph.load(settings.universe_file)
    http = httpx.AsyncClient(
        timeout=settings.eve.timeout_s,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        follow_redirects=False,
    )
    auth = AuthService(settings.eve, http) if settings.eve.client_id else None
    esi = None if settings.demo else EsiClient(settings.eve, http)
    chars = CharacterService(db, cipher, auth, settings)
    provider = DemoProvider(db) if settings.demo else LiveProvider(chars, esi)  # type: ignore[arg-type]
    ranking = RankingService(
        graph,
        db,
        provider,
        esi=esi,
        avoid_names=settings.avoid_systems,
        stale_after_s=settings.web.stale_after_s,
        refresh_interval_s=settings.web.refresh_interval_s,
        max_wait_age_s=settings.web.max_wait_age_s,
    )
    if settings.demo:
        seeded = seed_demo(db, cipher, graph)
        if seeded:
            log.info("seeded %s demo characters", seeded)
    launch = LaunchRecorder(db, settings, "demo" if settings.demo else "live")
    svc = Services(settings, db, graph, TargetResolver(graph), chars, ranking, auth, esi, http, launch, time.time())
    if settings.evescout.enabled:
        svc.evescout = EveScout(
            settings.evescout, graph, http, user_agent=settings.eve.user_agent, demo=settings.demo
        )
    if settings.ansiblex.enabled:
        svc.ansiblex = AnsiblexService(settings.ansiblex, db, graph, chars, esi, demo=settings.demo)
        if settings.demo:
            seed_demo_gates(db, graph)
    svc.gate = AllianceGate(settings.access, db, esi, demo=settings.demo)  # type: ignore[arg-type]
    svc.roster = RosterService(db, graph, svc.resolver, ranking, settings.instance_label)
    if settings.demo:
        seeded_roster = seed_demo_roster(svc.roster)
        if seeded_roster:
            log.info("seeded %s demo roster entries", seeded_roster)
    if settings.structures.enabled:
        svc.scout = StructureScout(settings, db, graph, chars, esi)  # type: ignore[arg-type]
    if settings.notifications.enabled:
        svc.notifications = NotificationService(
            settings.notifications, db, graph, chars, esi, demo=settings.demo
        )
        if settings.demo:
            seeded = seed_demo_notifications(db, graph)
            if seeded:
                log.info("seeded %s demo notifications", seeded)
    return svc


def build_wars(settings: Settings, ws: WarsSettings, svc: Services) -> WarService | None:
    """The war watcher (eve-war-bot-2 logic) when enabled in its own config file; demo mode shows sample wars."""
    if not ws.enabled:
        return None
    wars = WarService.create(ws, settings, svc.db, svc.graph)
    if settings.demo:
        n = seed_demo_wars(wars.store, svc.graph)
        if n:
            log.info("seeded %s demo wars", n)
        index = seed_demo_census(svc.db, svc.graph)
        svc.db.set_meta("demo_census", "1")
        if wars.structures is not None:  # sample structures; never downloaded in demo mode
            wars.structures.set_public(index)
            wars.structures.last_refresh = time.time() + 10 * 365 * 86400
    else:
        if wars.store.purge_demo([w[0] for w in DEMO_WARS]):
            log.info("removed the demo wars from a database now used live")
        if svc.db.get_meta("demo_census"):
            demo_ids = [w[0] for w in DEMO_WARS] + [w[0] for w in DEMO_CENSUS]
            marks = ",".join("?" * len(demo_ids))
            svc.db.execute(f"DELETE FROM war_census WHERE war_id IN ({marks})", tuple(demo_ids))  # noqa: S608
            svc.db.execute("DELETE FROM meta WHERE key='demo_census'")
    return wars


def build_alerts(settings: Settings, ws: WarsSettings, svc: Services) -> AlertService | None:
    """Structure alerts from zKillboard's live killmail feed (demo mode: sample alerts, no feed)."""
    if not settings.alerts.enabled:
        return None
    from ..wars.discord import DiscordNotifier
    from ..wars.service import public_base

    d = settings.alerts.discord
    notifier = DiscordNotifier(d.webhook_url, 2) if d.enabled and not settings.demo else None
    base = public_base(settings, ws)
    alerts = AlertService(
        settings.alerts,
        svc.db,
        svc.graph,
        svc.esi,  # type: ignore[arg-type]
        scout=svc.scout,
        structure_index=svc.wars.structures if svc.wars is not None else None,
        notifier=notifier,
        page_url=f"{base}/alerts" if base else None,
    )
    if settings.demo:
        seed_demo_alerts(svc.db, svc.graph)
    return alerts


def attach_notification_discord(settings: Settings, ws: WarsSettings, svc: Services) -> None:
    """Post notifications to Discord: through their own webhook, else through the wars webhook."""
    ns = svc.notifications
    d = settings.notifications.discord
    if ns is None or not d.enabled or settings.demo:
        return
    from ..wars.discord import DiscordNotifier
    from ..wars.service import public_base

    url = d.webhook_url or ws.discord.webhook_url
    if not url:
        log.info("notifications are not posted to Discord: no webhook (EVEDM_WARS_DISCORD_WEBHOOK)")
        return
    base = public_base(settings, ws)
    ns.notifier = DiscordNotifier(url, ws.discord.min_seconds_between_posts, dry_run=ws.discord.dry_run)
    ns.page_url = f"{base}/notifications" if base else None
    ns.evescout = svc.evescout
    log.info("notifications are posted to Discord (%s webhook)", "their own" if d.webhook_url else "the wars")


def create_app(settings: Settings, wars_settings: WarsSettings | None = None) -> FastAPI:
    settings.validate_for_serve()
    ws = wars_settings if wars_settings is not None else load_wars_settings()
    startup = check(settings)  # dependencies + fully qualified, verified resource paths
    if not startup.ok:
        raise ConfigError("startup check failed: " + "; ".join(startup.problems))
    setup_logging(settings)
    log.info("resolved paths: %s", startup.paths)
    holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        svc = build_services(settings)
        svc.graph.map_payload()  # build the cached map once, before the first visitor
        trim_memory()
        holder["svc"] = svc
        app.state.svc = svc
        svc.wars = build_wars(settings, ws, svc)
        if svc.scout is not None:
            if settings.demo:
                from ..wars.demo import _entity_ids

                seed_demo_structures(svc.db, svc.graph, _entity_ids())
            if svc.wars is not None and svc.wars.structures is not None:
                structures = svc.wars.structures
                svc.scout.on_change.append(structures.set_discovered)  # private finds join the war lists
                structures.set_discovered(svc.scout.index())
        svc.alerts = build_alerts(settings, ws, svc)
        attach_notification_discord(settings, ws, svc)
        svc.looplag = LoopLagMonitor()
        svc.looplag.start()
        svc.launch.start(
            {
                "universe": svc.graph.build,
                "systems": len(svc.graph.ids),
                "journal_mode": svc.db.journal_mode,
                "wars": ws.summary(),
            }
        )
        if svc.wars is not None and not settings.demo:
            svc.wars.start()
        if svc.scout is not None and not settings.demo:
            svc.scout.start()
        if svc.alerts is not None and not settings.demo:
            svc.alerts.start()
        if svc.notifications is not None and not settings.demo:
            svc.notifications.start()
        try:
            yield
        finally:
            if svc.looplag is not None:
                await svc.looplag.stop()
            if svc.notifications is not None:
                await svc.notifications.stop()
            if svc.alerts is not None:
                await svc.alerts.stop()
            if svc.scout is not None:
                await svc.scout.stop()
            if svc.wars is not None:
                await svc.wars.stop()
            svc.launch.stop()
            await svc.http.aclose()
            svc.db.close()

    app = FastAPI(
        title="EVE Distance Meter",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="evedm_session",
        max_age=settings.web.session_days * 86400,
        same_site="lax",
        https_only=settings.web.cookie_secure,
    )
    app.add_middleware(RequestCounter, owner=holder)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    def svc_of(request: Request) -> Services:
        return request.app.state.svc

    def user_of(request: Request) -> dict[str, Any] | None:
        """The signed-in main ({id, name}); id 0 is the single local user when web.auth is none.

        The session cookie (signed, HttpOnly, renewed on every response, valid ``web.session_days``)
        only carries the main's ID: characters and tokens stay encrypted in the database, so a user
        signs in once per browser and never re-adds alts.
        """
        if settings.web.auth == "none":
            return {"id": 0, "name": "local"}
        user = request.session.get("user")
        if not isinstance(user, dict) or "id" not in user:
            return None
        uid = int(user["id"])
        allowed = settings.web.allowed_login_characters
        if (allowed and uid not in allowed) or svc_of(request).db.get_user(uid) is None:
            request.session.clear()  # account deleted, or the main was taken off the allow list
            return None
        now = int(time.time())
        if now - int(request.session.get("seen", 0)) > SESSION_RENEW_S:
            # re-sign the cookie at most once a day: the session_days count from the last visit, not the sign-in
            request.session["seen"] = now
        return user

    def require_user(request: Request) -> dict[str, Any]:
        user = user_of(request)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in required")
        return user

    def owner(request: Request) -> int:
        return int(require_user(request)["id"])

    async def json_body(request: Request) -> dict[str, Any]:
        """The request body as a JSON object; 400 for anything else (never a 500)."""
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="request body must be JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        return body

    def require_csrf(request: Request) -> None:
        if request.headers.get("x-evedm") != "1":
            raise HTTPException(status_code=403, detail="missing X-EVEDM header")

    def is_member(request: Request) -> bool:
        """True when the signed-in main's alliance may see Notifications and Structures (cached, no ESI call)."""
        user = user_of(request)
        gate = svc_of(request).gate
        return user is not None and gate is not None and gate.allowed(int(user["id"]))

    async def require_member(request: Request, uid: int) -> None:
        """404 unless the main's alliance may see the members-only tabs; reads a stale alliance first (D16)."""
        gate = svc_of(request).gate
        if gate is not None:
            await gate.refresh(uid)
        if gate is None or not gate.allowed(uid):
            raise HTTPException(status_code=404, detail="this section is open to one alliance only")

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        svc = svc_of(request)
        member = is_member(request)
        base = {
            "request": request,
            "version": __version__,
            "asset_v": ASSET_V,
            "user": user_of(request),
            "demo": settings.demo,
            "auth": settings.web.auth,
            "auto_refresh_s": settings.web.auto_refresh_s,
            "sso_ready": svc.auth is not None,
            "universe": svc.graph.build,
            "session_days": settings.web.session_days,
            "wars_enabled": svc.wars is not None,
            "structures_enabled": svc.scout is not None and member,
            "alerts_enabled": svc.alerts is not None,
            "evescout_enabled": svc.evescout is not None,
            "evescout_hubs": list(svc.evescout.s.hubs) if svc.evescout is not None else [],
            "ansiblex_enabled": svc.ansiblex is not None,
            "min_structures": settings.notifications.min_structures,
            "notifications_enabled": svc.notifications is not None and member,
        }
        return templates.TemplateResponse(request, name, base | ctx)

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        return page(request, "index.html")

    @app.get("/characters", response_class=HTMLResponse)
    async def characters_page(request: Request) -> Response:
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        return page(
            request,
            "characters.html",
            added=request.query_params.get("added"),
            error=request.query_params.get("error"),
            welcome=request.query_params.get("welcome"),
        )

    @app.get("/roster", response_class=HTMLResponse)
    async def roster_page(request: Request) -> Response:
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        return page(request, "roster.html")

    @app.get("/notifications", response_class=HTMLResponse)
    async def notifications_page(request: Request) -> Response:
        user = user_of(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        ns = svc_of(request).notifications
        if ns is None:
            raise HTTPException(status_code=404, detail="notifications are disabled (notifications.enabled)")
        await require_member(request, int(user["id"]))
        return page(request, "notifications.html", poll_s=int(settings.notifications.poll_s))

    @app.get("/alerts", response_class=HTMLResponse)
    async def alerts_page(request: Request) -> Response:
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        if svc_of(request).alerts is None:
            raise HTTPException(status_code=404, detail="structure alerts are disabled (alerts.enabled)")
        return page(request, "alerts.html")

    @app.get("/structures", response_class=HTMLResponse)
    async def structures_page(request: Request) -> Response:
        user = user_of(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        if svc_of(request).scout is None:
            raise HTTPException(status_code=404, detail="the structure scout is disabled (structures.enabled)")
        await require_member(request, int(user["id"]))
        return page(request, "structures.html", scopes=settings.eve.optional_scopes)

    @app.get("/wars", response_class=HTMLResponse)
    async def wars_page(request: Request) -> Response:
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        if svc_of(request).wars is None:
            raise HTTPException(status_code=404, detail="the war watcher is disabled (config/wars.yaml)")
        return page(request, "wars.html", census=svc_of(request).wars.census is not None)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        return page(request, "login.html", error=request.query_params.get("error"))

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------------ SSO
    def _start_sso(request: Request, purpose: str, scopes: list[str]) -> Response:
        svc = svc_of(request)
        if svc.auth is None:
            return RedirectResponse("/login?error=sso_not_configured", status_code=303)
        state = svc.auth.new_state()
        svc.db.create_state(state, purpose)
        return RedirectResponse(svc.auth.authorize_url(state, scopes), status_code=303)

    @app.get("/sso/login")
    async def sso_login(request: Request) -> Response:
        # the main is the first linked character, so sign-in asks for the same scopes as Add character
        return _start_sso(request, "login", settings.eve.requested_scopes)

    @app.get("/sso/add")
    async def sso_add(request: Request) -> Response:  # SSO-01
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        return _start_sso(request, "add", settings.eve.requested_scopes)

    @app.get("/sso/callback")
    async def sso_callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
        svc = svc_of(request)
        if error:  # EVE SSO refused, e.g. invalid_scope when the EVE application lacks a requested scope
            purpose = svc.db.consume_state(state) if state else None
            log.warning("EVE SSO returned an error: %s", error)
            target = "/characters" if purpose == "add" and user_of(request) else "/login"
            return RedirectResponse(f"{target}?" + urlencode({"error": error[:60]}), status_code=303)
        purpose = svc.db.consume_state(state) if state else None
        if purpose is None or not code or svc.auth is None:  # SSO-04
            log.warning("rejected SSO callback (state missing, unknown, used or expired)")
            return RedirectResponse("/login?error=invalid_state", status_code=303)
        try:
            ts = await svc.auth.exchange_code(code)
        except SsoError as exc:
            log.warning("SSO code exchange failed: %s", exc.code)
            target = "/login" if purpose == "login" else "/characters"
            return RedirectResponse(f"{target}?error={exc.code}", status_code=303)
        ident = ts.identity
        if purpose == "login":
            allowed = settings.web.allowed_login_characters
            if allowed and ident.character_id not in allowed:
                log.warning("sign-in refused for character %s", ident.character_id)
                return RedirectResponse("/login?error=not_allowed", status_code=303)
            is_new = svc.db.upsert_user(ident.character_id, ident.name)
            if is_new and not settings.demo and svc.db.get_character(ident.character_id, 0) is not None:
                # upgrade from the single-user version: the characters stored before accounts existed
                # belong to whoever owned them then; a main that is one of them takes them all over
                moved = svc.db.reassign_owner(0, ident.character_id)
                log.info("main %s adopted %s characters stored before accounts existed", ident.character_id, moved)
            svc.chars.store(ts, owner_id=ident.character_id)  # the main is linked to itself
            if svc.gate is not None:
                # read the main's alliance now, so the Notifications and Structures links show on the first page
                try:
                    await svc.gate.refresh(ident.character_id, force=True)
                except Exception as exc:  # never block a sign-in on ESI
                    log.info("could not read the affiliation of main %s at sign-in: %s", ident.character_id, exc)
            request.session.clear()
            request.session["user"] = {"id": ident.character_id, "name": ident.name}
            request.session["seen"] = int(time.time())
            log.info("main %s signed in (%s)", ident.character_id, "new account" if is_new else "returning")
            return RedirectResponse("/characters?welcome=1" if is_new else "/", status_code=303)
        user = user_of(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        svc.chars.store(ts, owner_id=int(user["id"]))  # SSO-03 / SSO-05 / SSO-06, linked to the signed-in main
        return RedirectResponse("/characters?" + urlencode({"added": ident.name}), status_code=303)

    # ------------------------------------------------------------------ API: public
    @app.get("/api/health")
    async def health(request: Request) -> JSONResponse:
        svc = svc_of(request)
        return JSONResponse(
            {
                "status": "ok" if svc.db.ok() else "degraded",
                "version": __version__,
                "mode": "demo" if settings.demo else "live",
                "universe": {"build": svc.graph.build, "systems": len(svc.graph.ids), "edges": svc.graph.edge_count},
                "db": {"path": str(settings.db_path), "journal_mode": svc.db.journal_mode},
                "rss_mb": rss_mb(),
                "peak_rss_mb": peak_rss_mb(),
                "uptime_s": round(time.time() - svc.started),
                "requests": svc.launch.requests,
                "esi": svc.esi.stats if svc.esi else None,
                "wars": None
                if svc.wars is None
                else {"running": svc.wars.state["running"], "cycles": svc.wars.state["cycles"]},
                "structure_scout": None
                if svc.scout is None
                else {"running": svc.scout.state["running"], "cycles": svc.scout.state["cycles"]},
                "notifications": None
                if svc.notifications is None
                else {
                    "running": svc.notifications.state["running"],
                    "polls": svc.notifications.state["polls"],
                    "lag": svc.notifications.lag_brief(),
                },
                "alerts": None
                if svc.alerts is None
                else {"running": svc.alerts.state["running"], "polls": svc.alerts.state["polls"]},
                "event_loop": None if svc.looplag is None else svc.looplag.snapshot(),
            }
        )

    @app.get("/api/universe/map")
    async def universe_map(request: Request) -> Response:
        payload, etag = svc_of(request).graph.map_payload()
        headers = {"ETag": etag, "Cache-Control": "public, max-age=86400", "Vary": "Accept-Encoding"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        if "gzip" in request.headers.get("accept-encoding", ""):
            return Response(payload, media_type="application/json", headers=headers | {"Content-Encoding": "gzip"})
        return Response(gzip.decompress(payload), media_type="application/json", headers=headers)

    # ------------------------------------------------------------------ API: signed in
    @app.get("/api/systems/search")
    async def systems_search(request: Request, q: str = "", limit: int = 10) -> JSONResponse:
        require_user(request)
        return JSONResponse(svc_of(request).resolver.autocomplete(q, min(max(limit, 1), 25)))

    @app.post("/api/target/resolve")
    async def target_resolve(request: Request) -> JSONResponse:
        require_user(request)
        require_csrf(request)
        body = await json_body(request)
        text = str(body.get("text", ""))[:20000]
        return JSONResponse({"candidates": svc_of(request).resolver.extract(text)})

    @app.get("/api/targets/recent")
    async def recent_targets(request: Request) -> JSONResponse:
        uid = owner(request)
        svc = svc_of(request)
        ids = svc.db.recent_targets(owner_id=uid)
        out = [svc.graph.describe(i) for i in (svc.graph.index.get(s) for s in ids) if i is not None]
        return JSONResponse(out)

    @app.get("/api/rank")
    async def rank(request: Request, target: str, refresh: int = 1, thera: int = 0, hub: str = "") -> JSONResponse:
        uid = owner(request)
        svc = svc_of(request)
        idx = svc.graph.index.get(int(target)) if target.isdigit() else svc.graph.find(target)
        if idx is None:
            raise HTTPException(status_code=404, detail=f"unknown solar system: {target}")
        use_gates = hub.strip().lower() == "ansiblex"  # the Ansiblex filter (UC-16): its own radio button
        if use_gates:
            if svc.ansiblex is None:
                raise HTTPException(status_code=422, detail="the Ansiblex filter is disabled (ansiblex.enabled)")
            hub = ""
        scout = svc.evescout if (thera or hub) else None  # the Thera / Turnur filter (UC-15)
        hubs: list[str] | None = None  # None = any configured hub (the old thera=1)
        if scout is not None and hub:
            name = scout.hub_name(hub)
            if name is None:
                raise HTTPException(status_code=422, detail=f"unknown hub: {hub} (use {', '.join(scout.s.hubs)})")
            hubs = [name]  # one radio button = one mid-system
        if scout is not None:
            await scout.ensure_fresh()
        result = await svc.ranking.rank(
            idx,
            refresh=max(0, min(2, refresh)),
            owner_id=uid,
            evescout=scout,
            hubs=hubs,
            ansiblex=svc.ansiblex if use_gates else None,
        )
        return JSONResponse(result)

    @app.get("/api/evescout")
    async def evescout_list(request: Request, refresh: int = 0) -> JSONResponse:
        """The Thera / Turnur connections EVE-Scout lists (cached), with the state of the last read."""
        require_user(request)
        scout = svc_of(request).evescout
        if scout is None:
            raise HTTPException(status_code=404, detail="the EVE-Scout filter is disabled (evescout.enabled)")
        await scout.ensure_fresh(force=bool(refresh))
        return JSONResponse({"status": scout.status(), "connections": scout.listing()})

    @app.get("/api/characters")
    async def list_characters(request: Request) -> JSONResponse:
        return JSONResponse(svc_of(request).chars.list_view(owner(request)))

    @app.patch("/api/characters/{character_id}")
    async def patch_character(request: Request, character_id: int) -> JSONResponse:
        uid = owner(request)
        require_csrf(request)
        svc = svc_of(request)
        if svc.db.get_character(character_id, uid) is None:
            raise HTTPException(status_code=404, detail="unknown character")
        body = await json_body(request)
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise HTTPException(status_code=400, detail="enabled must be true or false")
            svc.db.set_enabled(character_id, body["enabled"], uid)
        if "notify" in body:
            if not isinstance(body["notify"], bool):
                raise HTTPException(status_code=400, detail="notify must be true or false")
            svc.db.set_notify(character_id, body["notify"], uid)
        if "tags" in body:
            tags = body["tags"] if isinstance(body["tags"], list) else str(body["tags"]).split(",")
            svc.db.set_tags(character_id, [str(t) for t in tags][:20], uid)
        return JSONResponse({"ok": True})

    @app.delete("/api/characters/{character_id}")
    async def delete_character(request: Request, character_id: int) -> JSONResponse:
        uid = owner(request)
        require_csrf(request)
        svc = svc_of(request)
        if svc.db.get_character(character_id, uid) is None:
            raise HTTPException(status_code=404, detail="unknown character")
        if uid and character_id == uid:
            raise HTTPException(status_code=400, detail="the main cannot be removed; delete the account instead")
        await svc.chars.remove(character_id, uid)
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ API: notifications (UC-13)
    async def notifications_of(request: Request, uid: int) -> NotificationService:
        ns = svc_of(request).notifications
        if ns is None:
            raise HTTPException(status_code=404, detail="notifications are disabled (notifications.enabled)")
        await require_member(request, uid)  # the alliance gate (D16)
        return ns

    @app.get("/api/notifications/wait")
    async def wait_for_notifications(request: Request, since: int = -1) -> JSONResponse:
        """Long poll: answers as soon as a notification newer than version ``since`` is stored (or after
        ``notifications.wait_s``), so the page shows it at once instead of on its next timer."""
        ns = await notifications_of(request, owner(request))
        version = await ns.wait_changed(since)
        return JSONResponse({"version": version, "changed": version != since})

    @app.get("/api/notifications")
    async def list_notifications(
        request: Request, hours: float = 168, types: str = "", limit: int = 100, refresh: int = 1
    ) -> JSONResponse:
        """Notifications of every registered character with a valid token (D17), newest first, each with the
        caller's closest characters."""
        uid = owner(request)
        svc = svc_of(request)
        ns = await notifications_of(request, uid)
        sources = ns.sources()  # character_id -> polling owner: all characters of the site, not only the caller's
        ids = list(sources)
        names = svc.db.character_names(ids)
        mains = {u["user_id"]: u["name"] for u in svc.db.list_users()}
        wanted = [t.strip() for t in types.split(",") if t.strip()] or ns.default_types()
        rows = ns.recent(ids, wanted, hours=max(1.0, min(hours, 24 * 90)), limit=max(1, min(limit, 500)))
        live = [c for c in svc.db.list_characters(uid) if c.enabled and c.status != REAUTH]
        if refresh and live:
            await svc.ranking.refresh_states(live, 1, uid)  # last known locations of the caller's characters
        static_origins = svc.roster.origins(uid) if svc.roster is not None else []
        if svc.evescout is not None:
            # Thera / Turnur labels and route types: a stale list is used as it is and re-read in the background
            await svc.evescout.ensure_fresh_soon()
        snaps = svc.db.get_snapshots([c.character_id for c in live])
        per_system: dict[int, tuple[Any, Any, Any]] = {}  # many notifications name the same system: work once

        def distances(idx: int) -> tuple[Any, Any, Any]:
            if idx not in per_system:
                per_system[idx] = (
                    closest_characters(svc, uid, idx, hubs=True, chars=live, snaps=snaps),
                    # the closer of Thera and Turnur (gates to the nearest listed system, plus the wormhole)
                    svc.evescout.nearest(idx, svc.ranking.avoid) if svc.evescout is not None else None,
                    RosterService.closest(static_origins, svc.ranking.parents(idx)) if static_origins else [],
                )
            return per_system[idx]

        items = []
        for r in rows:
            item = ns.describe(r, names)
            item["main"] = mains.get(sources.get(r["character_id"], -1))
            idx = item.pop("system_index")
            item["closest"], item["hub"], item["closest_static"] = (
                distances(idx) if idx is not None else ([], None, [])
            )
            items.append(item)
        checks = svc.db.notification_checks()
        return JSONResponse(
            {
                "items": items,
                "version": ns.version,
                "groups": ns.catalogue(svc.db.notification_types(ids)),
                "defaults": ns.default_types(),
                "poll_s": ns.s.poll_s,
                "characters": {
                    "linked": svc.db.registered_character_count(settings.demo),
                    "reading": len(ids) if settings.demo else len(ns.eligible()),
                    "min_structures": ns.s.min_structures,
                    "errors": [
                        {"name": names.get(cid, str(cid)), "error": checks[cid]["error"]}
                        for cid in ids
                        if cid in checks and checks[cid]["error"]
                    ],
                    "last_check": max((checks[cid]["checked_at"] for cid in ids if cid in checks), default=None),
                },
                "state": {**ns.state, "lag": ns.lag_brief()},
            }
        )

    # ------------------------------------------------------------------ API: static roster (UC-12)
    def roster_of(request: Request) -> RosterService:
        require_user(request)
        roster = svc_of(request).roster
        if roster is None:  # built with the services; only a half-built app has none
            raise HTTPException(status_code=503, detail="the roster is not ready")
        return roster

    def target_index(request: Request, target: str) -> int:
        svc = svc_of(request)
        idx = svc.graph.index.get(int(target)) if target.isdigit() else svc.graph.find(target)
        if idx is None:
            raise HTTPException(status_code=404, detail=f"unknown solar system: {target}")
        return idx

    @app.post("/api/roster/import")
    async def import_roster(request: Request, file: UploadFile = File(...)) -> JSONResponse:
        """ROS-01: merge an uploaded char_name + star_system_name file into this main's pool."""
        uid = owner(request)
        require_csrf(request)
        roster = roster_of(request)
        raw = await file.read()
        try:
            return JSONResponse(roster.import_file(raw, uid))
        except RosterRejected as exc:
            return JSONResponse({"rejected": exc.reason, "errors": exc.errors}, status_code=422)

    @app.post("/api/roster/resolve")
    async def resolve_roster(request: Request) -> JSONResponse:
        """ROS-11: try the unresolved entries again."""
        uid = owner(request)
        require_csrf(request)
        roster = roster_of(request)
        return JSONResponse({"resolved": roster.resolve_pending(uid), "stats": roster.stats(uid)})

    @app.get("/api/roster/export")
    async def export_roster(request: Request) -> Response:
        """ROS-14: the pool in the same schema. It carries no credentials, so no confirmation is needed."""
        uid = owner(request)
        try:
            doc = roster_of(request).export(uid)
        except RosterRejected as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from exc
        name = f"evedm-roster-{time.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            json.dumps(doc, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/roster")
    async def list_roster(request: Request, target: str = "", state: str = "", tag: str = "") -> JSONResponse:
        """With a target: the ranked roster (ROS-16). Without: the pool as it is, for the Roster page."""
        uid = owner(request)
        roster = roster_of(request)
        if target:
            return JSONResponse(roster.rank(target_index(request, target), uid, tag=tag[:24]))
        return JSONResponse({"rows": roster.list_view(uid, state=state, tag=tag[:24]), "stats": roster.stats(uid)})

    @app.patch("/api/roster/{key:path}")
    async def patch_roster(request: Request, key: str) -> JSONResponse:
        """ROS-12: enable/disable, tags, or set the system of one entry."""
        uid = owner(request)
        require_csrf(request)
        roster = roster_of(request)
        body = await json_body(request)
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled must be true or false")
        tags = None
        if "tags" in body:
            raw_tags = body["tags"] if isinstance(body["tags"], list) else str(body["tags"]).split(",")
            tags = [str(t) for t in raw_tags][:5]
        system_name = body.get("system_name")
        if system_name is not None and not isinstance(system_name, str):
            raise HTTPException(status_code=400, detail="system_name must be a solar system name")
        row = roster.patch(uid, key, enabled=enabled, tags=tags, system_name=system_name)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown roster entry")
        return JSONResponse(row)

    @app.delete("/api/roster")
    async def clear_roster(request: Request, confirm: int = 0) -> JSONResponse:
        """ROS-12: clear the whole pool (confirmed in the browser)."""
        uid = owner(request)
        require_csrf(request)
        roster_of(request)
        if not confirm:
            raise HTTPException(status_code=400, detail="confirm=1 required")
        return JSONResponse({"ok": True, "removed": svc_of(request).db.clear_roster(uid)})

    @app.delete("/api/roster/{key:path}")
    async def delete_roster(request: Request, key: str) -> JSONResponse:
        uid = owner(request)
        require_csrf(request)
        roster_of(request)
        if not svc_of(request).db.delete_roster(uid, key):
            raise HTTPException(status_code=404, detail="unknown roster entry")
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ API: wars
    def wars_of(request: Request) -> WarService:
        require_user(request)
        wars = svc_of(request).wars
        if wars is None:
            raise HTTPException(status_code=404, detail="the war watcher is disabled (config/wars.yaml)")
        return wars

    @app.get("/api/wars")
    async def list_wars(request: Request, limit: int = 50, offset: int = 0, type: str = "") -> JSONResponse:  # noqa: A002
        wars = wars_of(request)
        limit = max(1, min(limit, wars.s.page_limit))
        items = wars.store.summaries(limit, max(0, offset), type or None)
        return JSONResponse({"wars": items, "status": wars.status()})

    async def character_sources(svc: Services, uid: int, refresh: int) -> dict[int, list[str]]:
        """System index -> names of the main's characters there (for jumps from the nearest character)."""
        chars = [c for c in svc.db.list_characters(uid) if c.enabled and c.status != REAUTH]
        if refresh and chars:
            await svc.ranking.refresh_states(chars, 1, uid)  # stale-while-revalidate, as on the ranking page
        snaps = svc.db.get_snapshots([c.character_id for c in chars])
        sources: dict[int, list[str]] = {}
        for c in chars:
            snap = snaps.get(c.character_id)
            idx = svc.graph.index.get(snap["solar_system_id"]) if snap and snap["solar_system_id"] else None
            if idx is not None:
                sources.setdefault(idx, []).append(c.name)
        return sources

    # ------------------------------------------------------------------ structures (scout)
    async def scout_of(request: Request) -> StructureScout:
        uid = owner(request)
        scout = svc_of(request).scout
        if scout is None:
            raise HTTPException(status_code=404, detail="the structure scout is disabled (structures.enabled)")
        await require_member(request, uid)  # the alliance gate (D16)
        return scout

    @app.get("/api/structures")
    async def list_structures(
        request: Request,
        q: str = "",
        region: str = "",
        type: str = "",  # noqa: A002
        space: str = "",
        at_war: int = 0,
        source: str = "assets",
        sort: str = "jumps",
        limit: int = 100,
        offset: int = 0,
        refresh: int = 1,
    ) -> JSONResponse:
        """Player-owned structures shared by all members: those the characters' assets revealed (``source=assets``,
        the default), those a character may dock at without assets there (``dock``), or ``all``. Counts only."""
        uid = owner(request)
        scout = await scout_of(request)
        svc = svc_of(request)
        sources = await character_sources(svc, uid, refresh)
        wars = svc.wars.census.corps_at_war() if svc.wars is not None and svc.wars.census is not None else {}
        result = scout.listing(
            sources,
            svc.ranking.avoid,
            wars,
            q=q[:60],
            region=region,
            type_name=type,
            space=space,
            war_only=bool(at_war),
            source=source,
            sort=sort,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        result["wars_known"] = svc.wars is not None and svc.wars.census is not None
        return JSONResponse(result)

    @app.post("/api/structures/resolve")
    async def resolve_structures(request: Request) -> JSONResponse:
        """Start the Resolve unknown job (members only): 202 with its state, 409 busy when one is running."""
        uid = owner(request)
        require_csrf(request)
        scout = await scout_of(request)
        try:
            state = scout.start_resolve(uid)
        except ResolveError as exc:
            status = {"busy": 409, "demo": 409}.get(exc.code, 422)
            body: dict[str, Any] = scout.resolve_state() if exc.code == "busy" else {}
            body |= {"error": exc.code, "detail": exc.message}
            return JSONResponse(body, status_code=status)
        return JSONResponse(state, status_code=202)

    @app.get("/api/structures/resolve")
    async def resolve_state(request: Request) -> JSONResponse:
        """State of the Resolve unknown job (the running one, or the last one since the app started)."""
        scout = await scout_of(request)
        return JSONResponse(scout.resolve_state())

    @app.post("/api/structures/resolve/stop")
    async def resolve_stop(request: Request) -> JSONResponse:
        """Cancel the running Resolve unknown job; it ends at its next step."""
        owner(request)
        require_csrf(request)
        scout = await scout_of(request)
        return JSONResponse(scout.stop_resolve())

    @app.get("/api/structures/stats")
    async def structure_stats(request: Request) -> JSONResponse:
        """What the resolver has to work with (database only, no ESI call)."""
        scout = await scout_of(request)
        return JSONResponse(scout.stats())

    # ------------------------------------------------------------------ alerts + destinations
    def closest_characters(
        svc: Services,
        uid: int,
        target: int,
        n: int = 3,
        *,
        hubs: bool = False,
        chars: list[Any] | None = None,
        snaps: dict[int, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """The main's characters nearest to ``target`` (last known locations).

        By stargate jumps; with ``hubs`` a route through Thera or Turnur counts too when it is shorter, and each
        entry says which kind of route it is (``route``: gates, Thera or Turnur).
        """
        if chars is None:  # a caller that asks for many targets passes both in, read once
            chars = [c for c in svc.db.list_characters(uid) if c.enabled and c.status != REAUTH]
        if snaps is None:
            snaps = svc.db.get_snapshots([c.character_id for c in chars])
        parent = svc.ranking._parents(target)  # cached BFS from the target, as the ranking uses
        out = []
        for c in chars:
            snap = snaps.get(c.character_id)
            idx = svc.graph.index.get(snap["solar_system_id"]) if snap and snap["solar_system_id"] else None
            if idx is None:
                continue
            path = svc.graph.route(parent, idx)
            jumps = None if path is None else len(path) - 1
            route = "gates"
            plan = svc.evescout.via(idx, target, svc.ranking.avoid) if hubs and svc.evescout is not None else None
            if plan is not None and (jumps is None or plan["jumps"] < jumps):  # a tie keeps the gates
                jumps, route = plan["jumps"], plan["hub"]
            if jumps is None:
                continue
            out.append(
                {
                    "character_id": c.character_id,
                    "name": c.name,
                    "jumps": jumps,
                    "route": route,
                    "system": svc.graph.names[idx],
                    "online": None if snap["online"] is None else bool(snap["online"]),
                    "can_set_route": WAYPOINT_SCOPE in c.scopes or c.demo,
                }
            )
        out.sort(key=lambda r: (r["jumps"], 0 if r["online"] else 1, r["name"].lower()))
        return out[:n]

    @app.get("/api/alerts")
    async def list_alerts(
        request: Request,
        hours: float = 24,
        kinds: str = "",
        space: str = "",
        core_only: int = 0,
        capitals: int = 0,
        refresh: int = 1,
    ) -> JSONResponse:
        """Structure alerts grouped by structure, newest first, each with the caller's closest characters."""
        uid = owner(request)
        svc = svc_of(request)
        if svc.alerts is None:
            raise HTTPException(status_code=404, detail="structure alerts are disabled (alerts.enabled)")
        wanted = [k for k in kinds.split(",") if k in ("destroyed", "combat", "firing")]
        rows = svc.alerts.recent(max(0.1, min(hours, 24 * 7)), wanted or None, limit=500, capitals_only=bool(capitals))
        chars = [c for c in svc.db.list_characters(uid) if c.enabled and c.status != REAUTH]
        if refresh and chars:
            await svc.ranking.refresh_states(chars, 1, uid)
        static_origins = svc.roster.origins(uid) if svc.roster is not None else []  # ALR-11
        groups: dict[str, dict[str, Any]] = {}
        for r in rows:
            a = svc.alerts.describe(r)
            idx = svc.graph.index.get(r["solar_system_id"])
            if idx is None:
                continue
            a["system"]["space"] = "wormhole" if svc.graph.ids[idx] >= 31000000 else a["system"]["class"]
            if space and a["system"]["space"] != space:
                continue
            key = (
                f"s{r['structure_id']}"
                if r["structure_id"]
                else f"u{r['solar_system_id']}-{r['structure_type_id']}-{r['owner_id']}"
            )
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "key": key,
                    "structure": a["structure"],
                    "system": a["system"],
                    "owner": a["owner"],
                    "latest": a["time"] or "",
                    "latest_at": a["created_at"],
                    "counts": {"destroyed": 0, "combat": 0, "firing": 0},
                    "core_drop": False,
                    "capitals": False,
                    "alerts": [],
                    "closest": closest_characters(svc, uid, idx),
                    # ALR-11: static roster characters, jumps only, never in place of a live character
                    "closest_static": RosterService.closest(static_origins, svc.ranking.parents(idx))
                    if static_origins
                    else [],
                    "target_index": idx,
                }
            g["counts"][a["kind"]] += 1
            g["core_drop"] = g["core_drop"] or a["core_drop"]
            g["capitals"] = g["capitals"] or a["capitals"]
            if len(g["alerts"]) < 6:
                g["alerts"].append(a)
        out = [g for g in groups.values() if not core_only or g["core_drop"]]
        for g in out:
            g.pop("target_index", None)
            g["state"] = "destroyed" if g["counts"]["destroyed"] else ("combat" if g["counts"]["combat"] else "firing")
        out.sort(key=lambda g: -g["latest_at"])
        newest = max((r["alert_id"] for r in rows), default=0)
        return JSONResponse(
            {"groups": out, "newest_alert_id": newest, "feed": svc.alerts.state | {"stats": svc.alerts.feed.stats}}
        )

    @app.post("/api/characters/{character_id}/destination")
    async def set_destination(request: Request, character_id: int) -> JSONResponse:
        """Set an online character's autopilot destination in the game client (esi-ui.write_waypoint.v1)."""
        uid = owner(request)
        require_csrf(request)
        svc = svc_of(request)
        c = svc.db.get_character(character_id, uid)
        if c is None:
            raise HTTPException(status_code=404, detail="unknown character")
        body = await json_body(request)
        try:
            dest = int(body.get("destination_id"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="destination_id must be a solar system or structure ID"
            ) from None
        if svc.graph.index.get(dest) is None and dest < 1_000_000_000_000:
            raise HTTPException(status_code=400, detail="destination_id must be a solar system or structure ID")
        name = svc.graph.names[svc.graph.index[dest]] if dest in svc.graph.index else str(dest)
        if c.demo or settings.demo:
            return JSONResponse({"ok": True, "simulated": True, "destination": name})
        if WAYPOINT_SCOPE not in c.scopes:
            raise HTTPException(status_code=400, detail="re-authorize this character to allow setting its destination")
        try:
            token = await svc.chars.access_token(character_id, uid)
            await svc.esi.post_auth(  # type: ignore[union-attr]
                "/ui/autopilot/waypoint",
                token,
                {"destination_id": dest, "add_to_beginning": "false", "clear_other_waypoints": "true"},
            )
        except SsoError as exc:
            raise HTTPException(status_code=409, detail=f"token problem: {exc.code}") from None
        except EsiError as exc:
            raise HTTPException(
                status_code=502, detail=f"ESI refused ({exc.status}): is the character online?"
            ) from None
        log.info("destination of character %s set to %s", character_id, dest)
        return JSONResponse({"ok": True, "destination": name})

    def census_of(request: Request) -> Any:
        census = wars_of(request).census
        if census is None:
            raise HTTPException(status_code=404, detail="the war census is disabled (census_enabled in wars.yaml)")
        return census

    @app.get("/api/wars/stats")
    async def war_stats(request: Request) -> JSONResponse:
        """Statistics over every active war in New Eden (the census)."""
        census = census_of(request)
        await census.ensure_names(timeout=3)  # names, never bare IDs, even during the first scan
        return JSONResponse(census.stats())

    @app.get("/api/wars/structures")
    async def war_structures(
        request: Request,
        type: str = "",  # noqa: A002
        region: str = "",
        side: str = "",
        state: str = "",
        min_sec: float = 0.5,
        sort: str = "jumps",
        limit: int = 100,
        offset: int = 0,
        refresh: int = 1,
    ) -> JSONResponse:
        """High-sec structures of both sides of active wars, nearest to the caller's characters first."""
        uid = owner(request)
        census = census_of(request)
        await census.ensure_names(timeout=3)
        svc = svc_of(request)
        sources = await character_sources(svc, uid, refresh)
        result = census.loot(
            sources,
            svc.ranking.avoid,
            type_name=type,
            region=region,
            side=side,
            state=state,
            min_sec=min_sec,
            sort=sort,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        return JSONResponse(result)

    @app.get("/api/wars/{war_id}")
    async def get_war(request: Request, war_id: int) -> JSONResponse:
        item = wars_of(request).store.get_summary(war_id)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown war")
        return JSONResponse(item)

    # ------------------------------------------------------------------ API: account
    @app.get("/api/me")
    async def me(request: Request) -> JSONResponse:
        user = require_user(request)
        svc = svc_of(request)
        return JSONResponse(
            {
                "id": user["id"],
                "name": user["name"],
                "characters": len(svc.db.list_characters(int(user["id"]))),
                "session_days": settings.web.session_days,
            }
        )

    @app.delete("/api/me")
    async def delete_me(request: Request) -> JSONResponse:
        uid = owner(request)
        require_csrf(request)
        body = await json_body(request)
        if body.get("confirm") is not True:
            raise HTTPException(status_code=400, detail="confirm=true required")
        if uid == 0:
            raise HTTPException(status_code=400, detail="the local user has no account")
        removed = await svc_of(request).chars.remove_user(uid)
        request.session.clear()
        log.info("main %s deleted its account (%s characters)", uid, removed)
        return JSONResponse({"ok": True, "removed": removed})

    @app.post("/api/characters/import")
    async def import_characters(request: Request, file: UploadFile = File(...)) -> JSONResponse:
        uid = owner(request)
        require_csrf(request)
        raw = await file.read(1_000_001)
        try:
            results = await import_bundle(raw, svc_of(request).chars, uid)
        except BundleRejected as exc:
            return JSONResponse({"rejected": exc.reason, "errors": exc.errors}, status_code=422)
        return JSONResponse(report(results))

    @app.post("/api/characters/export")
    async def export_characters(request: Request) -> Response:
        uid = owner(request)
        require_csrf(request)
        body = await json_body(request)
        if body.get("confirm") is not True:  # EXP-02
            raise HTTPException(status_code=400, detail="export contains credentials: confirm=true required")
        try:
            ids = [int(i) for i in body.get("character_ids") or []]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="character_ids must be a list of IDs") from None
        try:
            doc = export_bundle(svc_of(request).chars, ids or None, uid)
        except BundleRejected as exc:
            raise HTTPException(status_code=400, detail=exc.reason) from exc
        name = f"evedm-characters-{time.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            json.dumps(doc, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/launches")
    async def launches(request: Request) -> JSONResponse:
        require_user(request)
        return JSONResponse(svc_of(request).db.launches())

    return app
