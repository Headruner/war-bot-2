"""Several mains on one site: each signs in once, links alts, and sees only its own characters."""

from __future__ import annotations

import httpx
import pytest
from helpers import evedm_env, evedm_server, fake_eve, free_port, sso_flow

pytestmark = pytest.mark.integration
H = {"X-EVEDM": "1"}
FIRST = 2_100_000_000


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
