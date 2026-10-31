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
from ..alerts import AlertService, seed_demo_alerts
from ..bundle import BundleRejected, export_bundle, import_bundle, report
from ..characters import REAUTH, CharacterService
from ..config import WAYPOINT_SCOPE, ConfigError, Settings
from ..crypto import TokenCipher
from ..db import Database
from ..demo import seed_demo
from ..esi import EsiClient, EsiError
from ..logs import LaunchRecorder, peak_rss_mb, rss_mb, setup_logging, trim_memory
from ..ranking import DemoProvider, LiveProvider, RankingService
from ..resolver import TargetResolver
from ..scout import StructureScout, seed_demo_structures
from ..selfcheck import check
from ..sso import AuthService, SsoError
from ..universe import GateGraph
from ..wars.config import WarsSettings, load_wars_settings
from ..wars.demo import DEMO_CENSUS, DEMO_WARS, seed_demo_census, seed_demo_wars
from ..wars.service import WarService

log = logging.getLogger("evedm.web")
SESSION_RENEW_S = 86400
HERE = Path(__file__).parent


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
    alerts: AlertService | None = None


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
    if settings.structures.enabled:
        svc.scout = StructureScout(settings, db, graph, chars, esi)  # type: ignore[arg-type]
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
        try:
            yield
        finally:
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

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        svc = svc_of(request)
        base = {
            "request": request,
            "version": __version__,
            "user": user_of(request),
            "demo": settings.demo,
            "auth": settings.web.auth,
            "auto_refresh_s": settings.web.auto_refresh_s,
            "sso_ready": svc.auth is not None,
            "universe": svc.graph.build,
            "session_days": settings.web.session_days,
            "wars_enabled": svc.wars is not None,
            "structures_enabled": svc.scout is not None,
            "alerts_enabled": svc.alerts is not None,
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

    @app.get("/alerts", response_class=HTMLResponse)
    async def alerts_page(request: Request) -> Response:
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        if svc_of(request).alerts is None:
            raise HTTPException(status_code=404, detail="structure alerts are disabled (alerts.enabled)")
        return page(request, "alerts.html")

    @app.get("/structures", response_class=HTMLResponse)
    async def structures_page(request: Request) -> Response:
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        if svc_of(request).scout is None:
            raise HTTPException(status_code=404, detail="the structure scout is disabled (structures.enabled)")
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
                "alerts": None
                if svc.alerts is None
                else {"running": svc.alerts.state["running"], "polls": svc.alerts.state["polls"]},
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
    async def rank(request: Request, target: str, refresh: int = 1) -> JSONResponse:
        uid = owner(request)
        svc = svc_of(request)
        idx = svc.graph.index.get(int(target)) if target.isdigit() else svc.graph.find(target)
        if idx is None:
            raise HTTPException(status_code=404, detail=f"unknown solar system: {target}")
        result = await svc.ranking.rank(idx, refresh=max(0, min(2, refresh)), owner_id=uid)
        return JSONResponse(result)

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
    def scout_of(request: Request) -> StructureScout:
        require_user(request)
        scout = svc_of(request).scout
        if scout is None:
            raise HTTPException(status_code=404, detail="the structure scout is disabled (structures.enabled)")
        return scout

    @app.get("/api/structures")
    async def list_structures(
        request: Request,
        q: str = "",
        region: str = "",
        type: str = "",  # noqa: A002
        space: str = "",
        at_war: int = 0,
        sort: str = "jumps",
        limit: int = 100,
        offset: int = 0,
        refresh: int = 1,
    ) -> JSONResponse:
        """Every player-owned structure the registered characters' assets revealed, shared by all mains."""
        uid = owner(request)
        scout = scout_of(request)
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
            sort=sort,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        result["wars_known"] = svc.wars is not None and svc.wars.census is not None
        return JSONResponse(result)

    # ------------------------------------------------------------------ alerts + destinations
    def closest_characters(svc: Services, uid: int, target: int, n: int = 3) -> list[dict[str, Any]]:
        """The main's characters nearest to ``target`` by stargate jumps (last known locations)."""
        chars = [c for c in svc.db.list_characters(uid) if c.enabled and c.status != REAUTH]
        snaps = svc.db.get_snapshots([c.character_id for c in chars])
        parent = svc.ranking._parents(target)  # cached BFS from the target, as the ranking uses
        out = []
        for c in chars:
            snap = snaps.get(c.character_id)
            idx = svc.graph.index.get(snap["solar_system_id"]) if snap and snap["solar_system_id"] else None
            if idx is None:
                continue
            path = svc.graph.route(parent, idx)
            if path is None:
                continue
            out.append(
                {
                    "character_id": c.character_id,
                    "name": c.name,
                    "jumps": len(path) - 1,
                    "system": svc.graph.names[idx],
                    "online": None if snap["online"] is None else bool(snap["online"]),
                    "can_set_route": WAYPOINT_SCOPE in c.scopes or c.demo,
                }
            )
        out.sort(key=lambda r: (r["jumps"], 0 if r["online"] else 1, r["name"].lower()))
        return out[:n]

    @app.get("/api/alerts")
    async def list_alerts(
        request: Request, hours: float = 24, kinds: str = "", space: str = "", core_only: int = 0, refresh: int = 1
    ) -> JSONResponse:
        """Structure alerts grouped by structure, newest first, each with the caller's closest characters."""
        uid = owner(request)
        svc = svc_of(request)
        if svc.alerts is None:
            raise HTTPException(status_code=404, detail="structure alerts are disabled (alerts.enabled)")
        wanted = [k for k in kinds.split(",") if k in ("destroyed", "combat", "firing")]
        rows = svc.alerts.recent(max(0.1, min(hours, 24 * 7)), wanted or None, limit=500)
        chars = [c for c in svc.db.list_characters(uid) if c.enabled and c.status != REAUTH]
        if refresh and chars:
            await svc.ranking.refresh_states(chars, 1, uid)
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
                    "alerts": [],
                    "closest": closest_characters(svc, uid, idx),
                    "target_index": idx,
                }
            g["counts"][a["kind"]] += 1
            g["core_drop"] = g["core_drop"] or a["core_drop"]
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
