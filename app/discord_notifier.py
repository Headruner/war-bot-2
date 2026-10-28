"""Discord Notifier with async support and banner upload."""
from __future__ import annotations
import logging
import httpx
from typing import List, Optional, Dict
from .models import NotifyError, WebhookInvalid

log = logging.getLogger(__name__)

class DiscordNotifier:
    def __init__(self, webhook_url: str, min_seconds_between_posts: int = 2, 
                 dry_run: bool = False):
        self.webhook_url = webhook_url
        self.min_seconds_between_posts = min_seconds_between_posts
        self.dry_run = dry_run
        self.last_post_time = 0.0

    async def send(self, payload: Dict, attachments: Optional[List[tuple]] = None) -> Dict:
        """Sends a message to Discord. Returns the message object or {}."""
        if self.dry_run:
            log.info("DRY RUN: Payload: %s | Attachments: %s", payload, attachments)
            return {"id": "dry_run_id", "attachments": []}

        # Rate limit check
        import time
        now = time.time()
        if now - self.last_post_time < self.min_seconds_between_posts:
            wait = self.min_seconds_between_posts - (now - self.last_post_time)
            log.info("Rate limiting: waiting %.2fs", wait)
            await asyncio.sleep(wait)

        async with httpx.AsyncClient() as client:
            try:
                # If we have attachments, we must use multipart form data
                if attachments:
                    files = []
                    for name, data in attachments:
                        files.append(("files", (name, data)))
                    
                    # This is a simplified way to handle multipart for a single webhook
                    # Discord's multipart for webhooks is slightly different than standard
                    # but usually follows the standard form-data rules.
                    response = await client.post(
                        self.webhook_url,
                        data={"payload": json.dumps(payload)},
                        files=files
                    )
                else:
                    response = await client.post(self.webhook_url, json=payload)
                
                response.raise_for_status()
                self.last_post_time = time.time()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise WebhookInvalid("Webhook URL is invalid or deleted.")
                raise NotifyError(f"Discord error {e.response.status_code}: {e.response.text}")
            except Exception as e:
                log.error("Discord notifier error: %s", e)
                raise NotifyError(str(e))
import json
import asyncio
