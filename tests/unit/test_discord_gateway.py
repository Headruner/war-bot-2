"""The bot's gateway connection: identify with the presence, heartbeat, READY state, reconnects, rejected token."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from evedm.discord_gateway import DiscordPresence, invite_url

pytestmark = [pytest.mark.anyio, pytest.mark.discord_gateway]

TOKEN = ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4", "abcdef", "x" * 27))


class FakeGateway:
    """Stands in for websockets' ``connect``: says hello, then plays the frames it is given."""

    def __init__(self, frames: list[dict], *, close: Exception | None = None, interval_ms: int = 20) -> None:
        self.frames, self.close, self.sent, self.opened = frames, close, [], 0
        self.interval_ms = interval_ms
        self.headers: dict = {}

    def __call__(self, url, **kw):
        self.url, self.headers = url, kw.get("additional_headers", {})
        self.opened += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        return json.dumps({"op": 10, "d": {"heartbeat_interval": self.interval_ms}})

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def ws_close(self, *a):
        return None

    close_ = ws_close

    def __aiter__(self):
        async def gen():
            for f in self.frames:
                await asyncio.sleep(0.01)
                yield json.dumps(f)
            if self.close is not None:
                raise self.close
            await asyncio.sleep(3600)

        return gen()


def ready(guilds: int = 2, username: str = "evedm") -> dict:
    return {
        "op": 0,
        "s": 1,
        "t": "READY",
        "d": {
            "user": {"id": "1234567890123456", "username": username},
            "guilds": [{"id": str(i)} for i in range(guilds)],
        },
    }


def guilds_api(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path.endswith("/users/@me/guilds"):
        return httpx.Response(200, json=[{"id": "111", "name": "Alliance HQ"}, {"id": "222", "name": "Test server"}])
    return httpx.Response(404)


API = httpx.MockTransport(guilds_api)
NO_SERVERS = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))


async def wait_for(cond, seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_the_bot_identifies_with_its_game_comes_online_and_beats() -> None:
    gw = FakeGateway([ready(guilds=2)])
    bot = DiscordPresence(TOKEN, game="Watching structures", guild_id="111", connect=gw, transport=API)
    bot.start()
    await wait_for(lambda: bot.state["online"])
    identify = next(m for m in gw.sent if m["op"] == 2)["d"]
    assert identify["token"] == TOKEN and identify["intents"] == 0
    assert identify["presence"]["status"] == "online"
    assert identify["presence"]["activities"] == [{"name": "Watching structures", "type": 0}]
    await wait_for(lambda: bot.state["server_list"])
    assert bot.state["user"] == "evedm" and bot.state["servers"] == 2
    assert bot.state["server_list"] == [{"id": "111", "name": "Alliance HQ"}, {"id": "222", "name": "Test server"}]
    assert bot.state["allowed_guild"] == "111"  # the Settings page marks the other one
    assert bot.state["invite_url"] == invite_url("1234567890123456") and "scope=bot" in bot.state["invite_url"]
    await wait_for(lambda: any(m["op"] == 1 for m in gw.sent))  # heartbeats go out
    assert gw.headers["User-Agent"].startswith("DiscordBot")
    await bot.stop()
    assert not bot.state["online"] and not bot.state["running"]


async def test_no_game_means_no_activity_and_a_gateway_reconnect_request_starts_over() -> None:
    gw = FakeGateway([ready(0), {"op": 7, "d": None}])
    bot = DiscordPresence(TOKEN, connect=gw, backoff_max_s=0.01, transport=NO_SERVERS)
    bot.start()
    await wait_for(lambda: gw.opened >= 2)
    assert next(m for m in gw.sent if m["op"] == 2)["d"]["presence"]["activities"] == []
    assert bot.state["servers"] == 0 and bot.state["connects"] >= 1
    await bot.stop()


async def test_a_rejected_token_is_reported_and_not_retried_at_once() -> None:
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    gw = FakeGateway([], close=ConnectionClosedError(Close(4004, "Authentication failed"), None))
    bot = DiscordPresence(TOKEN, connect=gw, fatal_wait_s=3600, transport=API)
    bot.start()
    await wait_for(lambda: bot.state["last_error"])
    assert "rejected the bot token" in bot.state["last_error"] and TOKEN not in bot.state["last_error"]
    await asyncio.sleep(0.1)
    assert gw.opened == 1 and not bot.state["online"]
    await bot.stop()


async def test_a_failed_server_list_does_not_stop_the_bot_coming_online() -> None:
    down = httpx.MockTransport(lambda request: httpx.Response(500))
    bot = DiscordPresence(TOKEN, connect=FakeGateway([ready(3)]), transport=down)
    bot.start()
    await wait_for(lambda: bot.state["online"])
    assert bot.state["servers"] == 3 and bot.state["server_list"] == []  # the count from READY stays
    await bot.stop()


async def test_a_connection_error_is_retried_with_back_off() -> None:
    calls = []

    def refuse(url, **kw):
        calls.append(1)
        raise OSError("network unreachable")

    bot = DiscordPresence(TOKEN, connect=refuse, backoff_max_s=0.01)
    bot.start()
    await wait_for(lambda: len(calls) >= 2)  # it keeps trying
    assert "network unreachable" in bot.state["last_error"] and not bot.state["online"]
    await bot.stop()


async def test_the_bot_name_is_set_once_when_it_differs() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return guilds_api(request)
        seen.append(request)
        return httpx.Response(200, json={"username": "Distance Meter"})

    gw = FakeGateway([ready(1, username="old-name")])
    bot = DiscordPresence(TOKEN, name="Distance Meter", connect=gw, transport=httpx.MockTransport(handler))
    bot.start()
    await wait_for(lambda: bot.state["user"] == "Distance Meter")
    assert len(seen) == 1 and seen[0].method == "PATCH" and seen[0].url.path.endswith("/users/@me")
    assert json.loads(seen[0].content) == {"username": "Distance Meter"}
    assert seen[0].headers["Authorization"] == f"Bot {TOKEN}"
    await bot.stop()
    same = DiscordPresence(TOKEN, name="evedm", connect=FakeGateway([ready()]), transport=httpx.MockTransport(handler))
    same.start()
    await wait_for(lambda: same.state["online"])
    assert len(seen) == 1  # the name already matches: no request
    await same.stop()
