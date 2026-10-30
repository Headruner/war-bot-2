"""Several mains on one site: each signs in once, links alts, and sees only its own characters."""

from __future__ import annotations

import json

import httpx
import pytest
from helpers import ROOT, evedm_env, evedm_server, fake_eve, free_port, sso_flow

pytestmark = pytest.mark.integration
H = {"X-EVEDM": "1"}
FIRST = 2_100_000_000
SCOPES = ["esi-location.read_location.v1", "esi-location.read_ship_type.v1", "esi-location.read_online.v1"]


def test_mains_are_isolated_and_stay_signed_in(tmp_path):
    port = free_port()
    with fake_eve(characters=10) as eve:
        env = evedm_env(tmp_path / "data", eve, port, EVEDM_REFRESH_INTERVAL_S="0", EVEDM_MAX_WAIT_AGE_S="0")
        with evedm_server(env, log_path=tmp_path / "server.out") as (base, _):
            alice, bob = httpx.Client(base_url=base), httpx.Client(base_url=base)

            # 1. signing in creates the account and links the main itself
            r = sso_flow(alice, "/sso/login", FIRST)
            assert r.headers["location"] == "/characters?welcome=1"
            assert [c["character_id"] for c in alice.get("/api/characters").json()] == [FIRST]
            assert alice.get("/api/characters").json()[0]["main"] is True
            r = sso_flow(bob, "/sso/login", FIRST + 1)
            assert r.headers["location"] == "/characters?welcome=1"

            # 2. alts are linked to the signed-in main; FIRST+5 is linked by both
            for cid in (FIRST + 2, FIRST + 5):
                assert sso_flow(alice, "/sso/add", cid).headers["location"].startswith("/characters?added=")
            assert sso_flow(bob, "/sso/add", FIRST + 5).headers["location"].startswith("/characters?added=")
            ids = lambda c: sorted(x["character_id"] for x in c.get("/api/characters").json())  # noqa: E731
            assert ids(alice) == [FIRST, FIRST + 2, FIRST + 5]
            assert ids(bob) == [FIRST + 1, FIRST + 5]

            # 3. rankings and recent targets are per main
            rows = alice.get("/api/rank", params={"target": "Jita", "refresh": 2}).json()["rows"]
            assert sorted(r["character_id"] for r in rows) == [FIRST, FIRST + 2, FIRST + 5]
            assert bob.get("/api/targets/recent").json() == []

            # 4. a main cannot touch another main's links, nor remove itself
            assert bob.delete(f"/api/characters/{FIRST + 2}", headers=H).status_code == 404
            assert bob.patch(f"/api/characters/{FIRST + 2}", json={"enabled": False}, headers=H).status_code == 404
            assert alice.delete(f"/api/characters/{FIRST}", headers=H).status_code == 400
            assert alice.delete(f"/api/characters/{FIRST + 5}", headers=H).json()["ok"]
            assert ids(bob) == [FIRST + 1, FIRST + 5]

            # 5. the session cookie alone keeps the main signed in (new client, same cookie)
            cookie = alice.cookies.get("evedm_session")
            again = httpx.Client(base_url=base, cookies={"evedm_session": cookie})
            assert again.get("/api/me").json()["name"]
            assert ids(again) == [FIRST, FIRST + 2]

            # 6. signing in again keeps the linked characters (returning main)
            assert sso_flow(alice, "/sso/login", FIRST).headers["location"] == "/"
            assert ids(alice) == [FIRST, FIRST + 2]

            # 7. deleting the account removes the links and ends every session of that main
            assert alice.request("DELETE", "/api/me", json={"confirm": True}, headers=H).json()["removed"] == 2
            assert again.get("/api/characters").status_code == 401
            assert ids(bob) == [FIRST + 1, FIRST + 5]


def test_upgrade_from_a_single_user_database(tmp_path):
    """A 0.3.0 database (schema v1) is migrated on start; its owner signs in and keeps every character."""
    import sqlite3
    import time

    from evedm.crypto import TokenCipher

    port = free_port()
    data = tmp_path / "data"
    data.mkdir()
    schema = (ROOT / "tests" / "unit" / "fixtures" / "schema-v1.sql").read_text(encoding="utf-8")
    with fake_eve(characters=10) as eve:
        cipher = TokenCipher("integration-secret")
        con = sqlite3.connect(data / "evedm.sqlite3")
        con.executescript(schema)
        con.execute("PRAGMA user_version=1")
        for cid in (FIRST + 3, FIRST + 4):
            tok = httpx.post(f"{eve}/_admin/mint", json={"character_id": cid}).json()
            con.execute(
                "INSERT INTO characters(character_id,name,status,scopes,added_at) VALUES(?,?,?,?,?)",
                (cid, f"legacy {cid}", "OK", json.dumps(SCOPES), time.time()),
            )
            con.execute(
                "INSERT INTO tokens(character_id,client_id,access_token,access_expires_at,refresh_token,"
                "refresh_tail,updated_at) VALUES(?,?,?,?,?,?,?)",
                (cid, "test-client-id", None, 0, cipher.encrypt(tok["refresh_token"]), "tail", time.time()),
            )
        con.commit()
        con.close()

        env = evedm_env(data, eve, port, EVEDM_REFRESH_INTERVAL_S="0", EVEDM_MAX_WAIT_AGE_S="0")
        with evedm_server(env, log_path=tmp_path / "server.out") as (base, _):
            stranger, owner = httpx.Client(base_url=base), httpx.Client(base_url=base)
            sso_flow(stranger, "/sso/login", FIRST + 7)
            assert [c["character_id"] for c in stranger.get("/api/characters").json()] == [FIRST + 7]

            sso_flow(owner, "/sso/login", FIRST + 3)  # the legacy owner's main
            got = sorted(c["character_id"] for c in owner.get("/api/characters").json())
            assert got == [FIRST + 3, FIRST + 4]
            rows = owner.get("/api/rank", params={"target": "Jita", "refresh": 2}).json()["rows"]
            assert {r["character_id"] for r in rows} == {FIRST + 3, FIRST + 4}
            assert all(r["error"] is None for r in rows)  # migrated tokens still refresh
        log = (data / "logs" / "evedm.log").read_text(encoding="utf-8")
        assert "adopted 2 characters stored before accounts existed" in log
