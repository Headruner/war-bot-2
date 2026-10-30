"""Configuration: YAML file + environment overrides (env always wins)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SCOPES = [
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-location.read_online.v1",
]

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when the configuration cannot be used to start the requested mode."""


@dataclass
class EveSettings:
    client_id: str = ""
    client_secret: str = ""
    callback_url: str = "http://localhost:8080/sso/callback"
    required_scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))
    sso_base: str = "https://login.eveonline.com"
    esi_base: str = "https://esi.evetech.net"
    compatibility_date: str = "2025-09-30"
    user_agent: str = "EVE-Distance-Meter/0.3 (+https://github.com/)"
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


def _expand(value: Any) -> Any:
    """Replace ${NAME} and ${NAME:-default}; like a shell, the default also applies when NAME is set but empty."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _apply(obj: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if not hasattr(obj, key):
            continue
        current = getattr(obj, key)
        if isinstance(current, (EveSettings, WebSettings)) and isinstance(value, dict):
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
    "EVEDM_ESI_CONCURRENCY": ("eve", "max_concurrency"),
    "EVEDM_HOST": ("web", "host"),
    "EVEDM_PORT": ("web", "port"),
    "EVEDM_WEB_AUTH": ("web", "auth"),
    "EVEDM_COOKIE_SECURE": ("web", "cookie_secure"),
    "EVEDM_ALLOWED_LOGIN_CHARACTERS": ("web", "allowed_login_characters"),
    "EVEDM_REFRESH_INTERVAL_S": ("web", "refresh_interval_s"),
    "EVEDM_MAX_WAIT_AGE_S": ("web", "max_wait_age_s"),
}


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from YAML (optional) and apply environment overrides."""
    settings = Settings()
    cfg_path = path or os.environ.get("EVEDM_CONFIG")
    if cfg_path and Path(cfg_path).is_file():
        raw = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}
        _apply(settings, _expand(raw))
    for env, target in ENV_MAP.items():
        value = os.environ.get(env)
        if value is None or value == "":
            continue
        obj: Any = settings
        for part in target[:-1]:
            obj = getattr(obj, part)
        if target[-1] == "allowed_login_characters":
            setattr(obj, target[-1], [int(v) for v in value.replace(";", ",").split(",") if v.strip()])
        else:
            _apply(obj, {target[-1]: value})
    settings.web.allowed_login_characters = [int(v) for v in settings.web.allowed_login_characters]
    if (
        settings.demo
        and not settings.eve.client_id
        and settings.web.auth == "sso"
        and os.environ.get("EVEDM_WEB_AUTH") != "sso"
    ):
        settings.web.auth = "none"  # demo without an EVE application: sign-in is impossible otherwise
    return settings
