"""Configuration: YAML file + environment overrides (env always wins)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("evedm.config")

# Lets the app read a character's own notifications (Notifications page: asset safety, structure attacks).
NOTIFICATION_SCOPE = "esi-characters.read_notifications.v1"
DEFAULT_SCOPES = [
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-location.read_online.v1",
    NOTIFICATION_SCOPE,
]

# ESI notification types, grouped for the filter list. Types ESI sends that are not here show up under
# "other" once seen, so a new CCP type is never lost - it only starts out switched off.
NOTIFICATION_GROUPS: dict[str, list[str]] = {
    "asset_safety": [
        "StructureItemsMovedToSafety",
        "StructureItemsDelivered",
        "StructureImpendingAbandonmentAssetsAtRisk",
    ],
    "structure_attack": [
        "StructureUnderAttack",
        "StructureLostShields",
        "StructureLostArmor",
        "StructureDestroyed",
        "StructuresReinforcementChanged",
        "OwnershipTransferred",
    ],
    "structure_state": [
        "StructureFuelAlert",
        "StructureLowReagentsAlert",
        "StructureNoReagentsAlert",
        "StructureWentLowPower",
        "StructureWentHighPower",
        "StructureServicesOffline",
        "StructureAnchoring",
        "StructureUnanchoring",
        "StructureOnline",
    ],
    "tower": ["TowerAlertMsg", "TowerResourceAlertMsg"],
    "other_attacks": [
        "OrbitalAttacked",
        "OrbitalReinforced",
        "SkyhookUnderAttack",
        "SkyhookLostShields",
        "SkyhookDestroyed",
        "MercenaryDenAttacked",
        "MercenaryDenReinforced",
    ],
    "sovereignty": [
        "SovStructureReinforced",
        "SovStructureDestroyed",
        "EntosisCaptureStarted",
        "SovCommandNodeEventStarted",
        "SovereigntyTCUDamageMsg",
        "SovereigntySBUDamageMsg",
        "SovereigntyIHDamageMsg",
        "InfrastructureHubBillAboutToExpire",
        "IHubDestroyedByBillFailure",
    ],
}
# switched on without a filter; the rest of the catalogue is one tick away
DEFAULT_NOTIFICATION_GROUPS = ["asset_safety", "structure_attack", "structure_state", "tower"]
# Asked for at sign-in and Add character, not needed for the ranking: they let the structure scout list the
# player-owned structures where each character has assets. They must be enabled on the EVE application.
STRUCTURE_SCOPES = ["esi-universe.read_structures.v1", "esi-assets.read_assets.v1"]
# Lets the app set an online character's autopilot destination (Alerts and Ranking pages: "Set destination").
WAYPOINT_SCOPE = "esi-ui.write_waypoint.v1"
# Lets the Ranking page's Ansiblex mode find the jump gates a character may see (character structure search).
ANSIBLEX_SCOPE = "esi-search.search_structures.v1"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when the configuration cannot be used to start the requested mode."""


@dataclass
class EveSettings:
    client_id: str = ""
    client_secret: str = ""
    callback_url: str = "http://localhost:8080/sso/callback"
    required_scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))
    optional_scopes: list[str] = field(default_factory=lambda: [*STRUCTURE_SCOPES, WAYPOINT_SCOPE, ANSIBLEX_SCOPE])

    @property
    def requested_scopes(self) -> list[str]:
        return list(dict.fromkeys([*self.required_scopes, *self.optional_scopes]))

    def esi_user_agent(self, label: str = "") -> str:
        """User-Agent for ESI requests: the configured agent plus ``label`` and the operator contact (character ID)."""
        base = self.user_agent or "EVE-Distance-Meter"  # the default agent is always set; this covers an empty override
        extra = [x for x in (label, self.contact) if x and x not in base]
        return f"{base} ({'; '.join(extra)})" if extra else base

    sso_base: str = "https://login.eveonline.com"
    esi_base: str = "https://esi.evetech.net"
    compatibility_date: str = "2025-09-30"
    user_agent: str = "EVE-Distance-Meter/0.7 (+https://github.com/Headruner/war-bot-2)"
    contact: str = "EVE character ID 196438470"  # ESI asks for a way to reach the operator: added to the User-Agent
    max_concurrency: int = 20
    timeout_s: float = 10.0
    jwt_issuers: list[str] = field(default_factory=lambda: ["https://login.eveonline.com", "login.eveonline.com"])


@dataclass
class WebSettings:
    host: str = "0.0.0.0"  # noqa: S104 - container default, bind is configurable
    port: int = 8080
    auth: str = "sso"  # sso | none
    allowed_login_characters: list[int] = field(default_factory=list)
    session_days: int = 90  # rolling: the cookie is renewed on every visit
    cookie_secure: bool = False
    auto_refresh_s: int = 30
    stale_after_s: int = 120
    refresh_interval_s: float = 5.0  # min seconds between ESI refresh rounds (ESI caches location 5 s)
    max_wait_age_s: float = 30.0  # answer from snapshots younger than this, refresh in the background


@dataclass
class StructureScoutSettings:
    """The structure scout: player-owned structures found through every registered character's assets."""

    enabled: bool = True
    interval_min: float = 60.0  # how often the scout runs
    assets_refresh_hours: float = 24.0  # re-read a character's assets this often
    structure_refresh_hours: float = 720.0  # re-read name, owner and system of known structures this often (0 = never)
    no_access_refresh_hours: float = 336.0  # ask again about a structure that refused (403) this rarely: errors count
    max_asset_pages: int = 1_000_000  # 1000 assets per page: effectively no limit, ESI's X-Pages ends the read
    concurrency: int = 4
    # public datasets that fill in name, owner and system of structures nobody here may read (shown as "historical")
    everef_enabled: bool = True
    everef_url: str = "https://data.everef.net/structures/structures-latest.v2.json"  # EVE Ref scrape, JSON by ID
    everef_max_mb: int = 300  # the download is refused above this size
    # flat list built by scripts/build_structure_index.py; "" = <data dir>/structure-index.jsonl (missing file = unused)
    index_file: str = ""
    # structures a character may dock at, found through ESI's character search (scope esi-search.search_structures.v1)
    dock_search_enabled: bool = True
    dock_search_hours: float = 24.0  # after its first complete pass a character is searched this often
    dock_search_prefix_len: int = 3  # ESI matches the start of a name: prefixes of this many characters of system names
    dock_search_cap: int = 500  # a result this long is treated as cut off (believed ESI limit, to be verified)
    dock_search_max_terms: int = 200  # searches per character and due run; a cursor goes on, the list end ends a pass
    dock_search_punctuated: str = "prefix"  # "full": systems whose prefix has punctuation are searched by full name


@dataclass
class AlertDiscordSettings:
    enabled: bool = False
    webhook_url: str = ""  # secret: EVEDM_ALERTS_DISCORD_WEBHOOK (a different channel from the wars one is fine)
    mention_role_id: str = ""
    kinds: list[str] = field(default_factory=lambda: ["destroyed", "combat", "firing"])
    combat_cooldown_min: float = 30.0  # one combat/firing post per structure within this time


@dataclass
class AlertSettings:
    """Structure alerts from zKillboard's live killmail feed (R2Z2): destroyed, combat on grid, structure firing."""

    enabled: bool = True
    feed_url: str = "https://r2z2.zkillboard.com/ephemeral"
    poll_s: float = 10.0  # zKillboard asks for at least 6 s after "no new killmail"
    grid_km: float = 1000.0  # a kill this close to a known structure counts as combat on its grid
    retention_days: float = 7.0
    max_per_poll: int = 300  # killmail files read per poll at most (zKillboard allows 15 requests/s)
    contact: str = ""  # added to the User-Agent sent to zKillboard
    # Only kills that matter are kept: a battleship or capital hull (T2 variants included) on either side, or a single
    # killmail worth at least ``min_value_isk``. Destroyed structures are always kept. false = keep every kill.
    big_kills_only: bool = True
    min_value_isk: float = 500_000_000.0
    discord: AlertDiscordSettings = field(default_factory=AlertDiscordSettings)


@dataclass
class EveScoutSettings:
    """EVE-Scout's public Thera / Turnur wormhole list, used by the Ranking filter (UC-15)."""

    enabled: bool = True
    url: str = "https://api.eve-scout.com/v2/public/signatures"
    refresh_s: float = 900.0  # the list is read again when older than this (on demand, never in a loop)
    timeout_s: float = 8.0
    hubs: list[str] = field(default_factory=lambda: ["Thera", "Turnur"])


@dataclass
class AnsiblexSettings:
    """Ansiblex jump gates for the Ranking page (UC-16): the gates each character may see, read through ESI."""

    enabled: bool = True
    refresh_hours: float = 6.0  # a character's visible gates are searched again after this long
    gate_refresh_hours: float = 168.0  # a gate's name and system are read again after this long
    concurrency: int = 3


@dataclass
class AccessSettings:
    """Who may see the members-only tabs, Notifications and Structures (D16, D17).

    A tab is shown only to a main whose alliance is in ``alliance_ids`` (default 99010320, "BB"). The alliance
    is read from ESI when the main signs in and again every ``affiliation_hours``. An empty list opens both
    tabs to every signed-in main.
    """

    alliance_ids: list[int] = field(default_factory=lambda: [99010320])
    affiliation_hours: float = 6.0  # how often a main's alliance is read again from ESI


@dataclass
class NotificationDiscordSettings:
    """Notifications posted to Discord. Without a webhook of its own the wars webhook is used
    (``EVEDM_WARS_DISCORD_WEBHOOK``); with neither, nothing is posted."""

    enabled: bool = True
    webhook_url: str = ""  # secret: EVEDM_NOTIFICATIONS_DISCORD_WEBHOOK; empty = the wars webhook
    mention_role_id: str = ""  # pinged on "structure attacked" notifications only
    groups: list[str] = field(default_factory=list)  # which groups are posted; empty = notifications.groups
    max_age_min: float = 60.0  # older notifications are never posted (the history ESI returns on a first read)


@dataclass
class NotificationSettings:
    """Character notifications from ESI: asset safety and structure attacks (UC-13).

    The page lists the notifications of every registered character with a valid token (D17); who may open
    it is decided by ``AccessSettings``.
    """

    enabled: bool = True
    poll_s: float = 60.0  # longest sleep of the loop (it wakes when a character is due); ESI caches a character 600 s
    character_interval_s: float = 600.0  # a character is asked again when its cached copy expires
    retention_days: float = 30.0
    concurrency: int = 4
    groups: list[str] = field(default_factory=lambda: list(DEFAULT_NOTIFICATION_GROUPS))
    # A character is read only when it has assets in at least this many player structures (NPC stations do not
    # count). The default 1 skips only characters with no structure at all; a larger value saves ESI requests but
    # silently stops reading everybody below it. 0 = read everyone, also before the first asset scan.
    min_structures: int = 1
    # Never ask one character more often than this when ESI sends no usable Expires; a real Expires is followed.
    min_interval_s: float = 30.0
    retry_s: float = 60.0  # a character whose read failed (ESI 5xx, error limit, network) is retried after this
    # Spread the reads of the characters over ESI's cache window: several characters of one corporation get the
    # same structure notification, each with its own 600 s cache, so with their reads evenly apart the first copy
    # arrives after 600 s / characters at worst instead of 600 s. A read is moved later by at most this many
    # seconds per round until the reads are apart. 0 = off.
    stagger_s: float = 60.0
    wait_s: float = 20.0  # how long the page's "anything new?" request is held open (long poll)
    discord: NotificationDiscordSettings = field(default_factory=NotificationDiscordSettings)

    def default_types(self) -> list[str]:
        return [t for g in self.groups for t in NOTIFICATION_GROUPS.get(g, [])]


@dataclass
class Settings:
    instance_label: str = "evedm"
    data_dir: Path = Path("./data")
    secret_key: str = ""
    demo: bool = False
    sqlite_journal: str = "wal"  # wal (default) | delete (fallback for filesystems without shared memory)
    universe_file: Path | None = None
    avoid_systems: list[str] = field(default_factory=lambda: ["Zarzakh"])
    log_level: str = "INFO"
    eve: EveSettings = field(default_factory=EveSettings)
    web: WebSettings = field(default_factory=WebSettings)
    structures: StructureScoutSettings = field(default_factory=StructureScoutSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    access: AccessSettings = field(default_factory=AccessSettings)
    evescout: EveScoutSettings = field(default_factory=EveScoutSettings)
    ansiblex: AnsiblexSettings = field(default_factory=AnsiblexSettings)

    @property
    def db_path(self) -> Path:
        return self.data_dir.resolve() / "evedm.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir.resolve() / "logs"

    def summary(self) -> dict[str, Any]:
        """Non-secret view of the settings, used in launch logs."""
        return {
            "instance": self.instance_label,
            "data_dir": str(self.data_dir.resolve()),
            "demo": self.demo,
            "sqlite_journal": self.sqlite_journal,
            "web_auth": self.web.auth,
            "bind": f"{self.web.host}:{self.web.port}",
            "esi_base": self.eve.esi_base,
            "sso_base": self.eve.sso_base,
            "client_id_set": bool(self.eve.client_id),
            "secret_key_set": bool(self.secret_key),
            "avoid_systems": self.avoid_systems,
            "optional_scopes": self.eve.optional_scopes,
            "structure_scout": self.structures.enabled,
            "alerts": self.alerts.enabled,
            "alerts_discord": self.alerts.discord.enabled,
            "notifications": self.notifications.enabled,
            "access_alliances": self.access.alliance_ids,
            "evescout": self.evescout.enabled,
            "ansiblex": self.ansiblex.enabled,
        }

    def validate_for_serve(self) -> None:
        """WEB-05: refuse to start without the secrets the mode needs."""
        missing = []
        if not self.secret_key:
            missing.append("EVEDM_SECRET_KEY")
        if not self.demo:
            if not self.eve.client_id:
                missing.append("EVE_CLIENT_ID")
            if not self.eve.client_secret:
                missing.append("EVE_CLIENT_SECRET")
        if missing:
            raise ConfigError("missing required settings: " + ", ".join(missing))
        if self.web.auth not in ("sso", "none"):
            raise ConfigError("web.auth must be 'sso' or 'none'")
        if self.sqlite_journal not in ("delete", "wal"):
            raise ConfigError("sqlite_journal must be 'delete' or 'wal'")
        sc = self.structures
        if sc.dock_search_enabled:
            if sc.dock_search_prefix_len < 3:
                raise ConfigError("structures.dock_search_prefix_len must be at least 3 (ESI's search rule)")
            if sc.dock_search_hours <= 0 or sc.dock_search_cap < 1 or sc.dock_search_max_terms < 1:
                raise ConfigError(
                    "structures.dock_search_hours > 0, dock_search_cap >= 1 and dock_search_max_terms >= 1"
                )
            if sc.dock_search_punctuated not in ("prefix", "full"):
                raise ConfigError("structures.dock_search_punctuated must be 'prefix' or 'full'")
        a = self.alerts
        if a.enabled and a.poll_s < 6:
            raise ConfigError("alerts.poll_s must be at least 6 (zKillboard's rule)")
        if a.grid_km <= 0 or a.retention_days <= 0 or not 1 <= a.max_per_poll <= 2000:
            raise ConfigError("alerts.grid_km and alerts.retention_days > 0, alerts.max_per_poll 1..2000")
        if a.min_value_isk < 0:
            raise ConfigError("alerts.min_value_isk must be 0 or more")
        bad = [k for k in a.discord.kinds if k not in ("destroyed", "combat", "firing")]
        if bad:
            raise ConfigError(f"alerts.discord.kinds: unknown {bad}; use destroyed, combat, firing")
        if a.enabled and a.discord.enabled and not a.discord.webhook_url:
            raise ConfigError("alerts.discord.enabled needs a webhook (EVEDM_ALERTS_DISCORD_WEBHOOK)")
        n = self.notifications
        if n.enabled:
            if n.poll_s < 30:
                raise ConfigError("notifications.poll_s must be at least 30 (ESI caches notifications 600 s)")
            if n.retention_days <= 0 or n.character_interval_s < 60:
                raise ConfigError("notifications.retention_days > 0 and character_interval_s >= 60")
            if n.min_structures < 0 or n.min_interval_s < 1 or n.retry_s < 5:
                raise ConfigError("notifications.min_structures >= 0, min_interval_s >= 1 and retry_s >= 5")
            unknown_d = [g for g in n.discord.groups if g not in NOTIFICATION_GROUPS]
            if unknown_d or n.discord.max_age_min <= 0:
                raise ConfigError(
                    f"notifications.discord.groups: unknown {unknown_d}; max_age_min must be above 0"
                )
            if n.stagger_s < 0 or not 1 <= n.wait_s <= 60:
                raise ConfigError("notifications.stagger_s >= 0 and wait_s between 1 and 60")
            unknown = [g for g in n.groups if g not in NOTIFICATION_GROUPS]
            if unknown:
                raise ConfigError(
                    f"notifications.groups: unknown {unknown}; use {', '.join(NOTIFICATION_GROUPS)}"
                )


def _expand(value: Any) -> Any:
    """Replace ${NAME} and ${NAME:-default}; like a shell, the default also applies when NAME is set but empty."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _legacy_access(raw: dict[str, Any]) -> dict[str, Any]:
    """0.8.x kept the alliance gate under ``notifications``; use it when there is no ``access`` section."""
    notifications = raw.get("notifications")
    if "access" in raw or not isinstance(notifications, dict):
        return raw
    moved = {k: notifications[k] for k in ("alliance_ids", "affiliation_hours") if k in notifications}
    return {**raw, "access": moved} if moved else raw


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


REMOVED_KEYS = {(StructureScoutSettings, "dock_search_term"): "ESI matches name prefixes: see dock_search_prefix_len"}


def _apply(obj: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if not hasattr(obj, key):  # unknown keys are skipped without a word, except the ones that were removed
            if (type(obj), key) in REMOVED_KEYS:
                why = REMOVED_KEYS[type(obj), key]
                log.info("setting structures.%s is no longer used and is ignored (%s)", key, why)
            continue
        current = getattr(obj, key)
        nested = (
            EveSettings,
            WebSettings,
            StructureScoutSettings,
            AlertSettings,
            AlertDiscordSettings,
            NotificationSettings,
            NotificationDiscordSettings,
            AccessSettings,
            EveScoutSettings,
            AnsiblexSettings,
        )
        if isinstance(current, nested) and isinstance(value, dict):
            _apply(current, value)
        elif key in ("data_dir", "universe_file") and value:
            setattr(obj, key, Path(value))
        elif isinstance(current, bool):
            setattr(obj, key, value if isinstance(value, bool) else _truthy(value))
        elif isinstance(current, int) and not isinstance(current, bool):
            setattr(obj, key, int(value))
        elif isinstance(current, float):
            setattr(obj, key, float(value))
        else:
            setattr(obj, key, value)


ENV_MAP: dict[str, tuple[str, ...]] = {
    "EVEDM_INSTANCE": ("instance_label",),
    "EVEDM_DATA_DIR": ("data_dir",),
    "EVEDM_SECRET_KEY": ("secret_key",),
    "EVEDM_DEMO": ("demo",),
    "EVEDM_SQLITE_JOURNAL": ("sqlite_journal",),
    "EVEDM_UNIVERSE_FILE": ("universe_file",),
    "EVEDM_LOG_LEVEL": ("log_level",),
    "EVE_CLIENT_ID": ("eve", "client_id"),
    "EVE_CLIENT_SECRET": ("eve", "client_secret"),
    "EVE_CALLBACK_URL": ("eve", "callback_url"),
    "EVEDM_SSO_BASE": ("eve", "sso_base"),
    "EVEDM_ESI_BASE": ("eve", "esi_base"),
    "EVEDM_ESI_CONTACT": ("eve", "contact"),
    "EVEDM_ESI_CONCURRENCY": ("eve", "max_concurrency"),
    "EVEDM_HOST": ("web", "host"),
    "EVEDM_PORT": ("web", "port"),
    "EVEDM_WEB_AUTH": ("web", "auth"),
    "EVEDM_COOKIE_SECURE": ("web", "cookie_secure"),
    "EVEDM_ALLOWED_LOGIN_CHARACTERS": ("web", "allowed_login_characters"),
    "EVEDM_REFRESH_INTERVAL_S": ("web", "refresh_interval_s"),
    "EVEDM_MAX_WAIT_AGE_S": ("web", "max_wait_age_s"),
    "EVE_OPTIONAL_SCOPES": ("eve", "optional_scopes"),
    "EVEDM_STRUCTURES_ENABLED": ("structures", "enabled"),
    "EVEDM_STRUCTURES_INTERVAL_MIN": ("structures", "interval_min"),
    "EVEDM_ALERTS_ENABLED": ("alerts", "enabled"),
    "EVEDM_ALERTS_FEED_URL": ("alerts", "feed_url"),
    "EVEDM_ALERTS_GRID_KM": ("alerts", "grid_km"),
    "EVEDM_ALERTS_CONTACT": ("alerts", "contact"),
    "EVEDM_ALERTS_DISCORD_ENABLED": ("alerts", "discord", "enabled"),
    "EVEDM_ALERTS_DISCORD_WEBHOOK": ("alerts", "discord", "webhook_url"),
    "EVEDM_ALERTS_DISCORD_MENTION_ROLE_ID": ("alerts", "discord", "mention_role_id"),
    "EVEDM_NOTIFICATIONS_ENABLED": ("notifications", "enabled"),
    "EVEDM_NOTIFICATIONS_ALLIANCES": ("access", "alliance_ids"),  # 0.8.x name; EVEDM_ACCESS_ALLIANCES wins
    "EVEDM_EVESCOUT_ENABLED": ("evescout", "enabled"),
    "EVEDM_EVESCOUT_URL": ("evescout", "url"),
    "EVEDM_EVESCOUT_REFRESH_S": ("evescout", "refresh_s"),
    "EVEDM_ANSIBLEX_ENABLED": ("ansiblex", "enabled"),
    "EVEDM_ALERTS_MIN_VALUE_ISK": ("alerts", "min_value_isk"),
    "EVEDM_ALERTS_BIG_KILLS_ONLY": ("alerts", "big_kills_only"),
    "EVEDM_NOTIFICATIONS_MIN_STRUCTURES": ("notifications", "min_structures"),
    "EVEDM_NOTIFICATIONS_DISCORD_ENABLED": ("notifications", "discord", "enabled"),
    "EVEDM_NOTIFICATIONS_DISCORD_WEBHOOK": ("notifications", "discord", "webhook_url"),
    "EVEDM_NOTIFICATIONS_DISCORD_MENTION_ROLE_ID": ("notifications", "discord", "mention_role_id"),
    "EVEDM_ACCESS_ALLIANCES": ("access", "alliance_ids"),
    "EVEDM_ACCESS_AFFILIATION_HOURS": ("access", "affiliation_hours"),
    "EVEDM_NOTIFICATIONS_POLL_S": ("notifications", "poll_s"),
    "EVEDM_NOTIFICATIONS_GROUPS": ("notifications", "groups"),
}


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from YAML (optional) and apply environment overrides."""
    try:
        return _load_settings(path)
    except ConfigError:
        raise
    except (TypeError, ValueError, yaml.YAMLError) as exc:  # e.g. EVEDM_PORT=eighty: a message, not a traceback
        raise ConfigError(f"invalid setting: {exc}") from exc


def _load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    settings = Settings()
    cfg_path = path or os.environ.get("EVEDM_CONFIG")
    if cfg_path and Path(cfg_path).is_file():
        raw = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}
        _apply(settings, _legacy_access(_expand(raw)))
    for env, target in ENV_MAP.items():
        value = os.environ.get(env)
        if value is None or value == "":
            continue
        obj: Any = settings
        for part in target[:-1]:
            obj = getattr(obj, part)
        if target[-1] == "alliance_ids":
            setattr(obj, target[-1], [int(v) for v in value.replace(";", ",").split(",") if v.strip()])
        elif target[-1] == "groups":
            setattr(obj, target[-1], [v for v in value.replace(",", " ").split() if v.strip()])
        elif target[-1] == "allowed_login_characters":
            setattr(obj, target[-1], [int(v) for v in value.replace(";", ",").split(",") if v.strip()])
        elif target[-1] == "optional_scopes":  # comma or space separated; "none" asks for no extra scope
            parts = [v for v in value.replace(",", " ").split() if v.strip()]
            setattr(obj, target[-1], [] if [p.lower() for p in parts] == ["none"] else parts)
        else:
            _apply(obj, {target[-1]: value})
    settings.web.allowed_login_characters = [int(v) for v in settings.web.allowed_login_characters]
    settings.access.alliance_ids = [int(v) for v in settings.access.alliance_ids]
    if (
        settings.demo
        and not settings.eve.client_id
        and settings.web.auth == "sso"
        and os.environ.get("EVEDM_WEB_AUTH") != "sso"
    ):
        settings.web.auth = "none"  # demo without an EVE application: sign-in is impossible otherwise
    return settings
