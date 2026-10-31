"""Structure alerts: zKillboard's R2Z2 feed, classification (destroyed / combat on grid / firing), Discord posts,
the Alerts page and API with each main's closest characters, and setting an in-game destination (ALR-*, RTE-08)."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from common import AMARR, JITA, PERIMETER, add_character
from evedm.alerts import CORE_GROUPS, R2Z2Feed, seed_demo_alerts
from evedm.alerts import AlertService as Alerts
from evedm.config import WAYPOINT_SCOPE, AlertSettings, ConfigError, Settings, load_settings
from evedm.esi import EsiError
from evedm.sso import SsoError
from evedm.wars.config import WarsSettings
from evedm.wars.discord import NotifyError, WebhookInvalid
from evedm.wars.structures import Structure, StructureIndex
from evedm.web.app import create_app
from fake_eve import ALL_SCOPES, GROUPS, TYPES

pytestmark = pytest.mark.anyio
CORP, OTHER, SHOOTER = 98000001, 98000002, 98000003
FORT, FORT2, REFINERY, TOWER = 1_040_000_000_001, 1_040_000_000_002, 1_040_000_000_003, 1_040_000_000_004
ASTRAHUS, ATHANOR, ANSIBLEX, RIFTER, LOKI = 35832, 35835, 35841, 587, 29990
P_FORT = (1.0e12, 2.0e10, -3.0e11)
P_FORT2 = (1.0e12 + 5.0e9, 2.0e10, -3.0e11)  # 5 million km away: another grid
P_REFINERY = (4.0e11, 0.0, 0.0)


def near(p: tuple[float, float, float], km: float) -> dict[str, float]:
    return {"x": p[0] + km * 1000, "y": p[1], "z": p[2]}


# --------------------------------------------------------------------------- fakes
class FakeEsi:
    """The ESI calls the alert service makes: types, groups, killmails by id/hash, names."""

    def __init__(self) -> None:
        self.killmails: dict[tuple[int, str], dict] = {}
        self.names = {CORP: "Fort Owners", OTHER: "Victims Inc", SHOOTER: "Shooters"}
        self.calls: list[str] = []
        self.fail = False

    async def get(self, path: str) -> dict:
        self.calls.append(path)
        if self.fail:
            raise EsiError(503, "down")
        parts = path.strip("/").split("/")
        if parts[:2] == ["universe", "types"] and int(parts[2]) in TYPES:
            name, group = TYPES[int(parts[2])]
            return {"type_id": int(parts[2]), "name": name, "group_id": group}
        if parts[:2] == ["universe", "groups"] and int(parts[2]) in GROUPS:
            name, cat = GROUPS[int(parts[2])]
            return {"group_id": int(parts[2]), "name": name, "category_id": cat}
        if parts[0] == "killmails" and (int(parts[1]), parts[2]) in self.killmails:
            return self.killmails[(int(parts[1]), parts[2])]
        raise EsiError(404, "not found")

    async def names_categories(self, ids: list[int]) -> dict[int, tuple[str, str]]:
        if self.fail:
            raise EsiError(503, "down")
        return {i: (self.names.get(i, f"Corp {i}"), "corporation") for i in ids}


class FakeR2Z2:
    """sequence.json + {n}.json with per-file status overrides; records every request."""

    def __init__(self, pointer: int = 100) -> None:
        self.pointer = pointer
        self.files: dict[int, dict] = {}
        self.status: dict[int, int] = {}
        self.pointer_status = 200
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = request.url.path.rsplit("/", 1)[1]
        if name == "sequence.json":
            if self.pointer_status != 200:
                return httpx.Response(self.pointer_status)
            return httpx.Response(200, json={"sequence": self.pointer})
        n = int(name.removesuffix(".json"))
        if n in self.status:
            return httpx.Response(self.status[n])
        if n in self.files:
            return httpx.Response(200, json=self.files[n])
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[1] for r in self.requests]


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.error: Exception | None = None

    async def send(self, payload: dict) -> dict:
        if self.error is not None:
            raise self.error
        self.sent.append(payload)
        return {"id": str(len(self.sent))}


def package(
    kid: int,
    victim_type: int,
    system: int = JITA,
    corp: int = OTHER,
    pos: dict | None = None,
    attackers: list[dict] | None = None,
    body: bool = True,
) -> dict:
    victim = {"ship_type_id": victim_type, "corporation_id": corp, "character_id": 90000001}
    if pos is not None:
        victim["position"] = pos
    km = {
        "killmail_id": kid,
        "killmail_time": "2026-10-31T12:00:00Z",
        "solar_system_id": system,
        "victim": victim,
        "attackers": attackers or [{"ship_type_id": RIFTER, "corporation_id": SHOOTER, "final_blow": True}],
    }
    pkg = {"killmail_id": kid, "hash": f"h{kid}", "zkb": {"hash": f"h{kid}", "totalValue": 1.5e9}}
    return pkg | {"esi": km} if body else pkg | {"_km": km}


@pytest.fixture
def index(graph) -> StructureIndex:
    idx = StructureIndex(graph, "", "test")
    idx.set_public(
        {
            CORP: [
                Structure(FORT, "Jita - Fort", ASTRAHUS, JITA, CORP, P_FORT),
                Structure(FORT2, "Jita - Second Fort", ASTRAHUS, JITA, CORP, P_FORT2),
                Structure(REFINERY, "Jita - Mill", ATHANOR, JITA, CORP, P_REFINERY),
                Structure(TOWER, "Amarr - Tower", ASTRAHUS, AMARR, CORP, None),  # no position known
            ]
        }
    )
    return idx


@pytest.fixture
async def make(db, graph, index):
    made: list[Alerts] = []

    def build(feed: FakeR2Z2 | None = None, notifier=None, esi: FakeEsi | None = None, **kw) -> Alerts:
        discord = kw.pop("discord", {})
        s = AlertSettings(**kw)
        for k, v in discord.items():
            setattr(s.discord, k, v)
        svc = Alerts(
            s,
            db,
            graph,
            esi or FakeEsi(),  # type: ignore[arg-type]
            structure_index=index,
            notifier=notifier,
            page_url="https://evedm.example/alerts",
            transport=(feed or FakeR2Z2()).transport,
        )
        svc.feed.delay_s = 0
        made.append(svc)
        return svc

    yield build
    for svc in made:
        await svc.feed.close()


def alerts(db) -> list:
    return db.query("SELECT * FROM structure_alerts ORDER BY alert_id")


# --------------------------------------------------------------------------- R2Z2 feed
def feed_for(db, fake: FakeR2Z2, **kw) -> R2Z2Feed:
    return R2Z2Feed(
        db, "https://r2z2.test/ephemeral/", "EVE-Distance-Meter/test", delay_s=0, transport=fake.transport, **kw
    )


async def test_feed_starts_at_the_pointer_resumes_and_sends_a_user_agent(db):
    fake = FakeR2Z2(pointer=100)
    fake.files = {n: {"killmail_id": n} for n in (100, 101, 102)}
    feed = feed_for(db, fake)
    assert [p["killmail_id"] for p in await feed.poll()] == [100, 101, 102]
    assert db.get_meta("alerts.sequence") == "103"
    assert all(r.headers["user-agent"] == "EVE-Distance-Meter/test" for r in fake.requests)
    assert fake.requests[0].url.path == "/ephemeral/sequence.json"  # no double slash from the trailing one
    fake.files[103] = {"killmail_id": 103}
    fake.requests.clear()
    assert [p["killmail_id"] for p in await feed.poll()] == [103]
    assert fake.paths() == ["103.json", "104.json", "sequence.json"]  # resumed from the stored position
    assert feed.stats["read"] == 4
    await feed.close()
    # a new process continues from the stored position too
    again = feed_for(db, fake)
    fake.files[104] = {"killmail_id": 104}
    assert [p["killmail_id"] for p in await again.poll()] == [104]
    await again.close()


async def test_feed_skips_gaps_below_the_pointer_and_stops_when_caught_up(db):
    fake = FakeR2Z2(pointer=100)
    fake.files = {100: {"killmail_id": 100}, 102: {"killmail_id": 102}}
    feed = feed_for(db, fake)
    db.set_meta("alerts.sequence", "100")
    db.set_meta("alerts.sequence_at", str(time.time()))
    fake.pointer = 103  # 101 never appears: skipped because newer files exist
    assert [p["killmail_id"] for p in await feed.poll()] == [100, 102]
    assert feed.stats["gaps"] == 1 and db.get_meta("alerts.sequence") == "103"
    assert await feed.poll() == []  # caught up: 103 is not there yet and the pointer is not ahead
    assert db.get_meta("alerts.sequence") == "103"
    await feed.close()


async def test_feed_limits_each_poll_and_backs_off_when_refused(db):
    fake = FakeR2Z2(pointer=1)
    fake.files = {n: {"killmail_id": n} for n in range(1, 11)}
    feed = feed_for(db, fake, max_per_poll=4)
    assert len(await feed.poll()) == 4 and len(await feed.poll()) == 4 and len(await feed.poll()) == 2
    fake.status[11] = 429
    fake.files[11] = {"killmail_id": 11}
    assert await feed.poll() == []
    assert feed.stats["blocked"] == 1 and feed.blocked_until > time.time() + 500
    fake.requests.clear()
    assert await feed.poll() == [] and fake.requests == []  # paused: no request at all
    feed.blocked_until = 0
    fake.status.clear()
    assert [p["killmail_id"] for p in await feed.poll()] == [11]  # the refused file is read after the pause
    await feed.close()


async def test_feed_restarts_at_the_pointer_after_a_long_stop_and_survives_errors(db):
    fake = FakeR2Z2(pointer=900)
    fake.files = {900: {"killmail_id": 900}, 5: {"killmail_id": 5}}
    db.set_meta("alerts.sequence", "5")
    db.set_meta("alerts.sequence_at", str(time.time() - 21 * 3600))  # older than R2Z2 keeps files
    feed = feed_for(db, fake)
    assert [p["killmail_id"] for p in await feed.poll()] == [900]
    fake.status[901] = 500
    assert await feed.poll() == [] and feed.stats["errors"] == 1
    fake.status.clear()
    fake.pointer_status = 403
    db.set_meta("alerts.sequence", "")  # no position: needs the pointer, which is refused
    assert await feed.poll() == [] and feed.stats["blocked"] == 1
    await feed.close()

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    down = R2Z2Feed(db, "https://r2z2.test/ephemeral", "ua", delay_s=0, transport=httpx.MockTransport(broken))
    assert await down.poll() == [] and down.stats["errors"] == 1
    await down.close()


# --------------------------------------------------------------------------- classification
async def test_destroyed_citadel_is_matched_and_drops_a_core(make, db):
    svc = make()
    db.execute(
        "INSERT INTO known_structures(structure_id, name, owner_id, solar_system_id, type_id, status, first_seen,"
        " last_seen) VALUES(?,?,?,?,?,?,?,?)",
        (FORT2, "Jita - Second Fort", CORP, JITA, ASTRAHUS, "ok", 1, 1),
    )
    new = await svc.handle(package(1, ASTRAHUS, corp=CORP, pos=near(P_FORT2, 2)))
    assert [(a["kind"], a["structure_id"], a["core_drop"]) for a in new] == [("destroyed", FORT2, 1)]
    assert db.query("SELECT destroyed_at FROM known_structures WHERE structure_id=?", (FORT2,))[0][0] > 0
    row = alerts(db)[0]
    assert row["structure_name"] == "Jita - Second Fort" and row["isk"] == 1.5e9 and row["attackers"] == 1
    assert row["killmail_hash"] == "h1" and row["victim_corp_id"] == CORP
    # refinery and engineering complex drop cores too; a jump gate does not
    assert {"Citadel", "Engineering Complex", "Refinery"} == CORE_GROUPS
    assert (await svc.handle(package(2, ATHANOR, corp=CORP)))[0]["core_drop"] == 1
    assert (await svc.handle(package(3, ANSIBLEX, corp=CORP)))[0]["core_drop"] == 0
    # an unknown structure still raises the alert, without a name
    unknown = await svc.handle(package(4, ASTRAHUS, system=PERIMETER, corp=OTHER))
    assert unknown[0]["structure_id"] is None and unknown[0]["core_drop"] == 1
    assert svc.describe(alerts(db)[-1])["structure"]["name"] == "unknown Astrahus"


async def test_combat_on_grid_uses_positions(make, db):
    svc = make(grid_km=1000)
    new = await svc.handle(package(10, RIFTER, pos=near(P_FORT, 50)))
    assert [(a["kind"], a["structure_id"], a["distance_km"]) for a in new] == [("combat", FORT, 50.0)]
    assert await svc.handle(package(11, RIFTER, pos=near(P_FORT, 1500))) == []  # off grid
    assert await svc.handle(package(12, RIFTER)) == []  # no position on the killmail
    assert await svc.handle(package(13, RIFTER, system=AMARR, pos=near(P_FORT, 1))) == []  # Amarr's has no position
    small = make(grid_km=10)
    assert await small.handle(package(14, RIFTER, pos=near(P_FORT, 50))) == []


async def test_structures_on_the_attacker_list_are_firing(make, db):
    svc = make()
    att = [
        {"ship_type_id": ASTRAHUS, "corporation_id": CORP},
        {"ship_type_id": ATHANOR, "corporation_id": SHOOTER},  # nobody knows SHOOTER's refinery here
        {"ship_type_id": RIFTER, "corporation_id": SHOOTER, "final_blow": True},
    ]
    new = await svc.handle(package(20, LOKI, attackers=att, pos=near(P_FORT2, 3)))
    got = sorted((a["kind"], a["structure_id"] or 0, a["owner_id"]) for a in new)
    assert got == [("combat", FORT2, CORP), ("firing", 0, SHOOTER), ("firing", FORT2, CORP)]  # nearest Astrahus
    # a structure shooting another structure: destroyed and firing from one killmail
    both = await svc.handle(
        package(21, ATHANOR, corp=CORP, attackers=[{"ship_type_id": ASTRAHUS, "corporation_id": OTHER}])
    )
    assert sorted(a["kind"] for a in both) == ["destroyed", "firing"]


async def test_killmail_body_is_fetched_by_id_and_hash_and_duplicates_are_ignored(make, db):
    esi = FakeEsi()
    svc = make(esi=esi)
    pkg = package(30, ASTRAHUS, corp=CORP, body=False)
    esi.killmails[(30, "h30")] = pkg.pop("_km")
    assert len(await svc.handle(pkg)) == 1
    assert "/killmails/30/h30" in esi.calls
    assert await svc.handle(pkg) == []  # the same killmail twice: one alert
    assert len(alerts(db)) == 1 and svc.version == 1
    assert await svc.handle({"killmail_id": 31, "hash": "missing"}) == []  # ESI does not know it
    assert await svc.handle({"esi": {"killmail_id": 32, "solar_system_id": 1, "victim": {}}}) == []  # no such system
    assert await svc.handle({"zkb": {}}) == []  # nothing usable at all


async def test_type_lookups_are_cached_in_the_database(make, db):
    esi = FakeEsi()
    svc = make(esi=esi)
    assert await svc.is_structure(ASTRAHUS) and not await svc.is_structure(RIFTER)
    assert await svc.type_info(None) == (None, None, None)
    calls = len(esi.calls)
    assert await svc.is_structure(ASTRAHUS) and len(esi.calls) == calls  # memory cache
    offline = FakeEsi()
    offline.fail = True
    other = make(esi=offline)
    assert await other.type_info(ASTRAHUS) == (65, "Citadel", "Astrahus")  # from type_groups, no ESI call
    assert await other.type_info(LOKI) == (None, None, None)  # unknown and ESI down: not a structure for now
    assert await other.handle(package(40, LOKI, pos=near(P_FORT, 1))) != []  # combat still detected


# --------------------------------------------------------------------------- Discord
async def test_discord_posts_kinds_cooldown_and_mentions(make, db):
    note = FakeNotifier()
    svc = make(notifier=note, discord={"kinds": ["destroyed", "combat"], "mention_role_id": "777"})
    await svc.handle(package(50, RIFTER, pos=near(P_FORT, 10)))
    await svc.handle(package(51, RIFTER, pos=near(P_FORT, 20)))  # same structure within the cooldown
    await svc.handle(package(52, LOKI, attackers=[{"ship_type_id": ASTRAHUS, "corporation_id": OTHER}]))  # firing
    await svc.handle(package(53, ASTRAHUS, corp=CORP, pos=near(P_FORT, 0)))
    assert await svc.post() == 2
    assert [r["posted"] for r in alerts(db)] == [1, 2, 2, 1]  # 2 = deliberately not posted
    combat, destroyed = note.sent
    assert combat["embeds"][0]["title"].startswith("⚔ Combat 10 km from Jita - Fort") and "content" not in combat
    assert destroyed["embeds"][0]["title"] == "💥 Astrahus destroyed - quantum core drop"
    assert destroyed["content"] == "<@&777>" and destroyed["allowed_mentions"] == {"roles": ["777"]}
    fields = {f["name"]: f["value"] for f in destroyed["embeds"][0]["fields"]}
    assert fields["Closest characters"].startswith("[Open the Alerts page](https://evedm.example/alerts#alert-")
    assert destroyed["embeds"][0]["url"] == "https://zkillboard.com/kill/53/"
    assert await svc.post() == 0  # nothing left


async def test_discord_failures_keep_alerts_queued(make, db):
    note = FakeNotifier()
    svc = make(notifier=note)
    await svc.handle(package(60, ASTRAHUS, corp=CORP))
    note.error = NotifyError("timeout")
    assert await svc.post() == 0 and alerts(db)[0]["posted"] == 0
    note.error = WebhookInvalid("404")
    assert await svc.post() == 0 and "webhook invalid" in svc.state["last_error"]
    note.error = None
    assert await svc.post() == 1 and alerts(db)[0]["posted"] == 1
    assert await make(notifier=None).post() == 0  # Discord off


# --------------------------------------------------------------------------- names, purge, loop
async def test_names_describe_and_purge(make, db):
    svc = make(retention_days=1)
    await svc.handle(package(70, ASTRAHUS, corp=CORP, pos=near(P_FORT, 0)))
    await svc.resolve_names()
    a = svc.describe(alerts(db)[0])
    assert a["owner"] == {"id": CORP, "name": "Fort Owners", "zkill": f"https://zkillboard.com/corporation/{CORP}/"}
    assert a["structure"] == {"id": FORT, "name": "Jita - Fort", "type": "Astrahus", "known": True}
    assert a["system"]["name"] == "Jita" and a["zkill"] == "https://zkillboard.com/kill/70/"
    db.execute("UPDATE structure_alerts SET created_at=?", (time.time() - 2 * 86400,))
    assert svc.recent(hours=24) == [] and len(svc.recent(hours=72)) == 1
    assert svc.purge() == 1 and alerts(db) == []
    broken = FakeEsi()
    broken.fail = True
    await make(esi=broken).resolve_names()  # ESI down: nothing to do, no error


async def test_poll_once_reads_the_feed_and_posts(make, db):
    fake = FakeR2Z2(pointer=7)
    fake.files = {
        7: package(80, ASTRAHUS, corp=CORP),
        8: {"killmail_id": 81, "esi": "broken"},  # an odd file does not stop the feed
        9: package(82, RIFTER, pos=near(P_FORT, 5)),
    }
    note = FakeNotifier()
    svc = make(feed=fake, notifier=note)
    assert await svc.poll_once() == 2
    assert svc.state["last_kill"] == "2026-10-31T12:00:00Z" and len(note.sent) == 2
    assert svc.feed.stats["read"] == 3


@pytest.mark.alerts_loop
async def test_background_loop_starts_and_stops(make, db):
    fake = FakeR2Z2(pointer=1)
    fake.files = {1: package(90, ASTRAHUS, corp=CORP)}
    svc = make(feed=fake, poll_s=0.05)
    svc.start()
    for _ in range(100):
        if svc.state["polls"] >= 2:
            break
        await asyncio.sleep(0.02)
    assert svc.state["running"] and svc.state["alerts"] == 1
    await svc.stop()
    assert not svc.state["running"] and svc._task is None


def test_demo_alerts_seed_once(db, graph):
    assert seed_demo_alerts(db, graph) == 4 and seed_demo_alerts(db, graph) == 0


# --------------------------------------------------------------------------- settings
def serve_settings(**alerts) -> Settings:
    s = Settings()
    s.secret_key, s.demo = "k", True
    for k, v in alerts.items():
        if k.startswith("discord_"):
            setattr(s.alerts.discord, k[8:], v)
        else:
            setattr(s.alerts, k, v)
    return s


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"poll_s": 3}, "at least 6"),
        ({"grid_km": 0}, "grid_km"),
        ({"max_per_poll": 0}, "max_per_poll"),
        ({"discord_kinds": ["boom"]}, r"unknown \['boom'\]"),
        ({"discord_enabled": True}, "needs a webhook"),
    ],
)
def test_alert_settings_are_validated(values, message):
    with pytest.raises(ConfigError, match=message):
        serve_settings(**values).validate_for_serve()


def test_alert_settings_from_yaml_and_environment(monkeypatch, tmp_path):
    cfg = tmp_path / "evedm.yaml"
    cfg.write_text("alerts:\n  grid_km: 250\n  discord:\n    kinds: [destroyed]\n    combat_cooldown_min: 5\n")
    monkeypatch.setenv("EVEDM_ALERTS_DISCORD_WEBHOOK", "https://discord.test/hook")
    monkeypatch.setenv("EVEDM_ALERTS_DISCORD_ENABLED", "true")
    monkeypatch.setenv("EVEDM_ALERTS_DISCORD_MENTION_ROLE_ID", "123")
    monkeypatch.setenv("EVEDM_ALERTS_CONTACT", "ops@example.org")
    s = load_settings(cfg)
    assert s.alerts.grid_km == 250 and s.alerts.discord.kinds == ["destroyed"]
    assert s.alerts.discord.combat_cooldown_min == 5.0
    assert s.alerts.discord.enabled and s.alerts.discord.webhook_url == "https://discord.test/hook"
    assert s.alerts.discord.mention_role_id == "123" and s.alerts.contact == "ops@example.org"
    assert "discord.test" not in str(s.summary())  # the webhook is a secret
    monkeypatch.setenv("EVEDM_ALERTS_ENABLED", "false")
    assert load_settings(cfg).alerts.enabled is False
    assert WAYPOINT_SCOPE in Settings().eve.optional_scopes


def test_user_agent_names_the_contact(db, graph):
    s = AlertSettings(contact="ops@example.org")
    svc = Alerts(s, db, graph, FakeEsi())  # type: ignore[arg-type]
    assert svc.feed.http.headers["user-agent"].startswith("EVE-Distance-Meter/")
    assert svc.feed.http.headers["user-agent"].endswith("(structure alerts; ops@example.org)")


# --------------------------------------------------------------------------- web: demo
def demo(tmp_path, **alerts) -> TestClient:
    s = Settings()
    s.data_dir, s.secret_key, s.demo, s.web.auth = tmp_path / "d", "k", True, "none"
    for k, v in alerts.items():
        setattr(s.alerts, k, v)
    return TestClient(create_app(s, WarsSettings(enabled=True)))


def test_alerts_page_and_api_in_demo(tmp_path):
    with demo(tmp_path) as c:
        assert 'href="/alerts"' in c.get("/").text
        assert "alerts.js" in c.get("/alerts").text
        body = c.get("/api/alerts").json()
        groups = {g["structure"]["name"]: g for g in body["groups"]}
        assert set(groups) == {"Perimeter - Tollgate Market", "unknown Astrahus", "Amamake - Ember Post"}
        assert [g["state"] for g in body["groups"]] == ["combat", "destroyed", "firing"]  # newest first
        tollgate = groups["Perimeter - Tollgate Market"]
        assert tollgate["counts"] == {"destroyed": 0, "combat": 2, "firing": 0} and len(tollgate["alerts"]) == 2
        assert tollgate["system"]["space"] == "high" and tollgate["structure"]["known"]
        hek = groups["unknown Astrahus"]
        assert hek["core_drop"] and hek["system"]["name"] == "Hek"
        for g in body["groups"]:
            jumps = [x["jumps"] for x in g["closest"]]
            assert 1 <= len(jumps) <= 3 and jumps == sorted(jumps)
            assert all(x["can_set_route"] for x in g["closest"])  # demo characters
        assert body["newest_alert_id"] == max(a["alert_id"] for g in body["groups"] for a in g["alerts"])
        assert body["feed"]["running"] is False
        get = lambda **p: [g["system"]["name"] for g in c.get("/api/alerts", params=p).json()["groups"]]  # noqa: E731
        assert get(core_only=1) == ["Hek"]
        assert get(kinds="firing") == ["Amamake"] and get(kinds="combat,bogus") == ["Perimeter"]
        assert get(space="low") == ["Amamake"] and set(get(space="high")) == {"Perimeter", "Hek"}
        assert get(space="null") == [] and get(hours=0.05) == ["Perimeter"]
        assert c.get("/api/health").json()["alerts"] == {"running": False, "polls": 0}


def test_alerts_off(tmp_path):
    with demo(tmp_path, enabled=False) as c:
        assert c.get("/alerts").status_code == 404 and c.get("/api/alerts").status_code == 404
        assert 'href="/alerts"' not in c.get("/").text
        assert c.get("/api/health").json()["alerts"] is None


def test_set_destination_in_demo_and_bad_input(tmp_path):
    H = {"X-EVEDM": "1"}
    with demo(tmp_path) as c:
        cid = c.get("/api/characters").json()[0]["character_id"]
        url = f"/api/characters/{cid}/destination"
        r = c.post(url, json={"destination_id": JITA}, headers=H)
        assert r.json() == {"ok": True, "simulated": True, "destination": "Jita"}
        assert c.post(url, json={"destination_id": FORT}, headers=H).json()["destination"] == str(FORT)
        assert c.post(url, json={"destination_id": JITA}).status_code == 403  # CSRF header missing
        for bad in ("abc", None, 12345, -1):
            assert c.post(url, json={"destination_id": bad}, headers=H).status_code == 400, bad
        assert c.post(url, content=b"{", headers=H).status_code == 400
        assert c.post("/api/characters/1/destination", json={"destination_id": JITA}, headers=H).status_code == 404
        rows = c.get("/api/rank", params={"target": "Tama", "refresh": 0}).json()["rows"]
        assert rows and all(r["can_set_route"] for r in rows)


# --------------------------------------------------------------------------- web: signed-in mains
def test_destination_requires_the_scope_and_reports_esi_errors(tmp_path, monkeypatch):
    from test_accounts_web import SCOPES, as_user, link, settings

    A, B, ALT = 2112000001, 2112000002, 2112000004
    H = {"X-EVEDM": "1"}
    with TestClient(create_app(settings(tmp_path), WarsSettings(enabled=False))) as c:
        svc = c.app.state.svc
        svc.db.upsert_user(A, "Alice")
        svc.db.upsert_user(B, "Bob")
        link(svc, A, A, "Alice", JITA)
        link(svc, A, ALT, "Alice Alt", AMARR)
        svc.db.upsert_character(ALT, "Alice Alt", "OK", [*SCOPES, WAYPOINT_SCOPE], owner_id=A)
        sent: list[tuple] = []

        async def token(cid: int, owner: int = 0) -> str:
            return f"token-{cid}-{owner}"

        async def post_auth(path: str, tok: str, params: dict) -> int:
            sent.append((path, tok, params))
            return 204

        monkeypatch.setattr(svc.chars, "access_token", token)
        monkeypatch.setattr(svc.esi, "post_auth", post_auth)
        assert as_user(c, None).post(f"/api/characters/{A}/destination", json={}, headers=H).status_code == 401
        c = as_user(c, A, "Alice")
        r = c.post(f"/api/characters/{A}/destination", json={"destination_id": JITA}, headers=H)
        assert r.status_code == 400 and "re-authorize" in r.json()["detail"]
        r = c.post(f"/api/characters/{ALT}/destination", json={"destination_id": PERIMETER}, headers=H)
        assert r.json() == {"ok": True, "destination": "Perimeter"}
        params = {"destination_id": PERIMETER, "add_to_beginning": "false", "clear_other_waypoints": "true"}
        assert sent == [("/ui/autopilot/waypoint", f"token-{ALT}-{A}", params)]
        rows = c.get("/api/rank", params={"target": "Jita", "refresh": 0}).json()["rows"]
        assert {r["character_id"]: r["can_set_route"] for r in rows} == {A: False, ALT: True}
        # another main cannot steer Alice's characters
        assert (
            as_user(c, B, "Bob")
            .post(f"/api/characters/{ALT}/destination", json={"destination_id": JITA}, headers=H)
            .status_code
            == 404
        )
        c = as_user(c, A, "Alice")

        async def offline(path: str, tok: str, params: dict) -> int:
            raise EsiError(404, "Character is not online")

        monkeypatch.setattr(svc.esi, "post_auth", offline)
        r = c.post(f"/api/characters/{ALT}/destination", json={"destination_id": JITA}, headers=H)
        assert r.status_code == 502 and "online" in r.json()["detail"]

        async def expired(cid: int, owner: int = 0) -> str:
            raise SsoError("invalid_grant", "revoked")

        monkeypatch.setattr(svc.chars, "access_token", expired)
        assert c.post(f"/api/characters/{ALT}/destination", json={"destination_id": JITA}, headers=H).status_code == 409
        # the alerts API lists Alice's characters only, closest first
        svc.alerts.db.execute(
            "INSERT INTO structure_alerts(kind, killmail_id, solar_system_id, structure_type_id, owner_id, core_drop,"
            " created_at) VALUES('destroyed', 1, ?, ?, ?, 1, ?)",
            (PERIMETER, ASTRAHUS, CORP, time.time()),
        )
        g = c.get("/api/alerts", params={"refresh": 0}).json()["groups"][0]
        assert [x["name"] for x in g["closest"]] == ["Alice", "Alice Alt"]  # Jita is next to Perimeter, Amarr far
        assert g["closest"][0]["jumps"] == 1 < g["closest"][1]["jumps"] and g["closest"][0]["system"] == "Jita"
        assert g["closest"][0]["can_set_route"] is False and g["closest"][1]["can_set_route"] is True


async def test_esi_post_auth_sets_the_waypoint_in_the_fake(services, fake_app):
    st = fake_app.state.fake
    cid = 2100000002  # online in the fake
    await add_character(services, cid, scopes=[*ALL_SCOPES, WAYPOINT_SCOPE])
    token = await services["chars"].access_token(cid)
    params = {"destination_id": JITA, "add_to_beginning": "false", "clear_other_waypoints": "true"}
    assert await services["esi"].post_auth("/ui/autopilot/waypoint", token, params) == 204
    assert st.waypoints == [
        {"character_id": cid, "destination_id": JITA, "add_to_beginning": False, "clear_other_waypoints": True}
    ]
    offline = 2100000001
    await add_character(services, offline, scopes=[*ALL_SCOPES, WAYPOINT_SCOPE])
    with pytest.raises(EsiError) as err:
        await services["esi"].post_auth("/ui/autopilot/waypoint", await services["chars"].access_token(offline), params)
    assert err.value.status == 404
    await add_character(services, 2100000003)  # no waypoint scope
    with pytest.raises(EsiError) as err:
        await services["esi"].post_auth(
            "/ui/autopilot/waypoint", await services["chars"].access_token(2100000003), params
        )
    assert err.value.status == 403
