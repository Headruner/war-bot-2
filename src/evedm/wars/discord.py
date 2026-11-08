"""Discord webhook notifier (from eve-war-bot-2): throttled, honours 429 retry_after, confirms with ?wait=true."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("evedm.wars.discord")

MAX_RATE_LIMIT_RETRIES = 3


class NotifyError(Exception):
    """A post failed (the wars stay queued and are retried)."""


class WebhookInvalid(NotifyError):
    """Discord rejected the webhook itself (401/403/404): fix the configuration."""


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str,
        min_seconds_between_posts: float = 2,
        dry_run: bool = False,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.webhook_url = webhook_url
        self.min_seconds_between_posts = min_seconds_between_posts
        self.dry_run = dry_run
        self.timeout = timeout
        self.transport = transport
        self.last_post_time = 0.0

    async def _throttle(self) -> None:
        wait = self.min_seconds_between_posts - (time.monotonic() - self.last_post_time)
        if wait > 0:
            await asyncio.sleep(wait)

    async def send(self, payload: dict) -> dict:
        """Post a message and return Discord's message object."""
        if self.dry_run:
            log.info(
                "DRY RUN: would post %d embed(s): %s",
                len(payload.get("embeds", [])),
                [e.get("title") for e in payload.get("embeds", [])],
            )
            return {"id": "dry_run"}
        if not self.webhook_url:
            raise WebhookInvalid("webhook_url is not configured")
        for _ in range(MAX_RATE_LIMIT_RETRIES):
            await self._throttle()
            try:
                async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                    response = await client.post(self.webhook_url, params={"wait": "true"}, json=payload)
            except httpx.HTTPError as e:
                raise NotifyError(f"Discord request failed: {e!r}") from e
            self.last_post_time = time.monotonic()
            if response.status_code == 429:
                try:
                    retry_after = float(response.json().get("retry_after", 1))
                except ValueError:
                    retry_after = 1.0
                log.warning("Discord rate limited; retrying in %.1fs", retry_after)
                await asyncio.sleep(min(retry_after, 60))
                continue
            if response.status_code in (401, 403, 404):
                raise WebhookInvalid(f"Discord rejected the webhook ({response.status_code}): {response.text[:200]}")
            if response.status_code >= 400:
                raise NotifyError(f"Discord error {response.status_code}: {response.text[:500]}")
            return response.json() if response.content else {}
        raise NotifyError("Discord rate limit: giving up for this cycle")
