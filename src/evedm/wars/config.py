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
    census_enabled: bool = True  # follow every active war in New Eden (statistics, high-sec structures)
    census_max_ids: int = 20000  # how many of the newest war IDs to examine (active wars hide among them)
    census_pages_per_cycle: int = 2  # pages of 2000 war IDs examined per cycle while the first scan runs
    census_refresh_hours: float = 6  # re-read active wars this often (score, retracted, finished)
    census_concurrency: int = 8  # parallel ESI requests of the census
    contact: str = ""  # added to the User-Agent sent to ESI and EVE Ref
    public_url: str = ""  # address of this site for links in Discord (default: derived from EVE_CALLBACK_URL)
    discord: DiscordSettings = field(default_factory=DiscordSettings)
    source_path: str = ""  # the file the settings came from (informational)

    def validate(self) -> None:
        bad = [s for s in self.statuses if s not in WAR_TYPES]
        if bad:
            raise ConfigError(f"wars.statuses: unknown war type(s) {bad}; use {list(WAR_TYPES)}")
        if self.poll_interval_min < 1:
            raise ConfigError("wars.poll_interval_min must be at least 1")
        if self.census_max_ids < 0 or self.census_pages_per_cycle < 1 or not 1 <= self.census_concurrency <= 32:
            raise ConfigError("wars.census_*: max_ids >= 0, pages_per_cycle >= 1, concurrency 1..32")
        if self.max_new_per_cycle < 1 or self.page_limit < 1:
            raise ConfigError("wars.max_new_per_cycle and wars.page_limit must be at least 1")
        if not 1 <= self.discord.batch_embeds <= 10:
            raise ConfigError("wars.discord.batch_embeds must be between 1 and 10")
        if self.public_url and not self.public_url.startswith(("http://", "https://")):
            raise ConfigError("wars.public_url must start with http:// or https://")
        if self.enabled and self.discord.enabled and not self.discord.dry_run and not self.discord.webhook_url:
            raise ConfigError("wars.discord.enabled needs discord.webhook_url (or EVEDM_WARS_DISCORD_WEBHOOK)")

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "config": self.source_path,
            "poll_interval_min": self.poll_interval_min,
            "statuses": self.statuses or "all",
            "structures": self.structures_enabled,
            "census": self.census_enabled,
            "census_max_ids": self.census_max_ids,
            "discord": self.discord.enabled,
            "discord_dry_run": self.discord.dry_run,
            "webhook_set": bool(self.discord.webhook_url),
        }


ENV_MAP: dict[str, tuple[str, ...]] = {
    "EVEDM_WARS_ENABLED": ("enabled",),
    "EVEDM_WARS_POLL_INTERVAL_MIN": ("poll_interval_min",),
    "EVEDM_WARS_CONTACT": ("contact",),
    "EVEDM_WARS_STATUSES": ("statuses",),
    "EVEDM_PUBLIC_URL": ("public_url",),
    "EVEDM_WARS_DISCORD_ENABLED": ("discord", "enabled"),
    "EVEDM_WARS_DISCORD_WEBHOOK": ("discord", "webhook_url"),
    "EVEDM_WARS_DISCORD_MENTION_ROLE_ID": ("discord", "mention_role_id"),
    "EVEDM_WARS_DISCORD_DRY_RUN": ("discord", "dry_run"),
}


def _statuses(value: Any) -> list[str]:
    """A list, or a comma-separated string (as from an environment variable); empty means all types."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    raise ConfigError(f"wars.statuses must be a list, got {type(value).__name__}")


def _apply_wars(obj: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key == "discord":
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ConfigError("wars.discord must be a mapping")
            _apply(obj.discord, value)
        elif key == "statuses":
            obj.statuses = _statuses(value)
        elif hasattr(obj, key) and key != "source_path":
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
    try:
        if cfg is not None and cfg.is_file():
            try:
                raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"{cfg}: not valid YAML: {exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"{cfg}: expected a mapping of settings")
            _apply_wars(s, _expand(raw))
            s.source_path = str(cfg.resolve())
        for env, target in ENV_MAP.items():
            value = os.environ.get(env)
            if value is None or value == "":
                continue
            if target == ("statuses",):
                s.statuses = _statuses(value)
                continue
            obj: Any = s
            for part in target[:-1]:
                obj = getattr(obj, part)
            _apply(obj, {target[-1]: value})
    except (TypeError, ValueError) as exc:  # e.g. poll_interval_min: soon
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"wars settings: invalid value ({exc})") from exc
    s.validate()
    return s
