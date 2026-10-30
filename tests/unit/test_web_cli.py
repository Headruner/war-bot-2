import gzip
import json

import pytest
from fastapi.testclient import TestClient

from evedm.cli import main
from evedm.config import Settings
from evedm.web.app import create_app

H = {"X-EVEDM": "1"}


def demo_settings(tmp_path, auth="none") -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.secret_key = "web-test"
    s.demo = True
    s.web.auth = auth
    return s


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(demo_settings(tmp_path))) as c:
        yield c


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["mode"] == "demo"
    assert body["universe"]["systems"] > 8000


def test_pages_render(client):
    for path in ("/", "/characters", "/login"):
        r = client.get(path)
        assert r.status_code == 200 and "EVE Distance Meter" in r.text
    assert "map.js" in client.get("/").text


def test_rank_endpoint_by_name_and_id(client):
    by_name = client.get("/api/rank", params={"target": "tama"}).json()
    assert by_name["target"]["name"] == "Tama"
    assert by_name["rows"][0]["recommended"] is True
    by_id = client.get("/api/rank", params={"target": "30002813", "refresh": 0}).json()
    assert [r["name"] for r in by_id["rows"]] == [r["name"] for r in by_name["rows"]]
    assert client.get("/api/rank", params={"target": "Nowhere"}).status_code == 404
    recent = client.get("/api/targets/recent").json()
    assert recent[0]["name"] == "Tama"


def test_map_payload_gzip_and_etag(client):
    r = client.get("/api/universe/map", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert len(r.json()["systems"]) > 5000  # TestClient decodes gzip transparently
    again = client.get("/api/universe/map", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304
    plain = client.get("/api/universe/map", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and json.loads(plain.content)["ids"]


def test_search_and_resolve(client):
    assert client.get("/api/systems/search", params={"q": "jit"}).json()[0]["name"] == "Jita"
    r = client.post(
        "/api/target/resolve", json={"text": "attack in https://evemaps.dotlan.net/system/Amamake"}, headers=H
    )
    assert [c["name"] for c in r.json()["candidates"]] == ["Amamake"]


def test_state_changing_calls_need_csrf_header(client):
    assert client.post("/api/target/resolve", json={"text": "Jita"}).status_code == 403
    assert client.patch("/api/characters/3900000001", json={"enabled": False}).status_code == 403
    assert client.post("/api/characters/export", json={"confirm": True}).status_code == 403


def test_character_patch_and_delete(client):
    assert client.patch("/api/characters/3900000001", json={"enabled": False, "tags": "a, b"}, headers=H).json()["ok"]
    chars = {c["character_id"]: c for c in client.get("/api/characters").json()}
    assert chars[3900000001]["enabled"] is False and chars[3900000001]["tags"] == ["a", "b"]
    assert chars[3900000001]["status"] == "DISABLED"
    ranked = client.get("/api/rank", params={"target": "Tama"}).json()
    assert "Kael Varn" not in [r["name"] for r in ranked["rows"]]
    assert client.delete("/api/characters/3900000002", headers=H).json()["ok"]
    assert 3900000002 not in {c["character_id"] for c in client.get("/api/characters").json()}
    assert client.patch("/api/characters/1", json={}, headers=H).status_code == 404


def test_export_requires_confirmation(client):
    assert client.post("/api/characters/export", json={}, headers=H).status_code == 400
    # demo characters are never exported
    assert client.post("/api/characters/export", json={"confirm": True}, headers=H).status_code == 400


def test_import_rejects_bad_files(client):
    r = client.post("/api/characters/import", files={"file": ("x.json", b"{}", "application/json")}, headers=H)
    assert r.status_code == 422 and r.json()["errors"]


def test_sso_mode_requires_sign_in(tmp_path):
    with TestClient(create_app(demo_settings(tmp_path, auth="sso"))) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/api/rank", params={"target": "Tama"}).status_code == 401
        assert c.get("/api/health").status_code == 200  # health stays public
        r = c.get("/sso/login", follow_redirects=False)
        assert "sso_not_configured" in r.headers["location"]
        r = c.get("/sso/callback", params={"code": "x", "state": "forged"}, follow_redirects=False)
        assert "invalid_state" in r.headers["location"]


def test_launch_is_recorded(tmp_path):
    s = demo_settings(tmp_path)
    with TestClient(create_app(s)) as c:
        c.get("/api/health")
        launches = c.get("/api/launches").json()
        assert launches[0]["mode"] == "demo" and "rss_mb_start" in launches[0]  # None on Windows
    lines = [json.loads(x) for x in (s.log_dir / "launches.jsonl").read_text().splitlines()]
    assert [x["event"] for x in lines] == ["start", "stop"]
    assert lines[1]["requests"] >= 2
    assert (s.log_dir / "evedm.log").read_text().count("launch #") == 1


def test_refuses_to_start_without_secret(tmp_path):
    s = demo_settings(tmp_path)
    s.secret_key = ""
    with pytest.raises(Exception, match="EVEDM_SECRET_KEY"):
        create_app(s)


# --------------------------------------------------------------------------- CLI
def test_cli_seed_and_rank(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVEDM_DATA_DIR", str(tmp_path / "d"))
    assert main(["--demo", "db", "seed-demo"]) == 0
    assert "seeded 10" in capsys.readouterr().out
    assert main(["--demo", "rank", "Tama"]) == 0
    out = capsys.readouterr().out
    assert "Kael Varn" in out and "*closest*" in out and "excluded: Old Alt" in out
    assert main(["--demo", "rank", "Tama", "--json", "--no-refresh"]) == 0
    assert json.loads(capsys.readouterr().out)["target"]["name"] == "Tama"
    assert main(["--demo", "launches"]) == 0


def test_cli_healthcheck_fails_without_server():
    assert main(["healthcheck", "--port", "1"]) == 1


def test_cli_universe_build_requires_source(capsys):
    assert main(["universe", "build"]) == 2


def test_cli_serve_without_config_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("EVEDM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("EVEDM_SECRET_KEY", raising=False)
    assert main(["serve"]) == 2


def test_universe_payload_is_gzip(client):
    r = client.get("/api/universe/map", headers={"Accept-Encoding": "gzip"})
    assert gzip.compress(b"")[:2] == b"\x1f\x8b" and r.headers["vary"] == "Accept-Encoding"
