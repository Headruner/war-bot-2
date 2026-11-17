"""Discord notifier: posts and edits channel messages as a registered application (bot token, REST only).

No gateway connection is kept, so the bot shows as offline. In the channel it needs View Channel, Send Messages and
Embed Links (and Mention Everyone to ping a role that is not mentionable). Throttled, honours 429 retry_after.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .. import __version__
from ..discord_rules import is_webhook

log = logging.getLogger("evedm.wars.discord")

API_BASE = "https://discord.com/api/v10"
USER_AGENT = f"DiscordBot (https://github.com/Headruner/war-bot-2, {__version__})"  # the form Discord asks of bots
MAX_RATE_LIMIT_RETRIES = 3
UNKNOWN_CHANNEL = 10003
UNKNOWN_WEBHOOK = 10015
NOT_THE_AUTHOR = 50005  # "Cannot edit a message authored by another user"


class NotifyError(Exception):
    """A post failed (the wars stay queued and are retried)."""


class DiscordRejected(NotifyError):
    """Discord rejected the bot itself (401 token, 403 no access to the channel, 404 no such channel): fix the
    configuration. ``status`` is Discord's answer (None: nothing was sent, the bot is not configured)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _code(response: httpx.Response) -> int:
    """Discord's own error code of a failed request (0 when the body has none)."""
    try:
        return int(response.json().get("code", 0))
    except (ValueError, TypeError, AttributeError):
        return 0


class DiscordNotifier:
    def __init__(
        self,
        bot_token: str,
        channel_id: str | int,
        min_seconds_between_posts: float = 2,
        dry_run: bool = False,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base: str = API_BASE,
    ):
        self.bot_token = str(bot_token or "").strip()
        self.channel_id = str(channel_id or "").strip()
        self.min_seconds_between_posts = min_seconds_between_posts
        self.dry_run = dry_run
        self.timeout = timeout
        self.transport = transport
        self.api_base = api_base.rstrip("/")
        self.last_post_time = 0.0

    def messages_url(self, channel_id: str | int | None = None) -> str:
        """The channel given (a rule's), else the notifier's own. A webhook URL is its own messages URL."""
        channel = str(channel_id or "").strip() or self.channel_id
        if is_webhook(channel):
            return channel.split("?")[0].rstrip("/")
        if not channel:
            raise DiscordRejected("the channel ID is not configured")
        return f"{self.api_base}/channels/{channel}/messages"

    async def _throttle(self) -> None:
        wait = self.min_seconds_between_posts - (time.monotonic() - self.last_post_time)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _request(self, method: str, url: str, payload: dict) -> httpx.Response:
        """One call as the bot (a webhook URL carries its own token: no bot header); a 429 is waited out and tried
        again a few times."""
        hook = is_webhook(url.split("?")[0].split("/messages/")[0])
        if not hook and not self.bot_token:
            raise DiscordRejected("the bot token is not configured")
        headers = {"User-Agent": USER_AGENT}
        if not hook:
            headers["Authorization"] = f"Bot {self.bot_token}"
        for _ in range(MAX_RATE_LIMIT_RETRIES):
            await self._throttle()
            try:
                async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                    response = await client.request(method, url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                raise NotifyError(f"Discord request failed: {e!r}") from e
            self.last_post_time = time.monotonic()
            if response.status_code != 429:
                return response
            try:
                retry_after = float(response.json().get("retry_after", 1))
            except (ValueError, TypeError, AttributeError):
                retry_after = 1.0
            log.warning("Discord rate limited; retrying in %.1fs", retry_after)
            await asyncio.sleep(min(retry_after, 60))
        raise NotifyError("Discord rate limit: giving up for this cycle")

    async def edit(self, message_id: str, payload: dict, channel_id: str | None = None) -> dict | None:
        """Replace a message the bot posted earlier. None when the message no longer exists.

        A message of the webhook used before the bot cannot be edited (only its author may): it is left as it
        is and reported as done, so nothing is posted a second time."""
        if self.dry_run or message_id == "dry_run":
            titles = [e.get("title") for e in payload.get("embeds", [])]
            log.info("DRY RUN: would edit message %s: %s", message_id, titles)
            return {"id": message_id}
        base = self.messages_url(channel_id)
        url = f"{base}/messages/{message_id}" if is_webhook(base) else f"{base}/{message_id}"
        response = await self._request("PATCH", url, payload)
        status, code = response.status_code, _code(response)
        if status == 404 and code == UNKNOWN_WEBHOOK:
            raise DiscordRejected("Discord rejected the webhook (404): it was deleted", status)
        if status == 403 and code == NOT_THE_AUTHOR:
            log.info("message %s was not posted by the bot (the old webhook?): left as it is", message_id)
            return {"id": message_id}
        if status == 404 and code != UNKNOWN_CHANNEL:
            return None  # the message was deleted in Discord
        if status in (401, 403, 404):
            raise DiscordRejected(f"Discord rejected the bot ({status}): {response.text[:200]}", status)
        if status >= 400:
            raise NotifyError(f"Discord error {status}: {response.text[:500]}")
        return response.json() if response.content else {}

    async def send(self, payload: dict, channel_id: str | None = None) -> dict:
        """Post a message to the channel (the notifier's own unless one is given) and return Discord's message."""
        if self.dry_run:
            log.info(
                "DRY RUN: would post %d embed(s): %s",
                len(payload.get("embeds", [])),
                [e.get("title") for e in payload.get("embeds", [])],
            )
            return {"id": "dry_run"}
        url = self.messages_url(channel_id)
        response = await self._request("POST", f"{url}?wait=true" if is_webhook(url) else url, payload)
        status = response.status_code
        if status in (401, 403, 404):
            raise DiscordRejected(f"Discord rejected the bot ({status}): {response.text[:200]}", status)
        if status >= 400:
            raise NotifyError(f"Discord error {status}: {response.text[:500]}")
        return response.json() if response.content else {}
