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

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import __version__
from ..bundle import BundleRejected, export_bundle, import_bundle, report
from ..characters import CharacterService
from ..config import ConfigError, Settings
from ..crypto import TokenCipher
from ..db import Database
from ..demo import seed_demo
from ..esi import EsiClient
from ..logs import LaunchRecorder, peak_rss_mb, rss_mb, setup_logging, trim_memory
from ..ranking import DemoProvider, LiveProvider, RankingService
from ..resolver import TargetResolver
from ..selfcheck import check
from ..sso import AuthService, SsoError
from ..universe import GateGraph

log = logging.getLogger("evedm.web")
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
    return Services(settings, db, graph, TargetResolver(graph), chars, ranking, auth, esi, http, launch, time.time())


def create_app(settings: Settings) -> FastAPI:
    settings.validate_for_serve()
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
        svc.launch.start(
            {"universe": svc.graph.build, "systems": len(svc.graph.ids), "journal_mode": svc.db.journal_mode}
        )
        try:
            yield
        finally:
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
        if settings.web.auth == "none":
            return {"id": 0, "name": "local"}
        return request.session.get("user")

    def require_user(request: Request) -> dict[str, Any]:
        user = user_of(request)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in required")
        return user

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
            request, "characters.html", added=request.query_params.get("added"), error=request.query_params.get("error")
        )

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
        return _start_sso(request, "login", [])

    @app.get("/sso/add")
    async def sso_add(request: Request) -> Response:  # SSO-01
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        return _start_sso(request, "add", settings.eve.required_scopes)

    @app.get("/sso/callback")
    async def sso_callback(request: Request, code: str = "", state: str = "") -> Response:
        svc = svc_of(request)
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
            if not allowed:
                log.warning("web.allowed_login_characters is empty: any EVE character can sign in")
            request.session["user"] = {"id": ident.character_id, "name": ident.name}
            return RedirectResponse("/", status_code=303)
        if user_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        svc.chars.store(ts)  # SSO-03 / SSO-05 / SSO-06
        return RedirectResponse(f"/characters?added={ident.name}", status_code=303)

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
        body = await request.json()
        text = str(body.get("text", ""))[:20000]
        return JSONResponse({"candidates": svc_of(request).resolver.extract(text)})

    @app.get("/api/targets/recent")
    async def recent_targets(request: Request) -> JSONResponse:
        require_user(request)
        svc = svc_of(request)
        out = [
            svc.graph.describe(i) for i in (svc.graph.index.get(s) for s in svc.db.recent_targets()) if i is not None
        ]
        return JSONResponse(out)

    @app.get("/api/rank")
    async def rank(request: Request, target: str, refresh: int = 1) -> JSONResponse:
        require_user(request)
        svc = svc_of(request)
        idx = svc.graph.index.get(int(target)) if target.isdigit() else svc.graph.find(target)
        if idx is None:
            raise HTTPException(status_code=404, detail=f"unknown solar system: {target}")
        result = await svc.ranking.rank(idx, refresh=max(0, min(2, refresh)))
        return JSONResponse(result)

    @app.get("/api/characters")
    async def list_characters(request: Request) -> JSONResponse:
        require_user(request)
        return JSONResponse(svc_of(request).chars.list_view())

    @app.patch("/api/characters/{character_id}")
    async def patch_character(request: Request, character_id: int) -> JSONResponse:
        require_user(request)
        require_csrf(request)
        svc = svc_of(request)
        if svc.db.get_character(character_id) is None:
            raise HTTPException(status_code=404, detail="unknown character")
        body = await request.json()
        if "enabled" in body:
            svc.db.set_enabled(character_id, bool(body["enabled"]))
        if "tags" in body:
            tags = body["tags"] if isinstance(body["tags"], list) else str(body["tags"]).split(",")
            svc.db.set_tags(character_id, [str(t) for t in tags])
        return JSONResponse({"ok": True})

    @app.delete("/api/characters/{character_id}")
    async def delete_character(request: Request, character_id: int) -> JSONResponse:
        require_user(request)
        require_csrf(request)
        await svc_of(request).chars.remove(character_id)
        return JSONResponse({"ok": True})

    @app.post("/api/characters/import")
    async def import_characters(request: Request, file: UploadFile = File(...)) -> JSONResponse:
        require_user(request)
        require_csrf(request)
        raw = await file.read(1_000_001)
        try:
            results = await import_bundle(raw, svc_of(request).chars)
        except BundleRejected as exc:
            return JSONResponse({"rejected": exc.reason, "errors": exc.errors}, status_code=422)
        return JSONResponse(report(results))

    @app.post("/api/characters/export")
    async def export_characters(request: Request) -> Response:
        require_user(request)
        require_csrf(request)
        body = await request.json()
        if body.get("confirm") is not True:  # EXP-02
            raise HTTPException(status_code=400, detail="export contains credentials: confirm=true required")
        ids = [int(i) for i in body.get("character_ids") or []]
        try:
            doc = export_bundle(svc_of(request).chars, ids or None)
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
