"""Keeps the Discord bot online: a gateway connection that sends the token, a heartbeat and the presence.

Posting needs no gateway (REST only, see wars/discord.py), but without one the bot shows as offline and nobody can
tell it is running. This connection asks for no intents, so no privileged intent has to be switched on in the
Developer Portal; it reads nothing from the servers, it only makes the bot appear online with ``bot_game`` as its
"Playing ..." text. The bot still has to be invited to the server (the Settings page shows the invite link).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from typing import Any

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .crypto import redact
from .wars.discord import API_BASE, USER_AGENT

log = logging.getLogger("evedm.discord_gateway")

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# View Channel, Send Messages, Embed Links, Mention Everyone (to ping a role that is not mentionable)
INVITE_PERMISSIONS = 1024 + 2048 + 16384 + 131072
# 4004 token rejected, 4010-4014 bad shard / API version / intents: retrying at once cannot help
FATAL_CLOSE = {
    4004: "Discord rejected the bot token (Developer Portal > Bot > Reset Token)",
    4010: "Discord rejected the shard settings",
    4011: "Discord refused the connection: sharding is required",
    4012: "Discord refused the API version",
    4013: "Discord rejected the intents",
    4014: "Discord refused the intents: one needs switching on in the Developer Portal",
}
FATAL_WAIT_S = 600.0


def invite_url(client_id: str | int) -> str:
    return f"https://discord.com/oauth2/authorize?client_id={client_id}&scope=bot&permissions={INVITE_PERMISSIONS}"


class DiscordPresence:
    """Gateway connection of the bot with automatic reconnects (capped exponential back-off)."""

    def __init__(
        self,
        token: str,
        *,
        game: str = "",
        name: str = "",
        url: str = GATEWAY_URL,
        api_base: str = API_BASE,
        connect: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_max_s: float = 300.0,
        fatal_wait_s: float = FATAL_WAIT_S,
    ):
        self.token = token
        self.game = game
        self.name = name
        self.url = url
        self.api_base = api_base.rstrip("/")
        self._connect = connect or ws_connect
        self._transport = transport
        self.backoff_max_s = backoff_max_s
        self.fatal_wait_s = fatal_wait_s
        self.state: dict[str, Any] = {
            "running": False,
            "online": False,
            "user": None,
            "user_id": None,
            "servers": None,
            "invite_url": None,
            "last_error": None,
            "connects": 0,
        }
        self._seq: int | None = None
        self._ack = True
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self.state["running"] = True
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.state.update(running=False, online=False)

    # -- connection --------------------------------------------------------------------------------------
    async def _run(self) -> None:
        first = min(5.0, self.backoff_max_s)
        delay = first
        while not self._stop.is_set():
            wait = delay
            try:
                await self._session()
                delay = first  # Discord asked for a new connection
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                code = getattr(getattr(exc, "rcvd", None), "code", None)
                if code in FATAL_CLOSE:
                    self.state["last_error"] = FATAL_CLOSE[code]
                    log.error("Discord gateway: %s", FATAL_CLOSE[code])
                    wait = self.fatal_wait_s
                else:
                    self.state["last_error"] = f"connection closed ({code})"
                    log.warning("Discord gateway closed (%s): reconnecting in %.0f s", code, wait)
                delay = min(delay * 2, self.backoff_max_s)
            except (OSError, TimeoutError, WebSocketException, ValueError) as exc:
                self.state["last_error"] = redact(f"{type(exc).__name__}: {exc}")
                log.warning("Discord gateway: %s (retry in %.0f s)", self.state["last_error"], wait)
                delay = min(delay * 2, self.backoff_max_s)
            finally:
                self.state["online"] = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=wait)

    def _identify(self) -> dict[str, Any]:
        activities = [{"name": self.game, "type": 0}] if self.game else []
        return {
            "op": 2,
            "d": {
                "token": self.token,
                "intents": 0,
                "properties": {"os": "linux", "browser": "evedm", "device": "evedm"},
                "presence": {"status": "online", "afk": False, "since": None, "activities": activities},
            },
        }

    async def _session(self) -> None:
        async with self._connect(
            self.url, additional_headers={"User-Agent": USER_AGENT}, open_timeout=15, max_size=4 * 1024 * 1024
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if hello.get("op") != 10:
                raise ValueError("the gateway did not say hello")
            interval = float(hello["d"]["heartbeat_interval"]) / 1000
            self._seq, self._ack = None, True
            beat = asyncio.ensure_future(self._heartbeat(ws, interval))
            try:
                await ws.send(json.dumps(self._identify()))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("s") is not None:
                        self._seq = msg["s"]
                    op = msg.get("op")
                    if op == 0 and msg.get("t") == "READY":
                        await self._ready(msg["d"])
                    elif op == 11:
                        self._ack = True
                    elif op == 1:
                        await ws.send(json.dumps({"op": 1, "d": self._seq}))
                    elif op in (7, 9):  # reconnect / invalid session: start over
                        return
            finally:
                beat.cancel()

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        await asyncio.sleep(interval * random.random())  # noqa: S311 - the jitter Discord asks for
        while True:
            if not self._ack:  # the last beat was never answered: the connection is dead
                await ws.close(4000, "no heartbeat ack")
                return
            self._ack = False
            await ws.send(json.dumps({"op": 1, "d": self._seq}))
            await asyncio.sleep(interval)

    async def _ready(self, data: dict[str, Any]) -> None:
        user = data.get("user") or {}
        self.state.update(
            online=True,
            user=user.get("username"),
            user_id=user.get("id"),
            servers=len(data.get("guilds") or []),
            invite_url=invite_url(user["id"]) if user.get("id") else None,
            last_error=None,
            connects=self.state["connects"] + 1,
        )
        log.info("Discord bot online as %s in %s server(s)", user.get("username"), self.state["servers"])
        if self.state["servers"] == 0:
            log.warning("the bot is in no Discord server yet: invite it with %s", self.state["invite_url"])
        if self.name and user.get("username") != self.name:
            await self._rename()

    async def _rename(self) -> None:
        """Set the bot's username (Discord allows two changes an hour; a refusal is only logged)."""
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as http:
                r = await http.patch(
                    f"{self.api_base}/users/@me",
                    json={"username": self.name},
                    headers={"Authorization": f"Bot {self.token}", "User-Agent": USER_AGENT},
                )
        except httpx.HTTPError as exc:
            log.warning("bot name not changed: %s", redact(str(exc)))
            return
        if r.status_code == 200:
            self.state["user"] = self.name
        else:
            log.warning("bot name not changed: Discord answered %s", r.status_code)
