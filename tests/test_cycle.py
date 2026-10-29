"""End-to-end cycle tests: fake ESI (real response shapes) -> store -> Discord payloads."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from app import main as bot
from app.discord_notifier import DiscordNotifier
from app.esi import EsiClient, EsiError
from app.models import NotifyError
from app.source import EsiWarSource
from app.store import MAX_POST_ATTEMPTS, WarStore
from fake_esi import FakeEsi, make_war

FIRST_ID = 763600


class CapturingNotifier(DiscordNotifier):
    def __init__(self, fail: bool = False):
        super().__init__("https://discord.invalid/webhook", 0)
        self.fail = fail
        self.payloads = []

    async def send(self, payload, attachments=None):
        if self.fail:
            raise NotifyError("Discord error 400: bad embed")
        self.payloads.append(payload)
        return {"id": f"msg{len(self.payloads)}", "attachments": []}

    def war_ids(self):
        return [int(e["footer"]["text"].split("#")[1]) for p in self.payloads for e in p["embeds"]]


def hourly_wars(hours_ago):
    """One war per entry, IDs ascending as declarations get newer."""
    now = datetime.now(timezone.utc)
    ordered = sorted(hours_ago, reverse=True)
    return {FIRST_ID + i: make_war(FIRST_ID + i, now - timedelta(hours=h)) for i, h in enumerate(ordered)}


@pytest_asyncio.fixture
async def store(tmp_path):
    st = WarStore(tmp_path / "wars.db")
    await st.connect()
    yield st
    await st.close()


async def cycle(s, store, esi, notifier):
    await bot.run_cycle(s, {}, store, notifier, esi, EsiWarSource(esi))


SETTINGS = {"bootstrap_hours": 24, "batch_embeds": 5, "max_new_per_cycle": 50, "report_enabled": True}


@pytest.mark.asyncio
async def test_clean_db_posts_last_24h_once(store):
    esi = FakeEsi(hourly_wars([1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34, 37, 40]))
    notifier = CapturingNotifier()

    await cycle(SETTINGS, store, esi, notifier)
    assert notifier.war_ids() == list(range(FIRST_ID + 6, FIRST_ID + 14))  # the 8 wars < 24h old
    assert len(notifier.payloads) == 2                                      # batches of 5 + 3

    await cycle(SETTINGS, store, esi, notifier)
    assert len(notifier.payloads) == 2, "wars must not be posted twice"
    assert (await store.stats()) == {"notified": 8, "stale": 6}


@pytest.mark.asyncio
async def test_new_war_after_bootstrap_is_posted(store):
    wars = hourly_wars([30, 2])
    esi = FakeEsi(wars)
    notifier = CapturingNotifier()
    await cycle(SETTINGS, store, esi, notifier)
    assert notifier.war_ids() == [FIRST_ID + 1]

    new_id = FIRST_ID + 2
    wars[new_id] = make_war(new_id, datetime.now(timezone.utc), mutual=True)
    await cycle(SETTINGS, store, esi, notifier)
    assert notifier.war_ids()[-1] == new_id
    assert notifier.payloads[-1]["embeds"][0]["description"] == "Mutual war"


@pytest.mark.asyncio
async def test_bootstrap_hours_zero_starts_from_now(store):
    esi = FakeEsi(hourly_wars([1, 2, 3]))
    notifier = CapturingNotifier()
    await cycle({**SETTINGS, "bootstrap_hours": 0}, store, esi, notifier)
    assert notifier.payloads == []
    assert await store.get_meta("watermark_war_id") == str(FIRST_ID + 2)


@pytest.mark.asyncio
async def test_heals_test_data_watermark(store):
    # The state shipped in the repo's data/wars.db: fake wars far above real ESI IDs
    await store.set_meta("bootstrapped", "true")
    await store.set_meta("watermark_war_id", "1000002")
    await store.mark_marker(1000001, "pending")
    await store.mark_marker(1000002, "notified")

    esi = FakeEsi(hourly_wars([30, 5, 1]))
    notifier = CapturingNotifier()
    await cycle(SETTINGS, store, esi, notifier)

    assert notifier.war_ids() == [FIRST_ID + 1, FIRST_ID + 2]
    assert not await store.has(1000001) and not await store.has(1000002)
    assert 1000001 not in esi.war_calls


@pytest.mark.asyncio
async def test_statuses_filter(store):
    wars = hourly_wars([3, 2, 1])
    wars[FIRST_ID + 1]["mutual"] = True
    esi = FakeEsi(wars)
    notifier = CapturingNotifier()
    await cycle({**SETTINGS, "statuses": ["mutual"]}, store, esi, notifier)
    assert notifier.war_ids() == [FIRST_ID + 1]
    assert (await store.stats()) == {"filtered": 2, "notified": 1}


@pytest.mark.asyncio
async def test_failed_post_is_retried_then_parked(store):
    esi = FakeEsi(hourly_wars([1]))
    notifier = CapturingNotifier(fail=True)
    for _ in range(MAX_POST_ATTEMPTS):
        await cycle(SETTINGS, store, esi, notifier)
    assert (await store.stats()) == {"failed": 1}


@pytest.mark.asyncio
async def test_transient_esi_error_does_not_skip_war(store):
    esi = FakeEsi(hourly_wars([2, 1]))
    flaky = FIRST_ID + 1
    real_war = esi.war
    calls = {"n": 0}

    async def war(war_id):
        if war_id == flaky:
            calls["n"] += 1
            if calls["n"] in (2, 3):  # call 1 is the bootstrap header read
                raise EsiError("HTTP 503", 503)
        return await real_war(war_id)

    esi.war = war
    notifier = CapturingNotifier()
    await cycle(SETTINGS, store, esi, notifier)
    assert notifier.war_ids() == [FIRST_ID]
    assert flaky in await store.pending_ids(), "a transient error must not mark the war skipped"
    await cycle(SETTINGS, store, esi, notifier)
    await cycle(SETTINGS, store, esi, notifier)
    assert notifier.war_ids() == [FIRST_ID, flaky]


@pytest.mark.asyncio
async def test_esi_names_posts_bare_array():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["compat"] = request.headers.get("X-Compatibility-Date")
        return httpx.Response(200, json=[{"category": "alliance", "id": 99010015, "name": "BLACKFLAG."}])

    client = EsiClient("https://esi.test", "ua", compatibility_date="2025-08-26")
    client._client = httpx.AsyncClient(base_url="https://esi.test", transport=httpx.MockTransport(handler),
                                       headers={"X-Compatibility-Date": "2025-08-26"})
    try:
        assert await client.names([99010015, None]) == {99010015: ("BLACKFLAG.", "alliance")}
    finally:
        await client.close()
    assert seen["body"] == [99010015]
    assert seen["compat"] == "2025-08-26"


@pytest.mark.asyncio
async def test_notifier_waits_for_message(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["wait"] = request.url.params.get("wait")
        return httpx.Response(200, json={"id": "123", "attachments": []})

    real = httpx.AsyncClient
    monkeypatch.setattr("app.discord_notifier.httpx.AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    msg = await DiscordNotifier("https://discord.test/api/webhooks/1/x", 0).send({"embeds": []})
    assert msg["id"] == "123"
    assert seen["wait"] == "true"


@pytest.mark.asyncio
async def test_ids_not_in_date_order_and_reissued_wars(store):
    # Real ESI pattern: old wars re-issued under new IDs sit between fresh ones
    now = datetime.now(timezone.utc)
    ages = [200, 5, 4, 90, 80, 3, 2]          # hours ago, by ascending war ID
    wars = {FIRST_ID + i: make_war(FIRST_ID + i, now - timedelta(hours=h)) for i, h in enumerate(ages)}
    esi = FakeEsi(wars)
    notifier = CapturingNotifier()
    await cycle(SETTINGS, store, esi, notifier)
    assert notifier.war_ids() == [FIRST_ID + 1, FIRST_ID + 2, FIRST_ID + 5, FIRST_ID + 6]

    # live: a re-issued months-old war is stored as stale, a fresh one is posted
    wars[FIRST_ID + 7] = make_war(FIRST_ID + 7, now - timedelta(days=60), retracted="2026-10-28T02:55:00Z")
    wars[FIRST_ID + 8] = make_war(FIRST_ID + 8, now)
    await cycle(SETTINGS, store, esi, notifier)
    assert notifier.war_ids()[-1] == FIRST_ID + 8
    assert FIRST_ID + 7 not in notifier.war_ids()


def test_credentials_nested_layout():
    from app.config import normalize_credentials
    creds = normalize_credentials({"discord": {"webhook_url": "https://discord.com/api/webhooks/1/x",
                                               "mention_role_id": "OPTIONAL_ROLE_ID"},
                                   "esi": {"contact": "me@example.com"}})
    assert creds["webhook_url"] == "https://discord.com/api/webhooks/1/x"
    assert creds["contact"] == "me@example.com"
    assert creds["mention_role_id"] == ""
    assert normalize_credentials({"webhook_url": "flat"})["webhook_url"] == "flat"
