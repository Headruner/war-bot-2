"""End-to-end: real evedm process + real fake EVE SSO/ESI process over HTTP."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from helpers import evedm_env, evedm_server, fake_eve, free_port, sso_flow

from evedm.bundle import validate

pytestmark = pytest.mark.integration
H = {"X-EVEDM": "1"}
FIRST = 2_100_000_000  # first fake character id


@pytest.fixture(scope="module")
def eve():
    with fake_eve(characters=20) as base:
        yield base


def test_full_user_journey(eve, tmp_path):
    port = free_port()
    data = tmp_path / "data"
    env = evedm_env(
        data,
        eve,
        port,
        EVEDM_ALLOWED_LOGIN_CHARACTERS=str(FIRST),
        EVEDM_REFRESH_INTERVAL_S="0",
        EVEDM_MAX_WAIT_AGE_S="0",
    )
    with evedm_server(env, log_path=tmp_path / "server.out") as (base, _):
        c = httpx.Client(base_url=base)
        # 1. anonymous users are sent to sign-in
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"

        # 2. a character that is not on the allow list is refused
        r = sso_flow(c, "/sso/login", FIRST + 1)
        assert "not_allowed" in r.headers["location"]

        # 3. sign in with the allowed character: a new main lands on Characters, linked to itself
        r = sso_flow(c, "/sso/login", FIRST)
        assert r.status_code == 303 and r.headers["location"] == "/characters?welcome=1"
        assert c.get("/").status_code == 200

        # 4. add two characters through SSO (SSO-01..SSO-03)
        for cid in (FIRST, FIRST + 1):
            r = sso_flow(c, "/sso/add", cid)
            assert r.headers["location"].startswith("/characters?added=")
        assert len(c.get("/api/characters").json()) == 2

        # 5. a replayed callback (used state) is rejected (SSO-04)
        assert "invalid_state" in c.get("/sso/callback?code=x&state=x", follow_redirects=False).headers["location"]

        # 6. import 10 more characters from a bundle (IMP-*)
        entries = []
        for cid in range(FIRST + 2, FIRST + 12):
            tok = httpx.post(f"{eve}/_admin/mint", json={"character_id": cid}).json()
            entries.append(
                {"refresh_token": tok["refresh_token"], "access_token": tok["access_token"], "tags": ["imported"]}
            )
        bundle = {"schema_version": 1, "client_id": "test-client-id", "characters": entries}
        r = c.post(
            "/api/characters/import",
            headers=H,
            files={"file": ("chars.json", json.dumps(bundle).encode(), "application/json")},
        )
        assert r.json()["summary"] == {"Added": 10, "Updated": 0, "Limited": 0, "Rejected": 0}

        # 7. rank: every character has a row, sorted by jumps, routes end at the target
        ranking = c.get("/api/rank", params={"target": "Jita"}).json()
        rows = ranking["rows"]
        assert len(rows) == 12
        jumps = [r["jumps"] for r in rows if r["reachable"]]
        assert jumps == sorted(jumps)
        assert all(r["route"][-1] == 30000142 for r in rows if r["reachable"])
        assert sum(r["recommended"] for r in rows) == 1
        calls_after_first = httpx.get(f"{eve}/_admin/stats").json()["calls"]["location"]

        # 8. a second ranking inside the ESI cache window makes no new location calls (RNK-10)
        c.get("/api/rank", params={"target": "Amarr"})
        assert httpx.get(f"{eve}/_admin/stats").json()["calls"]["location"] == calls_after_first

        # 9. export (with confirmation) produces a schema-valid bundle
        assert c.post("/api/characters/export", json={}, headers=H).status_code == 400
        exported = c.post("/api/characters/export", json={"confirm": True}, headers=H).json()
        assert validate(exported) == [] and len(exported["characters"]) == 12

        # 10. a character revoked on the EVE side drops out with "re-auth required" (TOK-04)
        httpx.post(f"{eve}/_admin/revoke/{FIRST + 5}")
        db = sqlite3.connect(data / "evedm.sqlite3")
        db.execute("UPDATE tokens SET access_expires_at=0 WHERE character_id=?", (FIRST + 5,))
        db.commit()
        db.close()
        ranking = c.get("/api/rank", params={"target": "Jita"}).json()
        assert FIRST + 5 not in [r["character_id"] for r in ranking["rows"]]
        assert {"character_id": FIRST + 5, "name": "Pilot 005", "reason": "re-auth required"} in ranking["excluded"]

        # 11. sign out
        c.post("/logout")
        assert c.get("/api/characters").status_code == 401
        refresh_tokens = [e["refresh_token"] for e in entries]

    # 12. the exported bundle imports into a second, independent instance (D5)
    port2 = free_port()
    with evedm_server(evedm_env(tmp_path / "data2", eve, port2, EVEDM_WEB_AUTH="none")) as (base2, _):
        r = httpx.post(
            f"{base2}/api/characters/import",
            headers=H,
            files={"file": ("e.json", json.dumps(exported).encode(), "application/json")},
        )
        summary = r.json()["summary"]
        assert summary["Added"] == 11 and summary["Rejected"] == 1  # the revoked one

    # 13. secrets never reach the log files (TOK-05, IMP-09)
    logs = "".join(p.read_text(errors="ignore") for p in (data / "logs").glob("*"))
    logs += (tmp_path / "server.out").read_text(errors="ignore")
    assert not any(rt in logs for rt in refresh_tokens)
    assert "eyJ" not in logs


def test_restart_keeps_data_and_records_launches(eve, tmp_path):
    """The DB on the data volume survives restarts; every launch is recorded (DB + launches.jsonl)."""
    data = tmp_path / "data"
    for _ in range(2):
        port = free_port()
        with evedm_server(evedm_env(data, eve, port, EVEDM_WEB_AUTH="none")) as (base, _):
            tok = httpx.post(f"{eve}/_admin/mint", json={"character_id": FIRST + 15}).json()
            bundle = {
                "schema_version": 1,
                "client_id": "test-client-id",
                "characters": [{"refresh_token": tok["refresh_token"]}],
            }
            httpx.post(
                f"{base}/api/characters/import",
                headers=H,
                files={"file": ("b.json", json.dumps(bundle).encode(), "application/json")},
            )
            assert len(httpx.get(f"{base}/api/characters").json()) == 1
    db = sqlite3.connect(data / "evedm.sqlite3")
    launches = db.execute("SELECT mode, stopped_at, requests FROM launches ORDER BY id").fetchall()
    db.close()
    assert len(launches) == 2 and all(row[1] for row in launches)
    events = [json.loads(x)["event"] for x in (data / "logs" / "launches.jsonl").read_text().splitlines()]
    assert events == ["start", "stop", "start", "stop"]


def test_demo_mode_needs_no_eve_application(tmp_path):
    port = free_port()
    env = evedm_env(
        tmp_path / "d",
        "http://127.0.0.1:9",
        port,
        EVEDM_DEMO="1",
        EVE_CLIENT_ID="",
        EVE_CLIENT_SECRET="",
        EVEDM_WEB_AUTH="none",
    )
    with evedm_server(env) as (base, _):
        result = httpx.get(f"{base}/api/rank", params={"target": "Tama"}).json()
        assert result["rows"][0]["name"] == "Kael Varn"
        assert httpx.get(f"{base}/api/health").json()["mode"] == "demo"
