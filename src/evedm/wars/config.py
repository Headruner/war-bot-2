"""Settings of the war watcher, kept in their own YAML file (config/wars.yaml, $EVEDM_WARS_CONFIG).

Values like ${NAME} or ${NAME:-default} are read from the environment; EVEDM_WARS_* variables override the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import ConfigError, _apply, _expand

WAR_TYPES = ("none", "mutual", "open_for_allies")


@dataclass
class DiscordSettings:
    enabled: bool = False
    webhook_url: str = ""  # secret: prefer ${EVEDM_WARS_DISCORD_WEBHOOK}
    mention_role_id: str = ""
    dry_run: bool = False  # log the payloads instead of posting
    batch_embeds: int = 5  # wars per Discord message (max 10)
    min_seconds_between_posts: float = 2.0


@dataclass
class WarsSettings:
    enabled: bool = False
    poll_interval_min: float = 15
    max_new_per_cycle: int = 50
    bootstrap_hours: float = 24  # on a clean database, also show wars declared in the last N hours
    max_war_age_hours: float = 48  # skip "new" war IDs declared longer ago (ESI re-issues old wars)
    statuses: list[str] = field(default_factory=list)  # war types to keep; empty = all
    report_enabled: bool = True
    report_recent_kills: int = 5
    report_kill_details_limit: int = 25
    report_max_killmail_pages: int = 1
    alliance_member_count_max_corps: int = 20
    structures_enabled: bool = True
    structures_refresh_hours: float = 24
    structures_url: str = "https://data.everef.net/structures/structures-latest.v2.json"
    page_limit: int = 200  # wars listed on the Wars page
    contact: str = ""  # added to the User-Agent sent to ESI and EVE Ref
    discord: DiscordSettings = field(default_factory=DiscordSettings)
    source_path: str = ""  # the file the settings came from (informational)

    def validate(self) -> None:
        bad = [s for s in self.statuses if s not in WAR_TYPES]
        if bad:
            raise ConfigError(f"wars.statuses: unknown war type(s) {bad}; use {list(WAR_TYPES)}")
        if self.poll_interval_min < 1:
            raise ConfigError("wars.poll_interval_min must be at least 1")
        if self.enabled and self.discord.enabled and not self.discord.dry_run and not self.discord.webhook_url:
            raise ConfigError("wars.discord.enabled needs discord.webhook_url (or EVEDM_WARS_DISCORD_WEBHOOK)")

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "config": self.source_path,
            "poll_interval_min": self.poll_interval_min,
            "statuses": self.statuses or "all",
            "structures": self.structures_enabled,
            "discord": self.discord.enabled,
            "discord_dry_run": self.discord.dry_run,
            "webhook_set": bool(self.discord.webhook_url),
        }


ENV_MAP: dict[str, tuple[str, ...]] = {
    "EVEDM_WARS_ENABLED": ("enabled",),
    "EVEDM_WARS_POLL_INTERVAL_MIN": ("poll_interval_min",),
    "EVEDM_WARS_CONTACT": ("contact",),
    "EVEDM_WARS_DISCORD_ENABLED": ("discord", "enabled"),
    "EVEDM_WARS_DISCORD_WEBHOOK": ("discord", "webhook_url"),
    "EVEDM_WARS_DISCORD_MENTION_ROLE_ID": ("discord", "mention_role_id"),
    "EVEDM_WARS_DISCORD_DRY_RUN": ("discord", "dry_run"),
}


def _apply_wars(obj: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key == "discord" and isinstance(value, dict):
            _apply(obj.discord, value)
        elif key == "statuses":
            obj.statuses = [str(v) for v in (value or [])]
        elif hasattr(obj, key):
            _apply(obj, {key: value})


def default_path() -> Path | None:
    env = os.environ.get("EVEDM_WARS_CONFIG")
    if env:
        return Path(env)
    main = os.environ.get("EVEDM_CONFIG")
    if main:  # next to the main config file (/app/config in the image)
        return Path(main).resolve().parent / "wars.yaml"
    return Path("config/wars.yaml")


def load_wars_settings(path: str | os.PathLike[str] | None = None) -> WarsSettings:
    s = WarsSettings()
    cfg = Path(path) if path else default_path()
    if cfg is not None and cfg.is_file():
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{cfg}: expected a mapping")
        _apply_wars(s, _expand(raw))
        s.source_path = str(cfg.resolve())
    for env, target in ENV_MAP.items():
        value = os.environ.get(env)
        if value is None or value == "":
            continue
        obj: Any = s
        for part in target[:-1]:
            obj = getattr(obj, part)
        _apply(obj, {target[-1]: value})
    s.validate()
    return s
