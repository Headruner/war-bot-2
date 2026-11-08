"""The alliance gate for the Notifications and Structures tabs (D16, NTF-01, STR-07)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from evedm.access import AllianceGate
from evedm.config import Settings, load_settings
from evedm.wars.config import WarsSettings
from evedm.web.app import create_app

pytestmark = pytest.mark.anyio

A, B = 2100000001, 2100000002
BB = 99010320


@pytest.fixture
def gate(settings, services):  # noqa: ANN001, ANN201
    settings.access.alliance_ids = [BB]
    return AllianceGate(settings.access, services["db"], services["esi"])


async def test_only_a_main_of_the_configured_alliance_passes(gate, services, http, settings):
    db = services["db"]
    db.upsert_user(A, "Alpha One")
    await http.post("/_admin/affiliation", json={"character_id": A, "alliance_id": BB})
    assert await gate.refresh(A) == BB
    assert gate.allowed(A) is True

    db.set_affiliation(A, 98000001, 99009999)  # the main left the alliance
    assert gate.allowed(A) is False

    settings.access.alliance_ids = []  # no alliance configured: open to everyone
    assert gate.allowed(A) is True


async def test_an_unknown_alliance_keeps_the_tabs_hidden(gate, services):
    services["db"].upsert_user(B, "Bravo Two")
    assert gate.allowed(B) is False  # nothing read yet
    assert gate.allowed(123) is False  # not even an account


async def test_the_alliance_is_read_again_only_after_affiliation_hours(settings, services):
    settings.access.alliance_ids = [BB]
    calls: list[str] = []
    answers = iter([BB, 99009999])

    class Esi:
        async def get(self, path, *a, **kw):  # noqa: ANN002, ANN003
            calls.append(path)
            return {"corporation_id": 98000001, "alliance_id": next(answers)}

    gate = AllianceGate(settings.access, services["db"], Esi())  # type: ignore[arg-type]
    services["db"].upsert_user(A, "Alpha One")
    assert await gate.refresh(A) == BB and gate.allowed(A) is True
    assert await gate.refresh(A) == BB and len(calls) == 1  # cached: no second ESI call
    assert await gate.refresh(A, force=True) == 99009999  # sign-in reads it again
    assert len(calls) == 2 and gate.allowed(A) is False


def test_the_alliance_gate_lives_under_access_and_the_old_names_still_work(tmp_path, monkeypatch):
    assert Settings().access.alliance_ids == [BB]
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("notifications:\n  alliance_ids: [99000001]\n  affiliation_hours: 2\n", encoding="utf-8")
    old = load_settings(legacy)
    assert old.access.alliance_ids == [99000001] and old.access.affiliation_hours == 2

    new = tmp_path / "new.yaml"
    new.write_text(
        "access:\n  alliance_ids: [99000002]\nnotifications:\n  alliance_ids: [99000001]\n", encoding="utf-8"
    )
    assert load_settings(new).access.alliance_ids == [99000002]  # the access section wins

    monkeypatch.setenv("EVEDM_NOTIFICATIONS_ALLIANCES", "99000003")
    assert load_settings(new).access.alliance_ids == [99000003]  # 0.8.x variable
    monkeypatch.setenv("EVEDM_ACCESS_ALLIANCES", "99000004,99000005")
    assert load_settings(new).access.alliance_ids == [99000004, 99000005]  # the new variable wins


def web_client(tmp_path) -> TestClient:  # noqa: ANN001
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.secret_key = "access-secret"
    s.eve.client_id, s.eve.client_secret = "cid", "csecret"
    s.eve.sso_base = s.eve.esi_base = "http://127.0.0.1:9"  # never reached
    s.web.auth = "none"
    s.alerts.enabled = False
    s.access.alliance_ids = [BB]
    return TestClient(create_app(s, WarsSettings(enabled=False)))


def test_notifications_and_structures_are_hidden_until_the_alliance_matches(tmp_path):
    with web_client(tmp_path) as c:
        svc = c.app.state.svc
        svc.scout.start = lambda: None  # the scout would try to reach ESI
        for url in ("/notifications", "/structures", "/api/notifications", "/api/structures"):
            assert c.get(url).status_code == 404, url
        home = c.get("/").text
        assert 'href="/notifications"' not in home and 'href="/structures"' not in home

        svc.db.upsert_user(0, "local")
        svc.db.set_affiliation(0, 98000001, 99009999)  # another alliance
        assert c.get("/api/structures").status_code == 404 and c.get("/structures").status_code == 404

        svc.db.set_affiliation(0, 98000001, BB)
        for url in ("/notifications", "/structures", "/api/notifications?refresh=0", "/api/structures?refresh=0"):
            assert c.get(url).status_code == 200, url
        home = c.get("/").text
        assert 'href="/notifications"' in home and 'href="/structures"' in home
