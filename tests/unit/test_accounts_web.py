"""Accounts through the web API with signed session cookies (no SSO round trip needed): every request is scoped to
the signed-in main, foreign links are invisible, broken input gets 4xx, deleted or de-listed mains lose access."""

from __future__ import annotations

import json
import time
from base64 import b64decode, b64encode

import itsdangerous
import pytest
from fastapi.testclient import TestClient

from evedm.config import Settings
from evedm.wars.config import WarsSettings
from evedm.web.app import create_app

H = {"X-EVEDM": "1"}
SECRET = "accounts-web-secret"
A, B = 2112000001, 2112000002  # mains
SHARED, ALT = 2112000003, 2112000004
JITA, AMARR, TAMA = 30000142, 30002187, 30002813
SCOPES = ["esi-location.read_location.v1", "esi-location.read_ship_type.v1", "esi-location.read_online.v1"]


def session_cookie(data: dict, secret: str = SECRET) -> str:
    """The cookie Starlette's SessionMiddleware would set for ``data``."""
    return itsdangerous.TimestampSigner(secret).sign(b64encode(json.dumps(data).encode())).decode()


def settings(tmp_path, **web) -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.secret_key = SECRET
    s.eve.client_id, s.eve.client_secret = "cid", "csecret"
    s.eve.sso_base = s.eve.esi_base = "http://127.0.0.1:9"  # never reached: no SSO round trips here
    for k, v in web.items():
        setattr(s.web, k, v)
    return s


def link(svc, owner: int, cid: int, name: str, system: int) -> None:
    db, cipher = svc.db, svc.chars.cipher
    db.upsert_character(cid, name, "OK", SCOPES, owner_id=owner)
    db.save_tokens(
        cid, "cid", cipher.encrypt("a"), time.time() + 900, cipher.encrypt(f"r-{owner}-{cid}"), "tail", owner_id=owner
    )
    db.save_snapshot(cid, solar_system_id=system, ship_type_id=587, online=0, fetched_at=time.time(), error=None)


def as_user(c: TestClient, uid: int | None, name: str = "main") -> TestClient:
    c.cookies.clear()
    if uid is not None:
        c.cookies.set("evedm_session", session_cookie({"user": {"id": uid, "name": name}}))
    return c


@pytest.fixture
def app_client(tmp_path):
    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=False))) as c:
        svc = c.app.state.svc
        svc.auth.revoke = no_revoke  # TOK-05 is covered elsewhere; no network here
        for main, name in ((A, "Alice"), (B, "Bob")):
            svc.db.upsert_user(main, name)
            link(svc, main, main, name, JITA)
            link(svc, main, SHARED, "Shared Alt", AMARR)
        link(svc, A, ALT, "Alice Alt", TAMA)
        yield c


async def no_revoke(refresh_token: str) -> None:
    return None


def ids(c: TestClient) -> list[int]:
    r = c.get("/api/characters")
    assert r.status_code == 200, r.text
    return sorted(x["character_id"] for x in r.json())


def test_anonymous_requests_are_sent_to_sign_in(app_client):
    c = as_user(app_client, None)
    for path in ("/", "/characters"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in ("/api/characters", "/api/me", "/api/targets/recent", "/api/rank?target=Jita"):
        assert c.get(path).status_code == 401, path
    assert c.get("/login").status_code == 200


def test_each_main_sees_and_ranks_only_its_own_links(app_client):
    c = as_user(app_client, A, "Alice")
    assert ids(c) == [A, SHARED, ALT]
    view = {x["character_id"]: x for x in c.get("/api/characters").json()}
    assert view[A]["main"] is True and view[SHARED]["main"] is False
    rows = c.get("/api/rank", params={"target": "Jita", "refresh": 0}).json()["rows"]
    assert {r["character_id"] for r in rows} == {A, SHARED, ALT} and rows[0]["character_id"] == A
    assert [t["name"] for t in c.get("/api/targets/recent").json()] == ["Jita"]

    c = as_user(app_client, B, "Bob")
    assert ids(c) == [B, SHARED]
    rows = c.get("/api/rank", params={"target": "Tama", "refresh": 0}).json()["rows"]
    assert {r["character_id"] for r in rows} == {B, SHARED}
    assert [t["name"] for t in c.get("/api/targets/recent").json()] == ["Tama"]  # Alice's Jita is not listed


def test_foreign_links_cannot_be_changed_and_the_main_stays(app_client):
    c = as_user(app_client, B, "Bob")
    assert c.patch(f"/api/characters/{ALT}", json={"enabled": False}, headers=H).status_code == 404
    assert c.delete(f"/api/characters/{ALT}", headers=H).status_code == 404
    assert c.delete(f"/api/characters/{A}", headers=H).status_code == 404
    assert c.delete(f"/api/characters/{B}", headers=H).status_code == 400  # own main
    # Bob's settings on the shared character do not touch Alice's link
    assert c.patch(f"/api/characters/{SHARED}", json={"enabled": False, "tags": ["cyno"]}, headers=H).json()["ok"]
    assert c.delete(f"/api/characters/{SHARED}", headers=H).json()["ok"]
    assert ids(c) == [B]
    c = as_user(app_client, A, "Alice")
    shared = {x["character_id"]: x for x in c.get("/api/characters").json()}[SHARED]
    assert shared["enabled"] is True and shared["tags"] == []
    assert SHARED in app_client.app.state.svc.db.get_snapshots()  # still linked by Alice: snapshot kept


def test_bad_input_is_a_4xx_never_a_500(app_client):
    c = as_user(app_client, A, "Alice")
    bad = [
        c.patch(f"/api/characters/{ALT}", content=b"{not json", headers=H | {"Content-Type": "application/json"}),
        c.patch(f"/api/characters/{ALT}", json=["enabled"], headers=H),
        c.patch(f"/api/characters/{ALT}", json={"enabled": "false"}, headers=H),
        c.post("/api/target/resolve", content=b"\xff\xfe", headers=H | {"Content-Type": "application/json"}),
        c.post("/api/characters/export", json={"confirm": True, "character_ids": ["x"]}, headers=H),
        c.post("/api/characters/export", json={"confirm": "yes"}, headers=H),
        c.request("DELETE", "/api/me", content=b"", headers=H),
        c.request("DELETE", "/api/me", json={"confirm": "true"}, headers=H),
        c.get("/api/rank", params={"target": ""}),
        c.get("/api/rank", params={"target": "999"}),
    ]
    assert [r.status_code for r in bad] == [400, 400, 400, 400, 400, 400, 400, 400, 404, 404]
    assert c.patch(f"/api/characters/{ALT}", json={"tags": [str(i) for i in range(50)]}, headers=H).json()["ok"]
    alt = {x["character_id"]: x for x in c.get("/api/characters").json()}[ALT]
    assert len(alt["tags"]) == 20  # capped


def test_state_changes_need_the_csrf_header(app_client):
    c = as_user(app_client, A, "Alice")
    assert c.patch(f"/api/characters/{ALT}", json={"enabled": False}).status_code == 403
    assert c.delete(f"/api/characters/{ALT}").status_code == 403
    assert c.request("DELETE", "/api/me", json={"confirm": True}).status_code == 403
    assert c.post("/api/characters/export", json={"confirm": True}).status_code == 403
    assert ids(c) == [A, SHARED, ALT]


def test_export_contains_only_the_callers_links(app_client):
    c = as_user(app_client, B, "Bob")
    r = c.post("/api/characters/export", json={"confirm": True}, headers=H)
    assert r.status_code == 200
    doc = r.json()
    assert sorted(x["character_id"] for x in doc["characters"]) == [B, SHARED]
    assert {x["refresh_token"] for x in doc["characters"]} == {f"r-{B}-{B}", f"r-{B}-{SHARED}"}
    # asking for someone else's character exports nothing of it
    r = c.post("/api/characters/export", json={"confirm": True, "character_ids": [ALT]}, headers=H)
    assert r.status_code == 400


def test_account_deletion_ends_every_session_of_that_main(app_client):
    c = as_user(app_client, A, "Alice")
    me = c.get("/api/me").json()
    assert me == {"id": A, "name": "Alice", "characters": 3, "session_days": 90}
    page = c.get("/characters").text
    assert "Delete account" in page and "Signed in as <b>Alice</b>" in page
    assert c.request("DELETE", "/api/me", json={"confirm": True}, headers=H).json() == {"ok": True, "removed": 3}
    stale = as_user(app_client, A, "Alice")  # another browser with the same main
    assert stale.get("/api/characters").status_code == 401
    assert stale.get("/", follow_redirects=False).headers["location"] == "/login"
    db = app_client.app.state.svc.db
    assert db.get_user(A) is None and db.list_characters(A) == []
    assert ids(as_user(app_client, B, "Bob")) == [B, SHARED]
    assert SHARED in db.get_snapshots() and ALT not in db.get_snapshots()


def test_forged_or_malformed_sessions_are_anonymous(app_client):
    c = as_user(app_client, None)
    c.cookies.set("evedm_session", session_cookie({"user": {"id": A, "name": "x"}}, secret="another-key"))
    assert c.get("/api/characters").status_code == 401
    for data in ({"user": "Alice"}, {"user": {"name": "no id"}}, {"other": 1}):
        c.cookies.clear()
        c.cookies.set("evedm_session", session_cookie(data))
        assert c.get("/api/characters").status_code == 401, data
    c.cookies.set("evedm_session", session_cookie({"user": {"id": 999, "name": "never signed in"}}))
    assert c.get("/api/me").status_code == 401


def test_session_is_renewed_daily_long_lived_httponly_and_lax(app_client):
    c = as_user(app_client, None)
    c.cookies.set("evedm_session", session_cookie({"user": {"id": A, "name": "Alice"}, "seen": 1}))
    r = c.get("/api/me")
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]  # re-signed: the 90 days now count from this visit
    assert "evedm_session=" in cookie and f"Max-Age={90 * 86400}" in cookie
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    assert "set-cookie" not in c.get("/api/me").headers  # renewed at most once a day
    data = json.loads(b64decode(r.cookies["evedm_session"].split(".")[0]))
    assert data["user"]["id"] == A and time.time() - data["seen"] < 60


def test_expired_session_signature_needs_a_new_sign_in(app_client, monkeypatch):
    signed_long_ago = itsdangerous.TimestampSigner(SECRET)
    monkeypatch.setattr(signed_long_ago, "get_timestamp", lambda: int(time.time()) - 91 * 86400)
    old = signed_long_ago.sign(b64encode(json.dumps({"user": {"id": A, "name": "Alice"}}).encode())).decode()
    c = as_user(app_client, None)
    c.cookies.set("evedm_session", old)
    assert c.get("/api/me").status_code == 401


def test_main_removed_from_the_allow_list_loses_its_session(tmp_path):
    with TestClient(create_app(settings(tmp_path, allowed_login_characters=[A]), WarsSettings(enabled=False))) as c:
        svc = c.app.state.svc
        for main in (A, B):
            svc.db.upsert_user(main, str(main))
            link(svc, main, main, str(main), JITA)
        assert ids(as_user(c, A)) == [A]
        assert as_user(c, B).get("/api/characters").status_code == 401


def test_health_is_public_but_reveals_no_errors(tmp_path, monkeypatch):
    from evedm.wars.service import WarService

    monkeypatch.setattr(WarService, "start", lambda self: self.state.update(running=True))  # no ESI polling here
    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=True, structures_enabled=False))) as c:
        svc = c.app.state.svc
        svc.wars.state["last_error"] = "ESI: something internal"
        body = c.get("/api/health").json()
        assert body["wars"] == {"running": True, "cycles": body["wars"]["cycles"]}
        assert "internal" not in json.dumps(body)


# --------------------------------------------------------------------------- SSO callback (code exchange stubbed)
def sso(c: TestClient, purpose: str, cid: int, name: str, scopes=SCOPES):
    """Complete an SSO callback for ``cid`` as if EVE had returned it (the code exchange is stubbed)."""
    from evedm.sso import Identity, TokenSet

    svc = c.app.state.svc
    ident = Identity(cid, name, list(scopes), f"hash-{cid}", time.time() + 900)

    async def exchange(code: str) -> TokenSet:
        return TokenSet(f"access-{cid}", f"refresh-{cid}-{time.time()}", time.time() + 900, ident)

    svc.auth.exchange_code = exchange
    state = f"state-{purpose}-{cid}-{time.time()}"
    svc.db.create_state(state, purpose)
    return c.get("/sso/callback", params={"code": "c", "state": state}, follow_redirects=False)


def test_sign_in_creates_the_account_and_links_the_main(tmp_path):
    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=False))) as c:
        r = sso(c, "login", A, "Alice")
        assert r.headers["location"] == "/characters?welcome=1"
        assert ids(c) == [A] and c.app.state.svc.db.get_user(A)["name"] == "Alice"
        assert "Welcome, Alice" in c.get("/characters?welcome=1").text
        r = sso(c, "add", ALT, "Alice Alt", scopes=SCOPES[:1])  # declined two scopes
        assert r.headers["location"] == "/characters?added=Alice+Alt"
        alt = {x["character_id"]: x for x in c.get("/api/characters").json()}[ALT]
        assert alt["status"] == "LIMITED" and len(alt["missing_scopes"]) == 2
        # signing in again: returning main, links kept, renamed main updated
        assert sso(c, "login", A, "Alice Renamed").headers["location"] == "/"
        assert ids(c) == [A, ALT] and c.app.state.svc.db.get_user(A)["name"] == "Alice Renamed"
        # signing in as another main switches the session to that account
        assert sso(c, "login", B, "Bob").headers["location"] == "/characters?welcome=1"
        assert ids(c) == [B]


def test_add_needs_a_signed_in_main_and_a_fresh_state(tmp_path):
    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=False))) as c:
        assert sso(c, "add", ALT, "Alt").headers["location"] == "/login"
        assert c.app.state.svc.db.list_characters(0) == [] and c.app.state.svc.db.owners_of(ALT) == []
        sso(c, "login", A, "Alice")
        svc = c.app.state.svc
        svc.db.create_state("used", "add")
        assert svc.db.consume_state("used") == "add"
        r = c.get("/sso/callback", params={"code": "c", "state": "used"}, follow_redirects=False)
        assert "invalid_state" in r.headers["location"]
        assert (
            c.get("/sso/callback", params={"state": "x"}, follow_redirects=False)
            .headers["location"]
            .endswith("invalid_state")
        )
        r = c.get("/sso/add", follow_redirects=False)
        assert r.status_code == 303 and "esi-location.read_ship_type.v1" in r.headers["location"]
        r = c.get("/sso/login", follow_redirects=False)  # sign-in asks for the same scopes: the main is linked
        assert "esi-location.read_online.v1" in r.headers["location"]


def test_allow_list_refuses_without_creating_anything(tmp_path):
    with TestClient(create_app(settings(tmp_path, allowed_login_characters=[A]), WarsSettings(enabled=False))) as c:
        assert sso(c, "login", B, "Bob").headers["location"] == "/login?error=not_allowed"
        svc = c.app.state.svc
        assert svc.db.get_user(B) is None and svc.db.owners_of(B) == []
        assert c.get("/api/characters").status_code == 401
        assert sso(c, "login", A, "Alice").headers["location"] == "/characters?welcome=1"


def test_upgrade_from_single_user_version_adopts_legacy_characters(tmp_path):
    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=False))) as c:
        svc = c.app.state.svc
        for cid in (A, ALT):  # stored by 0.3.0, before accounts existed: owner 0
            link(svc, 0, cid, str(cid), JITA)
        svc.db.remember_target(TAMA)
        # someone whose main is not among them gets nothing
        sso(c, "login", B, "Bob")
        assert ids(c) == [B] and len(svc.db.list_characters(0)) == 2
        # the legacy main signs in: it takes over every legacy character and the recent targets
        sso(c, "login", A, "Alice")
        assert ids(c) == [A, ALT] and svc.db.list_characters(0) == []
        assert [t["name"] for t in c.get("/api/targets/recent").json()] == ["Tama"]
        token = svc.chars.cipher.decrypt(svc.db.get_tokens(A, A)["refresh_token"])
        assert token.startswith(f"refresh-{A}-")  # the fresh sign-in token, not the legacy one
