"""War watcher (merged eve-war-bot-2): polling, filtering, Discord posting, structures, Wars page and config."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from evedm.config import ConfigError, Settings
from evedm.wars.config import WarsSettings, load_wars_settings
from evedm.wars.discord import DiscordNotifier, NotifyError
from evedm.wars.embeds import FIELD_LIMIT, _structure_value
from evedm.wars.esi import WarEsi, WarEsiError
from evedm.wars.service import WarService
from evedm.wars.store import MAX_POST_ATTEMPTS
from evedm.wars.structures import Structure, StructureIndex, build_index
from evedm.web.app import create_app
from fake_war_esi import NAMES, FakeEsi, make_war

pytestmark = pytest.mark.anyio
FIRST_ID = 763600
JITA, PERIMETER, TAMA, DQ = 30000142, 30000144, 30002813, 30004759


class CapturingNotifier(DiscordNotifier):
    def __init__(self, fail: bool = False):
        super().__init__("https://discord.invalid/webhook", 0)
        self.fail = fail
        self.payloads = []

    async def send(self, payload):
        if self.fail:
            raise NotifyError("Discord error 400: bad embed")
        self.payloads.append(payload)
        return {"id": f"msg{len(self.payloads)}"}

    def war_ids(self):
        return [int(e["footer"]["text"].split("#")[1]) for p in self.payloads for e in p["embeds"]]


def hourly_wars(hours_ago):
    """One war per entry, IDs ascending as declarations get newer."""
    now = datetime.now(UTC)
    ordered = sorted(hours_ago, reverse=True)
    return {FIRST_ID + i: make_war(FIRST_ID + i, now - timedelta(hours=h)) for i, h in enumerate(ordered)}


def service(db, graph, esi, notifier=None, structures=None, **kw) -> WarService:
    ws = WarsSettings(**({"enabled": True, "bootstrap_hours": 24} | kw))
    ws.discord.batch_embeds = 5
    return WarService(ws, db, graph, esi, notifier=notifier, structures=structures, page_url="http://x/wars")


async def test_clean_db_posts_last_24h_once(db, graph):
    esi = FakeEsi(hourly_wars([1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34, 37, 40]))
    notifier = CapturingNotifier()
    svc = service(db, graph, esi, notifier)
    await svc.run_cycle()
    assert notifier.war_ids() == list(range(FIRST_ID + 6, FIRST_ID + 14))  # the 8 wars < 24h old
    assert len(notifier.payloads) == 2  # batches of 5 + 3
    await svc.run_cycle()
    assert len(notifier.payloads) == 2, "wars must not be posted twice"
    assert svc.store.stats() == {"notified": 8, "stale": 6}
    links = notifier.payloads[0]["embeds"][0]["fields"][-1]["value"]
    assert "http://x/wars#war-" in links


async def test_new_war_after_bootstrap_is_posted(db, graph):
    wars = hourly_wars([30, 2])
    esi = FakeEsi(wars)
    notifier = CapturingNotifier()
    svc = service(db, graph, esi, notifier)
    await svc.run_cycle()
    assert notifier.war_ids() == [FIRST_ID + 1]
    wars[FIRST_ID + 2] = make_war(FIRST_ID + 2, datetime.now(UTC), mutual=True)
    await svc.run_cycle()
    assert notifier.war_ids()[-1] == FIRST_ID + 2
    assert notifier.payloads[-1]["embeds"][0]["description"] == "Mutual war"


async def test_without_discord_wars_are_ready_for_the_page(db, graph):
    svc = service(db, graph, FakeEsi(hourly_wars([3, 1])))
    await svc.run_cycle()
    assert svc.store.stats() == {"ready": 2}
    items = svc.store.summaries()
    assert [w["war_id"] for w in items] == [FIRST_ID + 1, FIRST_ID]  # newest first
    w = items[0]
    assert w["aggressor"]["name"] == "BLACKFLAG." and w["aggressor"]["corporations"] == 2
    assert w["defender"]["members"] == 7 and w["post_status"] == "ready"
    assert svc.store.monthly()[0]["total"] == 2


async def test_bootstrap_hours_zero_starts_from_now(db, graph):
    notifier = CapturingNotifier()
    svc = service(db, graph, FakeEsi(hourly_wars([1, 2, 3])), notifier, bootstrap_hours=0)
    await svc.run_cycle()
    assert notifier.payloads == []
    assert svc.store.get_meta("watermark_war_id") == str(FIRST_ID + 2)


async def test_heals_test_data_watermark(db, graph):
    svc = service(db, graph, FakeEsi(hourly_wars([30, 5, 1])), CapturingNotifier())
    svc.store.set_meta("bootstrapped", "true")
    svc.store.set_meta("watermark_war_id", "1000002")
    svc.store.mark_marker(1000001, "pending")
    await svc.run_cycle()
    assert svc.notifier.war_ids() == [FIRST_ID + 1, FIRST_ID + 2]
    assert not svc.store.has(1000001) and 1000001 not in svc.esi.war_calls


async def test_statuses_filter(db, graph):
    wars = hourly_wars([3, 2, 1])
    wars[FIRST_ID + 1]["mutual"] = True
    notifier = CapturingNotifier()
    svc = service(db, graph, FakeEsi(wars), notifier, statuses=["mutual"])
    await svc.run_cycle()
    assert notifier.war_ids() == [FIRST_ID + 1]
    assert svc.store.stats() == {"filtered": 2, "notified": 1}


async def test_failed_post_is_retried_then_parked(db, graph):
    svc = service(db, graph, FakeEsi(hourly_wars([1])), CapturingNotifier(fail=True))
    for _ in range(MAX_POST_ATTEMPTS):
        await svc.run_cycle()
    assert svc.store.stats() == {"failed": 1}


async def test_transient_esi_error_does_not_skip_war(db, graph):
    esi = FakeEsi(hourly_wars([2, 1]))
    flaky, real_war, calls = FIRST_ID + 1, esi.war, {"n": 0}

    async def war(war_id):
        if war_id == flaky:
            calls["n"] += 1
            if calls["n"] in (2, 3):  # call 1 is the bootstrap header read
                raise WarEsiError("HTTP 503", 503)
        return await real_war(war_id)

    esi.war = war
    notifier = CapturingNotifier()
    svc = service(db, graph, esi, notifier)
    await svc.run_cycle()
    assert notifier.war_ids() == [FIRST_ID]
    assert flaky in svc.store.pending_ids()
    await svc.run_cycle()
    await svc.run_cycle()
    assert notifier.war_ids() == [FIRST_ID, flaky]


async def test_reissued_old_wars_are_stale(db, graph):
    now = datetime.now(UTC)
    ages = [200, 5, 4, 90, 80, 3, 2]  # hours ago, by ascending war ID
    wars = {FIRST_ID + i: make_war(FIRST_ID + i, now - timedelta(hours=h)) for i, h in enumerate(ages)}
    notifier = CapturingNotifier()
    svc = service(db, graph, FakeEsi(wars), notifier)
    await svc.run_cycle()
    assert notifier.war_ids() == [FIRST_ID + 1, FIRST_ID + 2, FIRST_ID + 5, FIRST_ID + 6]
    wars[FIRST_ID + 7] = make_war(FIRST_ID + 7, now - timedelta(days=60), retracted="2026-10-28T02:55:00Z")
    wars[FIRST_ID + 8] = make_war(FIRST_ID + 8, now)
    await svc.run_cycle()
    assert notifier.war_ids()[-1] == FIRST_ID + 8 and FIRST_ID + 7 not in notifier.war_ids()


async def test_structures_give_rankable_systems(db, graph):
    NAMES.setdefault(35832, ("Astrahus", "inventory_type"))
    NAMES.setdefault(35825, ("Raitaru", "inventory_type"))

    def st(i, name, type_id, system):
        return Structure(i, name, type_id, system, 98000001)

    structures = StructureIndex(graph, "http://unused", "ua")
    structures.index = {
        98000001: [  # a corporation of the aggressor alliance
            st(1, "Jita - Trade Hub", 35832, JITA),
            st(2, "Perimeter - Edge", 35825, PERIMETER),
            st(3, "Tama - Lowsec Base", 35832, TAMA),
            st(4, "1DQ1-A - Nullsec Keep", 35832, DQ),
            st(5, "Tama - Second", 35832, TAMA),
        ]
    }
    structures.last_refresh = 9e18  # do not download in tests
    notifier = CapturingNotifier()
    svc = service(db, graph, FakeEsi(hourly_wars([1])), notifier, structures=structures)
    await svc.run_cycle()

    fields = {f["name"]: f["value"] for f in notifier.payloads[0]["embeds"][0]["fields"]}
    agg = fields["🏗 Aggressor structures"]
    assert "**5** public: 4× Astrahus, 1× Raitaru" in agg and "High-sec (2):" in agg
    assert "• Perimeter (1.0) – Edge [Raitaru]" in agg and "• Jita (0.9) – Trade Hub [Astrahus]" in agg
    assert "Tama" not in agg and "1DQ1-A" not in agg
    assert fields["🏗 Defender structures"] == "No public structures known"

    war = svc.store.summaries()[0]
    assert [t["name"] for t in war["targets"]] == ["Tama", "1DQ1-A", "Jita", "Perimeter"]  # most structures first
    assert war["targets"][0]["why"] == ["2 aggressor structure(s)"]
    assert war["structures"]["aggressor"]["by_type"] == {"Astrahus": 4, "Raitaru": 1}
    assert {s["name"] for s in war["structures"]["aggressor"]["structures"]} >= {"Trade Hub", "Nullsec Keep"}


async def test_structure_dataset_parsing_and_alliance_summary(graph):
    dataset = {
        "1": {"structure_id": 1, "owner_id": 1, "type_id": 35832, "solar_system_id": JITA, "name": "Jita - A"},
        "2": {"structure_id": 2, "owner_id": 2, "type_id": 35825},
        "3": {"structure_id": 3, "owner_id": 2, "type_id": 35825},
        "4": {"structure_id": 4, "is_public_structure": False},  # no owner: ignored
    }
    index = StructureIndex(graph, "http://test", "ua")
    index.index = build_index(json.dumps(dataset).encode())

    class Esi:
        async def alliance_corporations(self, _):
            return [1, 2, 3]

    class P:
        type, entity_id = "alliance", 99010320

    summary = await index.summarize(Esi(), P())
    assert summary["total"] == 3 and summary["by_type"] == {35832: 1, 35825: 2}
    assert summary["corps"] == [(2, 2), (1, 1)] and summary["corp_count"] == 3
    assert summary["structures"][0]["name"] == "A" and summary["structures"][0]["system"]["name"] == "Jita"


async def test_structure_refresh_downloads_with_user_agent(graph):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json={"9": {"structure_id": 9, "owner_id": 5, "type_id": 35832}})

    index = StructureIndex(graph, "https://data.test/s.json", "evedm-test", transport=httpx.MockTransport(handler))
    await index.refresh_if_due()
    assert index.loaded and index.owners == 1 and seen["ua"] == "evedm-test"


def test_structure_field_fits_discord_limit():
    summary = {
        "total": 300,
        "by_type": {35832: 300},
        "corps": [],
        "corp_count": 1,
        "highsec": [{"system": f"S{i:03}", "security": 0.9, "name": "X" * 20, "type_id": 35832} for i in range(300)],
    }
    value = _structure_value(summary, {35832: "Astrahus"})
    assert len(value) <= FIELD_LIMIT and value.splitlines()[-1].startswith("…+")


async def test_esi_names_posts_bare_array_with_compatibility_date():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["compat"] = request.headers.get("X-Compatibility-Date")
        return httpx.Response(200, json=[{"category": "alliance", "id": 99010015, "name": "BLACKFLAG."}])

    esi = WarEsi("https://esi.test", "ua", "2025-09-30", transport=httpx.MockTransport(handler))
    try:
        assert await esi.names([99010015, None]) == {99010015: ("BLACKFLAG.", "alliance")}
    finally:
        await esi.close()
    assert seen == {"body": [99010015], "compat": "2025-09-30"}


async def test_notifier_waits_for_message_and_honours_429():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("wait"))
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(200, json={"id": "123"})

    notifier = DiscordNotifier("https://discord.test/api/webhooks/1/x", 0, transport=httpx.MockTransport(handler))
    assert (await notifier.send({"embeds": []}))["id"] == "123"
    assert calls == ["true", "true"]


# --------------------------------------------------------------------------- config
def test_wars_config_file_env_and_validation(tmp_path, monkeypatch):
    cfg = tmp_path / "wars.yaml"
    cfg.write_text(
        "enabled: ${EVEDM_WARS_ENABLED:-true}\npoll_interval_min: 5\nstatuses: [mutual]\n"
        "discord:\n  enabled: true\n  webhook_url: ${EVEDM_WARS_DISCORD_WEBHOOK}\n  batch_embeds: 3\n"
    )
    monkeypatch.setenv("EVEDM_WARS_DISCORD_WEBHOOK", "https://discord.test/hook")
    ws = load_wars_settings(cfg)
    assert ws.enabled and ws.poll_interval_min == 5 and ws.statuses == ["mutual"]
    assert ws.discord.webhook_url == "https://discord.test/hook" and ws.discord.batch_embeds == 3
    assert ws.summary()["webhook_set"] is True and "discord.test" not in json.dumps(ws.summary())

    monkeypatch.setenv("EVEDM_WARS_ENABLED", "0")
    assert load_wars_settings(cfg).enabled is False

    monkeypatch.delenv("EVEDM_WARS_ENABLED")
    monkeypatch.setenv("EVEDM_WARS_DISCORD_WEBHOOK", "")
    with pytest.raises(ConfigError, match="webhook_url"):
        load_wars_settings(cfg)
    cfg.write_text("statuses: [peace]\n")
    with pytest.raises(ConfigError, match="unknown war type"):
        load_wars_settings(cfg)


def test_wars_config_defaults_next_to_main_config(tmp_path, monkeypatch):
    (tmp_path / "evedm.yaml").write_text("{}")
    (tmp_path / "wars.yaml").write_text("enabled: true\n")
    monkeypatch.delenv("EVEDM_WARS_CONFIG", raising=False)
    monkeypatch.setenv("EVEDM_CONFIG", str(tmp_path / "evedm.yaml"))
    assert load_wars_settings().enabled is True
    monkeypatch.setenv("EVEDM_WARS_CONFIG", str(tmp_path / "missing.yaml"))
    assert load_wars_settings().enabled is False  # no file: watcher off


# --------------------------------------------------------------------------- web
def test_wars_page_and_api_in_demo(tmp_path):
    s = Settings()
    s.data_dir, s.secret_key, s.demo, s.web.auth = tmp_path / "d", "k", True, "none"
    with TestClient(create_app(s, WarsSettings(enabled=True))) as c:
        page = c.get("/wars")
        assert page.status_code == 200 and "wars.js" in page.text
        assert 'href="/wars"' in c.get("/").text
        body = c.get("/api/wars").json()
        assert [w["war_id"] for w in body["wars"]] == [990001, 990002, 990003]
        assert body["wars"][0]["targets"][0]["name"] == "Tama" and body["status"]["running"] is False
        assert [w["war_id"] for w in c.get("/api/wars", params={"type": "mutual"}).json()["wars"]] == [990002]
        assert c.get("/api/wars/990003").json()["type"] == "open_for_allies"
        assert c.get("/api/wars/1").status_code == 404


def test_wars_disabled_hides_page(tmp_path):
    s = Settings()
    s.data_dir, s.secret_key, s.demo, s.web.auth = tmp_path / "d", "k", True, "none"
    with TestClient(create_app(s, WarsSettings(enabled=False))) as c:
        assert c.get("/wars").status_code == 404 and c.get("/api/wars").status_code == 404
        assert 'href="/wars"' not in c.get("/").text


# --------------------------------------------------------------------------- CLI
def test_cli_wars_list_stats_check(tmp_path, monkeypatch, capsys, graph):
    from evedm.cli import main
    from evedm.db import Database
    from evedm.wars.demo import seed_demo_wars
    from evedm.wars.store import WarStore

    monkeypatch.setenv("EVEDM_DATA_DIR", str(tmp_path))
    db = Database(tmp_path / "evedm.sqlite3")
    seed_demo_wars(WarStore(db), graph)
    db.close()
    assert main(["wars", "list"]) == 0
    out = capsys.readouterr().out
    assert "#990001" in out and "[Tama, Kedama, Nourvukaiken]" in out
    assert main(["wars", "stats"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["total"] == 3
    assert main(["wars", "check", "--wars-config", str(tmp_path / "none.yaml")]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False
