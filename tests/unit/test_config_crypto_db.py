import time

import pytest

from evedm.config import ConfigError, Settings, load_settings
from evedm.crypto import TokenCipher, redact, tail
from evedm.db import SCHEMA_VERSION, Database


# --------------------------------------------------------------------------- config
def test_yaml_with_env_expansion_and_env_override(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "instance_label: vm\n"
        "eve:\n  client_id: ${MY_ID}\n  client_secret: ${MY_SECRET:-fallback}\n"
        "web:\n  port: 9000\n  allowed_login_characters: [1, 2]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_ID", "abc")
    monkeypatch.setenv("EVEDM_PORT", "8088")
    s = load_settings(cfg)
    assert s.instance_label == "vm"
    assert s.eve.client_id == "abc" and s.eve.client_secret == "fallback"
    assert s.web.port == 8088  # env wins
    assert s.web.allowed_login_characters == [1, 2]


def test_allowed_characters_from_env(monkeypatch):
    monkeypatch.setenv("EVEDM_ALLOWED_LOGIN_CHARACTERS", "10, 20;30")
    assert load_settings(None).web.allowed_login_characters == [10, 20, 30]


def test_demo_defaults_to_no_web_auth(monkeypatch):
    monkeypatch.setenv("EVEDM_DEMO", "1")
    s = load_settings(None)
    assert s.demo and s.web.auth == "none"


def test_validate_for_serve_requires_secrets():
    s = Settings()
    with pytest.raises(ConfigError, match="EVEDM_SECRET_KEY"):
        s.validate_for_serve()
    s.secret_key = "k"
    with pytest.raises(ConfigError, match="EVE_CLIENT_ID"):
        s.validate_for_serve()
    s.demo = True
    s.validate_for_serve()
    s.sqlite_journal = "memory"
    with pytest.raises(ConfigError):
        s.validate_for_serve()


def test_summary_has_no_secrets():
    s = Settings(secret_key="supersecret")
    s.eve.client_secret = "verysecret"
    text = str(s.summary())
    assert "supersecret" not in text and "verysecret" not in text


# --------------------------------------------------------------------------- crypto
def test_cipher_roundtrip_and_wrong_key():
    blob = TokenCipher("one").encrypt("refresh-token-value")
    assert TokenCipher("one").decrypt(blob) == "refresh-token-value"
    with pytest.raises(ValueError):
        TokenCipher("two").decrypt(blob)
    with pytest.raises(ValueError):
        TokenCipher("")


def test_redact_masks_jwts_and_tokens():
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJDSEFSQUNURVIifQ.c2lnbmF0dXJlLXZhbHVl"
    text = redact(f"Bearer {jwt} refresh_token=abcdefghijklmnop123 ok")
    assert jwt not in text and "abcdefghijklmnop123" not in text and "ok" in text


def test_tail():
    assert tail("abcdefgh") == "…efgh"
    assert tail("") == ""


# --------------------------------------------------------------------------- db
@pytest.mark.parametrize("mode", ["delete", "wal"])
def test_journal_modes(tmp_path, mode):
    d = Database(tmp_path / "x.sqlite3", mode)
    assert d.journal_mode == mode
    assert d.ok()
    d.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "x.sqlite3"
    Database(path).close()
    d = Database(path)
    assert d.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION
    d.close()


def test_upsert_keeps_local_settings(db):
    assert db.upsert_character(1, "A", "OK", ["s1"], tags=["x"]) is True
    db.set_enabled(1, False)
    db.set_tags(1, ["main", " fc ", ""])
    assert db.upsert_character(1, "A renamed", "LIMITED", []) is False
    c = db.get_character(1)
    assert c.name == "A renamed" and c.status == "LIMITED"
    assert c.enabled is False and c.tags == ["fc", "main"]


def test_sso_state_single_use_and_expiry(db):
    db.create_state("s1", "login")
    assert db.consume_state("s1") == "login"
    assert db.consume_state("s1") is None  # used
    db.create_state("s2", "add")
    db.execute("UPDATE sso_states SET created_at=? WHERE state='s2'", (time.time() - 601,))
    assert db.consume_state("s2") is None  # older than 10 minutes
    assert db.consume_state("nope") is None


def test_recent_targets_keeps_last_20(db):
    for i in range(25):
        db.remember_target(30000000 + i)
        db.execute("UPDATE target_history SET used_at=? WHERE solar_system_id=?", (i, 30000000 + i))
    db.remember_target(30000099)
    recent = db.recent_targets()
    assert len(recent) == 20 and recent[0] == 30000099


def test_launch_records(db):
    lid = db.record_launch(
        started_at="t",
        version="v",
        pid=1,
        mode="demo",
        python="3",
        platform="p",
        cpu_count=2,
        rss_mb_start=50.0,
        config="{}",
    )
    db.finish_launch(lid, stopped_at="t2", rss_mb_peak=60.0, requests=3)
    row = db.launches()[0]
    assert row["requests"] == 3 and row["rss_mb_peak"] == 60.0


def test_env_defaults_apply_to_empty_variables(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("eve:\n  callback_url: ${EVE_CALLBACK_URL:-http://localhost:8080/sso/callback}\n")
    monkeypatch.setenv("EVE_CALLBACK_URL", "")  # a key left empty in .env
    assert load_settings(cfg).eve.callback_url == "http://localhost:8080/sso/callback"
    monkeypatch.setenv("EVE_CALLBACK_URL", "http://localhost:8081/sso/callback")
    assert load_settings(cfg).eve.callback_url == "http://localhost:8081/sso/callback"


def test_discord_webhook_tokens_are_redacted():
    line = "Discord request failed for https://discord.com/api/webhooks/1234567890/AbC-def_123.xyz?wait=true"
    out = redact(line)
    assert "AbC-def_123" not in out and "webhooks/1234567890/<redacted>" in out
    assert (
        redact("https://discordapp.com/api/v10/webhooks/42/tok")
        == "https://discordapp.com/api/v10/webhooks/42/<redacted>"
    )


def test_execute_returns_row_ids_for_inserts_and_counts_otherwise(db):
    db.upsert_user(1, "One")
    rid = db.execute("INSERT INTO sso_states(state, purpose, created_at) VALUES('s1', 'login', 1)")
    assert rid >= 1
    db.execute("INSERT INTO sso_states(state, purpose, created_at) VALUES('s2', 'login', 1)")
    assert db.execute("UPDATE sso_states SET used=1 WHERE state IN ('s1','s2')") == 2
    assert db.execute("DELETE FROM sso_states WHERE state='nope'") == 0  # not the last insert's row ID
    assert db.execute("DELETE FROM sso_states") == 2
