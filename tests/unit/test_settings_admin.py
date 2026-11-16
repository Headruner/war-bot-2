"""The Settings page: open to the administrator main only, every setting of both configuration files, saved as
overrides in data/settings.yaml (secrets encrypted), validated before saving, applied to the running app."""

from __future__ import annotations

import time

import pytest
import yaml
from fastapi.testclient import TestClient
from test_accounts_web import H, as_user

from evedm.config import ConfigError, Settings, load_settings
from evedm.settings_admin import SettingsAdmin
from evedm.wars.config import WarsSettings
from evedm.wars.discord import NotifyError
from evedm.web import app as web_app
from evedm.web.app import create_app

SECRET = "accounts-web-secret"  # the key test_accounts_web signs its session cookies with
MIO, BOB = 2112000010, 2112000011
TOKEN = ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4OTA", "GabcDE", "abcdefghijklmnopqrstuvwxyz0123456789AB"))


def settings(tmp_path, **web) -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.secret_key = SECRET
    s.eve.client_id, s.eve.client_secret = "cid", "csecret"
    s.eve.sso_base = s.eve.esi_base = "http://127.0.0.1:9"  # never reached
    s.alerts.enabled = s.structures.enabled = s.evescout.enabled = False
    s.web.admin_characters = ["Shadowed Mio"]
    for k, v in web.items():
        setattr(s.web, k, v)
    return s


def fields(state: dict) -> dict[str, dict]:
    return {f["key"]: f for g in state["groups"] for f in g["fields"]}


class FakeNotifier:
    channel_id, dry_run, error = "42", False, None

    def __init__(self):
        self.sent = []

    async def send(self, payload: dict) -> dict:
        if self.error:
            raise self.error
        self.sent.append(payload)
        return {"id": "900"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=False))) as c:
        c.app.state.svc.db.upsert_user(MIO, "Shadowed Mio")
        c.app.state.svc.db.upsert_user(BOB, "Bob")
        yield as_user(c, MIO, "Shadowed Mio")


def test_only_the_administrator_main_gets_the_page(client, tmp_path):
    assert client.get("/settings").status_code == 200 and 'href="/settings"' in client.get("/").text
    assert client.get("/api/settings").status_code == 200
    as_user(client, MIO, "shadowed mio")  # the name is compared without case
    assert client.get("/api/settings").status_code == 200
    as_user(client, BOB, "Bob")
    assert client.get("/settings").status_code == 404 and 'href="/settings"' not in client.get("/").text
    assert client.get("/api/settings").status_code == 404
    assert client.put("/api/settings", json={"set": {"evedm:alerts.poll_s": 12}}, headers=H).status_code == 404
    assert client.post("/api/settings/restart", json={}, headers=H).status_code == 404
    as_user(client, None)
    assert client.get("/settings", follow_redirects=False).status_code == 303
    assert client.get("/api/settings").status_code == 401
    open_app = create_app(settings(tmp_path / "open", auth="none"), WarsSettings(enabled=False))
    with TestClient(open_app) as c:  # no sign-in, no administrator
        assert c.get("/settings").status_code == 404 and c.get("/api/settings").status_code == 404
    by_id = create_app(settings(tmp_path / "id", admin_characters=[str(BOB)]), WarsSettings(enabled=False))
    with TestClient(by_id) as c:
        c.app.state.svc.db.upsert_user(BOB, "Bob")
        assert as_user(c, BOB, "Bob").get("/api/settings").status_code == 200


def test_every_setting_is_listed_and_secrets_are_never_shown(client):
    response = client.get("/api/settings")
    state, f = response.json(), fields(response.json())
    assert state["groups"][0]["id"] == "evedm:discord" and state["file"].endswith("settings.yaml")
    for key in ("evedm:alerts.poll_s", "evedm:notifications.zkb_check.window_s", "wars:poll_interval_min"):
        assert key in f, key
    assert "evedm:secret_key" not in f and "wars:discord.bot_token" not in f
    assert "csecret" not in response.text and SECRET not in response.text
    secret = f["evedm:eve.client_secret"]
    assert (secret["type"], secret["value"], secret["is_set"]) == ("secret", None, True)
    assert f["evedm:web.auth"]["locked"] and f["evedm:web.admin_characters"]["locked"] and f["evedm:data_dir"]["locked"]
    assert f["evedm:log_level"]["restart"] and not f["evedm:alerts.poll_s"]["restart"]
    assert f["evedm:discord.bot_token"]["env"] == "EVEDM_DISCORD_BOT_TOKEN"
    assert "zKillboard" in f["evedm:alerts.poll_s"]["help"]  # the comment of the field in config.py
    assert f["wars:statuses"]["type"] == "list" and f["evedm:alerts.discord.enabled"]["type"] == "bool"
    assert state["discord"]["notifications"]["ready"] is False and state["discord"]["wars"]["ready"] is False


def test_saving_writes_the_file_and_changes_the_running_app(client, tmp_path):
    svc = client.app.state.svc
    assert svc.notifications.notifier is None
    body = {
        "set": {
            "evedm:discord.bot_token": TOKEN,
            "wars:discord.channel_id": "555000111222333444",
            "evedm:alerts.poll_s": 12,
            "evedm:log_level": "DEBUG",
            "evedm:access.alliance_ids": ["99010320", "99000001"],
            "wars:statuses": ["mutual"],
        }
    }
    assert client.put("/api/settings", json=body).status_code == 403  # the X-EVEDM header is required
    response = client.put("/api/settings", json=body, headers=H)
    assert response.status_code == 200, response.text
    out, f = response.json(), fields(response.json())
    assert set(out["changed"]) == set(body["set"]) and out["restart_needed"] == ["evedm:log_level"]
    assert TOKEN not in response.text and f["evedm:discord.bot_token"]["is_set"]
    assert f["evedm:alerts.poll_s"] == f["evedm:alerts.poll_s"] | {"value": 12.0, "base": 10.0, "overridden": True}
    s = svc.settings
    assert s.alerts.poll_s == 12.0 and s.access.alliance_ids == [99010320, 99000001] and s.discord.bot_token == TOKEN
    # the running app posts with the new token at once: notifications fall back to the wars channel
    notifier = svc.notifications.notifier
    assert (notifier.bot_token, notifier.channel_id) == (TOKEN, "555000111222333444")
    assert out["discord"]["notifications"]["ready"] is True
    text = (tmp_path / "data" / "settings.yaml").read_text()
    saved = yaml.safe_load(text)
    assert TOKEN not in text and saved["evedm"]["discord"]["bot_token"].startswith("enc:")
    assert saved["evedm"]["alerts"]["poll_s"] == 12.0 and saved["wars"]["statuses"] == ["mutual"]
    assert saved["wars"]["discord"] == {"channel_id": "555000111222333444"}  # only what was changed

    # a restart reads the file on top of the configuration; a reset returns to the configuration
    again, ws = settings(tmp_path), WarsSettings(enabled=False)
    SettingsAdmin(again, ws).load()
    assert again.discord.bot_token == TOKEN == ws.discord.bot_token and again.alerts.poll_s == 12.0
    assert ws.discord.channel_id == "555000111222333444" and ws.statuses == ["mutual"]
    response = client.put(
        "/api/settings", json={"reset": ["evedm:alerts.poll_s"], "set": {"evedm:log_level": "INFO"}}, headers=H
    )
    assert sorted(response.json()["changed"]) == ["evedm:alerts.poll_s", "evedm:log_level"]
    saved = yaml.safe_load((tmp_path / "data" / "settings.yaml").read_text())
    assert "alerts" not in saved["evedm"] and "log_level" not in saved["evedm"] and s.alerts.poll_s == 10.0
    assert client.put("/api/settings", json={"reset": ["evedm:discord.bot_token"]}, headers=H).status_code == 200
    assert svc.notifications.notifier is None and s.discord.bot_token == ""


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"set": {"evedm:notifications.poll_s": 5}}, "at least 30"),
        ({"set": {"evedm:alerts.poll_s": "soon"}}, "alerts.poll_s"),
        ({"set": {"wars:discord.channel_id": "#wars"}}, "numeric"),
        ({"set": {"evedm:web.auth": "none"}}, "cannot be changed here"),
        ({"set": {"evedm:secret_key": "x"}}, "unknown setting"),
        ({"set": {"evedm:discord.bot_token": ""}}, "empty"),
        ({"set": {"evedm:web.allowed_login_characters": [str(BOB)]}}, "your own character"),
        ({"set": {"evedm:notifications.concurrency": 1.5}}, "whole number"),
        ({"set": ["evedm:alerts.poll_s"]}, "'set' must be an object"),
        ({"reset": ["evedm:nope"]}, "unknown setting"),
    ],
)
def test_a_wrong_value_is_refused_and_nothing_is_saved(client, tmp_path, body, message):
    good = {"set": {"evedm:alerts.grid_km": 500}} | {k: v for k, v in body.items() if k != "set"}
    if isinstance(body.get("set"), dict):
        good["set"] |= body["set"]  # a good value in the same request is not saved either
        body = good
    response = client.put("/api/settings", json=body, headers=H)
    assert response.status_code == 400 and message in response.json()["detail"], response.text
    assert not (tmp_path / "data" / "settings.yaml").exists()
    assert client.app.state.svc.settings.alerts.grid_km == 1000.0


def test_the_whole_configuration_is_validated_before_saving(tmp_path):
    s, ws = settings(tmp_path), WarsSettings(enabled=True)
    s.alerts.enabled = True
    admin = SettingsAdmin(s, ws)
    admin.load()
    for changes, message in (
        ({"evedm:alerts.poll_s": 3}, "at least 6"),
        ({"evedm:alerts.discord.enabled": True}, "bot token"),
        ({"wars:discord.enabled": True, "wars:discord.channel_id": "42"}, "bot_token"),
    ):
        with pytest.raises(ConfigError, match=message):
            admin.save(changes, [], MIO)
    assert not admin.path.exists() and not ws.discord.enabled and s.alerts.poll_s == 10.0
    # the one bot token of the page serves the wars watcher too
    admin.save({"evedm:discord.bot_token": TOKEN, "wars:discord.enabled": True, "wars:discord.channel_id": 42}, [], MIO)
    assert ws.discord.enabled and ws.discord.bot_token == TOKEN and ws.discord.channel_id == "42"


def test_a_file_that_breaks_the_configuration_is_ignored_at_start(tmp_path):
    path = tmp_path / "data" / "settings.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("evedm:\n  notifications:\n    poll_s: 1\n  alerts:\n    grid_km: 250\n  nope: 1\n")
    s = settings(tmp_path)
    admin = SettingsAdmin(s, WarsSettings(enabled=False))
    admin.load()  # poll_s 1 is invalid: the whole file is left out, the app starts on its configuration
    assert s.notifications.poll_s == 60.0 and s.alerts.grid_km == 1000.0 and "ignored" in admin.note
    path.write_text("evedm:\n  alerts:\n    grid_km: 250\n  web:\n    auth: none\n  nope: 1\n")
    s = settings(tmp_path)
    SettingsAdmin(s, WarsSettings(enabled=False)).load()
    assert s.alerts.grid_km == 250.0 and s.web.auth == "sso"  # locked and unknown keys of the file are skipped
    path.write_text("not: [valid")
    s = settings(tmp_path)
    SettingsAdmin(s, WarsSettings(enabled=False)).load()
    assert s.alerts.grid_km == 1000.0
    broken = settings(tmp_path / "other")
    broken.alerts.enabled, broken.alerts.poll_s = True, 1  # invalid without any file: reported as before
    with pytest.raises(ConfigError, match="at least 6"):
        create_app(broken, WarsSettings(enabled=False))


def test_test_message_and_restart(client, monkeypatch):
    svc = client.app.state.svc
    post = lambda target: client.post("/api/settings/discord/test", json={"target": target}, headers=H)  # noqa: E731
    assert post("notifications").status_code == 400 and post("everything").status_code == 400
    svc.notifications.notifier = note = FakeNotifier()
    assert post("notifications").json() == {"ok": True, "detail": "message posted"}
    assert "Shadowed Mio" in note.sent[0]["embeds"][0]["description"]
    note.error = NotifyError(f"Discord error 400 for Bot {TOKEN}")
    response = post("notifications")
    assert response.status_code == 400 and TOKEN not in response.text and "redacted" in response.text

    stopped = []
    monkeypatch.setattr(web_app.os, "kill", lambda pid, sig: stopped.append(sig))
    assert client.post("/api/settings/restart", json={}, headers=H).json() == {"restarting": True}
    deadline = time.time() + 3
    while not stopped and time.time() < deadline:
        client.get("/api/health")  # lets the app's event loop run its timer
        time.sleep(0.1)
    assert stopped == [web_app.signal.SIGTERM]


def test_admin_characters_from_the_environment_and_yaml(monkeypatch, tmp_path):
    cfg = tmp_path / "evedm.yaml"
    cfg.write_text("web:\n  admin_characters: ${EVEDM_ADMIN_CHARACTERS:-Shadowed Mio}\n")
    assert load_settings(cfg).web.admin_characters == ["Shadowed Mio"]
    monkeypatch.setenv("EVEDM_ADMIN_CHARACTERS", "Shadowed Mio, 2112000011")
    assert load_settings(cfg).web.admin_characters == ["Shadowed Mio", "2112000011"]
    assert Settings().web.admin_characters == []  # nobody unless configured


def test_discord_rules_are_edited_on_the_page(client, tmp_path):
    svc = client.app.state.svc
    state = client.get("/api/settings").json()
    assert fields(state)["evedm:discord.rules"] == fields(state)["evedm:discord.rules"] | {"type": "rules", "value": []}
    options = state["rule_options"]
    assert options["kinds"] == ["destroyed", "combat", "firing"] and "The Forge" in options["regions"]
    assert "StructureUnderAttack" in options["groups"]["structure_attack"] and "wormhole" in options["spaces"]

    def put(rules):
        return client.put("/api/settings", json={"set": {"evedm:discord.rules": rules}}, headers=H)

    attacks = {"source": "notifications", "channel_id": "11", "name": "attacks", "groups": ["structure_attack"]}
    for rules, message in (
        ([attacks | {"systems": ["Jitta"]}], "rule 1: unknown solar system 'Jitta'"),
        ([attacks, attacks | {"exclude_regions": ["Mordor"]}], "rule 2: unknown region 'Mordor'"),
        ([attacks | {"channel_id": "#ops"}], "rule 1 (attacks): channel_id"),
        ([attacks | {"source": "wars"}], "source must be one of"),
        ("attacks", "must be a list"),
    ):
        response = put(rules)
        assert response.status_code == 400 and message in response.json()["detail"], response.text
    assert svc.settings.discord.rules == [] and not (tmp_path / "data" / "settings.yaml").exists()

    kills = {"source": "alerts", "channel_id": 22, "kinds": ["destroyed"], "systems": ["jita"], "regions": ["domain"]}
    response = put([attacks, kills])
    assert response.status_code == 200 and response.json()["changed"] == ["evedm:discord.rules"], response.text
    saved = svc.settings.discord.rules
    assert [r["channel_id"] for r in saved] == ["11", "22"] and saved[0]["enabled"] is True
    assert saved[1]["systems"] == ["Jita"] and saved[1]["regions"] == ["Domain"]  # as the universe spells them
    on_disk = yaml.safe_load((tmp_path / "data" / "settings.yaml").read_text())["evedm"]["discord"]["rules"]
    assert on_disk == saved and fields(response.json())["evedm:discord.rules"]["overridden"]
    # the running services see the rules at once; without a bot token nothing can be posted yet
    assert svc.notifications.rules() == saved and svc.notifications.notifier is None
    status = response.json()["discord"]["notifications"]
    assert status["detail"] == "1 of 1 rule(s) on, 1 channel(s); the bot token is missing" and not status["ready"]
    test = client.post("/api/settings/discord/test", json={"channel_id": "11"}, headers=H)
    assert test.status_code == 400 and "bot token" in test.json()["detail"]
    client.put("/api/settings", json={"set": {"evedm:discord.bot_token": TOKEN}}, headers=H)
    notifier = svc.notifications.notifier
    assert (notifier.bot_token, notifier.channel_id) == (TOKEN, "")  # the rules name the channels

    again = settings(tmp_path)  # after a restart
    SettingsAdmin(again, WarsSettings(enabled=False)).load()
    assert again.discord.rules == saved
    assert client.put("/api/settings", json={"reset": ["evedm:discord.rules"]}, headers=H).status_code == 200
    assert svc.settings.discord.rules == [] and svc.notifications.notifier is None


def test_discord_without_a_bot_is_switched_off_at_start_up_instead_of_stopping_the_app(tmp_path):
    s = settings(tmp_path)
    s.alerts.enabled = s.alerts.discord.enabled = True  # a webhook-era configuration: no bot token, no channel ID
    ws = WarsSettings(enabled=True)
    ws.discord.enabled = True
    with TestClient(create_app(s, ws)) as c:
        assert s.alerts.discord.enabled is False and ws.discord.enabled is False
        c.app.state.svc.db.upsert_user(MIO, "Shadowed Mio")
        state = as_user(c, MIO, "Shadowed Mio").get("/api/settings").json()
        assert "switched off at start-up" in (state["note"] or "")
        # saving the same combination from the page is still refused
        bad = c.put("/api/settings", json={"set": {"evedm:alerts.discord.enabled": True}}, headers=H)
        assert bad.status_code == 422 or "needs the bot token" in bad.text


def test_a_complete_discord_setup_is_left_alone(tmp_path):
    s = settings(tmp_path)
    s.alerts.enabled = s.alerts.discord.enabled = True
    s.discord.bot_token, s.alerts.discord.channel_id = TOKEN, "123456789012345678"
    with TestClient(create_app(s, WarsSettings(enabled=False))):
        assert s.alerts.discord.enabled is True
