"""The Settings page's store: values changed there are kept as overrides in ``<data_dir>/settings.yaml``.

Layers, lowest first: defaults, the configuration files (evedm.yaml, wars.yaml), environment variables, this file.
Only changed values are written and secrets are encrypted with EVEDM_SECRET_KEY. A value that fails validation is
refused before anything is saved; a file that no longer validates at start-up is ignored (and logged) so the app
still starts. The dataclass instances the services hold are changed in place, so most settings apply at once; the
ones captured at start-up are marked ``restart``.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import inspect
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from . import config as main_config
from .config import ConfigError, Settings
from .crypto import TokenCipher
from .discord_rules import RuleError, normalize_all
from .wars import config as wars_config
from .wars.config import WarsSettings

log = logging.getLogger("evedm.settings")

FILES = ("evedm", "wars")
SOURCES = {"evedm": "evedm.yaml", "wars": "wars.yaml"}
ENCRYPTED = "enc:"
# never shown: the key everything else is encrypted with; the wars copy of the bot token follows discord.bot_token
HIDDEN = {"evedm": {"secret_key"}, "wars": {"discord.bot_token", "source_path"}}
# shown, not editable: a wrong value here would lock the administrator out or stop the app from starting
LOCKED = {
    "evedm": {
        "data_dir",
        "universe_file",
        "demo",
        "sqlite_journal",
        "web.host",
        "web.port",
        "web.auth",
        "web.admin_characters",
    },
    "wars": set(),
}
SECRET = {"evedm": {"discord.bot_token", "eve.client_secret"}, "wars": set()}
RULES = {"evedm": {"discord.rules"}, "wars": set()}  # a list of mappings with an editor of its own on the page
# read once at start-up (a constructor argument, an HTTP client, whether a service exists at all)
RESTART = {
    "evedm": {
        "instance_label",
        "avoid_systems",
        "log_level",
        "eve.client_id",
        "eve.callback_url",
        "eve.esi_base",
        "eve.compatibility_date",
        "eve.user_agent",
        "eve.contact",
        "eve.max_concurrency",
        "eve.timeout_s",
        "web.session_days",
        "web.cookie_secure",
        "web.stale_after_s",
        "web.refresh_interval_s",
        "web.max_wait_age_s",
        "structures.enabled",
        "alerts.enabled",
        "alerts.feed_url",
        "alerts.max_per_poll",
        "alerts.contact",
        "notifications.enabled",
        "notifications.zkb_check.enabled",
        "notifications.zkb_check.api_enabled",
        "notifications.zkb_check.api_url",
        "notifications.zkb_check.min_api_gap_s",
        "evescout.enabled",
        "ansiblex.enabled",
    },
    "wars": {
        "enabled",
        "alliance_member_count_max_corps",
        "structures_enabled",
        "structures_url",
        "structures_refresh_hours",
        "census_enabled",
        "contact",
        "public_url",
    },
}
TITLES = {
    "evedm:": "General",
    "evedm:discord": "Discord bot",
    "evedm:eve": "EVE application and ESI",
    "evedm:web": "Web",
    "evedm:structures": "Structure scout",
    "evedm:alerts": "Structure alerts",
    "evedm:alerts.discord": "Structure alerts on Discord",
    "evedm:notifications": "Notifications",
    "evedm:notifications.discord": "Notifications on Discord",
    "evedm:notifications.zkb_check": "Notifications: zKillboard check",
    "evedm:access": "Access (members-only tabs)",
    "evedm:evescout": "EVE-Scout (Thera / Turnur)",
    "evedm:ansiblex": "Ansiblex jump gates",
    "wars:": "Wars",
    "wars:discord": "Wars on Discord",
}
_COMMENT = re.compile(r"\s{4}(\w+):[^#]*#\s*(.+)$")
_HEADER = (
    "# Written by the Settings page of EVE Distance Meter: the values changed there. They win over the\n"
    "# configuration files and the environment. Delete this file to return to the configuration files.\n"
)


@dataclasses.dataclass(frozen=True)
class Field:
    file: str
    path: str
    kind: str  # bool | int | float | str | secret | list | rules | readonly
    of_int: bool  # a list of numbers
    help: str

    @property
    def key(self) -> str:
        return f"{self.file}:{self.path}"

    @property
    def locked(self) -> bool:
        return self.kind == "readonly" or self.path in LOCKED[self.file]

    @property
    def restart(self) -> bool:
        return self.path in RESTART[self.file]


def _comments(cls: type) -> dict[str, str]:
    """The trailing comment of each field in the dataclass source: the help text of the page."""
    out: dict[str, str] = {}
    try:
        source = inspect.getsource(cls)
    except (OSError, TypeError):
        return out
    for line in source.splitlines():
        m = _COMMENT.match(line)
        if m:
            text = m.group(2).strip()
            if text.startswith("noqa"):
                text = text.partition(" - ")[2]
            out[m.group(1)] = text
    return out


def _fields(obj: Any, file: str, prefix: str = "") -> Iterator[Field]:
    comments = _comments(type(obj))
    for f in dataclasses.fields(obj):
        value, path = getattr(obj, f.name), prefix + f.name
        if dataclasses.is_dataclass(value):
            yield from _fields(value, file, path + ".")
            continue
        if path in HIDDEN[file]:
            continue
        declared = str(f.type).replace(" ", "")
        kind = declared if declared in ("bool", "int", "float", "str") else "readonly"
        if declared.startswith("list["):
            kind = "list"
        if path in SECRET[file]:
            kind = "secret"
        if path in RULES[file]:
            kind = "rules"
        yield Field(file, path, kind, declared == "list[int]", comments.get(f.name, ""))


def _get(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _set(obj: Any, path: str, value: Any) -> None:
    *head, last = path.split(".")
    for part in head:
        obj = getattr(obj, part)
    setattr(obj, last, value)


def _coerce(f: Field, value: Any) -> Any:
    """The value in the field's type; ValueError for anything that is not one."""
    if f.kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("expected true or false")
    if f.kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("expected a number")
        number = float(value)
        if f.kind == "float":
            return number
        if not number.is_integer():
            raise ValueError("expected a whole number")
        return int(number)
    if f.kind in ("str", "secret"):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("expected text")
        return str(value).strip()
    if f.kind == "list":
        items = value if isinstance(value, list) else str(value).split(",")
        items = [str(v).strip() for v in items if str(v).strip()]
        return [int(v) for v in items] if f.of_int else items
    if f.kind == "rules":
        try:
            return normalize_all(value, list(main_config.NOTIFICATION_GROUPS))
        except RuleError as exc:
            raise ValueError(str(exc)) from None
    raise ValueError("this setting cannot be changed here")


class SettingsAdmin:
    def __init__(self, settings: Settings, ws: WarsSettings):
        self.live: dict[str, Any] = {"evedm": settings, "wars": ws}
        self.base: dict[str, Any] = {"evedm": copy.deepcopy(settings), "wars": copy.deepcopy(ws)}
        self.path = Path(settings.data_dir) / "settings.yaml"
        self.fields: dict[str, Field] = {f.key: f for file in FILES for f in _fields(self.base[file], file)}
        self.overrides: dict[str, Any] = {}  # key -> value, secrets in the clear (memory only)
        self.note: str | None = None
        self._env = {
            "evedm": {".".join(path): env for env, path in main_config.ENV_MAP.items()},
            "wars": {".".join(path): env for env, path in wars_config.ENV_MAP.items()},
        }

    # -- layers --------------------------------------------------------------------------------------
    def _apply_to(self, objs: dict[str, Any], overrides: dict[str, Any]) -> None:
        for key, f in self.fields.items():
            if f.locked:
                continue
            value = overrides[key] if key in overrides else _get(self.base[f.file], f.path)
            if _get(objs[f.file], f.path) != value:
                _set(objs[f.file], f.path, copy.deepcopy(value))
        # one bot for everything: the wars watcher posts with the same token
        objs["wars"].discord.bot_token = objs["evedm"].discord.bot_token or self.base["wars"].discord.bot_token

    @staticmethod
    def _validate(objs: dict[str, Any]) -> None:
        objs["evedm"].validate_for_serve()
        objs["wars"].validate()

    def load(self) -> None:
        """Start-up: put the file on top of the configuration. ConfigError only when the configuration is
        invalid without the file too: a file that breaks it is ignored, so the page stays reachable."""
        saved = self._read_file() if self.live["evedm"].secret_key else {}
        if saved:
            self._apply_to(self.live, saved)
            try:
                self._validate(self.live)
            except ConfigError as exc:
                log.error("%s is ignored, it makes the configuration invalid: %s", self.path, exc)
                self.note = f"The saved settings were ignored at start-up: {exc}"
            else:
                self.overrides = saved
                log.info("%d setting(s) taken from %s", len(saved), self.path)
                return
        self._apply_to(self.live, {})
        self._switch_off_unusable_discord()
        self._validate(self.live)

    def _switch_off_unusable_discord(self) -> None:
        """A configuration written for webhooks (Discord enabled, no bot token or channel ID) must not stop the
        app: Discord posting of that feature is switched off with a warning, and the administrator fixes it on
        the Settings page. Saving the same combination from the page is still refused."""
        main, wars = self.live["evedm"], self.live["wars"]
        off = []
        a = main.alerts
        alerts_ready = bool(main.discord.bot_token) and main_config.is_snowflake(a.discord.channel_id)
        if a.enabled and a.discord.enabled and not alerts_ready:
            a.discord.enabled = False
            off.append("alerts (needs the bot token and alerts.discord.channel_id)")
        d = wars.discord
        if wars.enabled and d.enabled and not d.dry_run and not (d.bot_token and str(d.channel_id or "").strip()):
            d.enabled = False
            off.append("wars (needs the bot token and wars discord.channel_id)")
        if off:
            what = ", ".join(off)
            log.warning("Discord posting is switched off for %s: set EVEDM_DISCORD_BOT_TOKEN and the channel IDs", what)
            self.note = f"Discord posting was switched off at start-up for {what}; set them below."

    # -- the file ------------------------------------------------------------------------------------
    def _cipher(self) -> TokenCipher:
        return TokenCipher(self.live["evedm"].secret_key)

    def _leaves(self, file: str, node: Any, prefix: str = "") -> Iterator[tuple[Field, Any]]:
        if not isinstance(node, dict):
            return
        for name, value in node.items():
            f = self.fields.get(f"{file}:{prefix}{name}")
            if f is not None:
                yield f, value
            else:
                yield from self._leaves(file, value, f"{prefix}{name}.")

    def _read_file(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.error("%s cannot be read and is ignored: %s", self.path, exc)
            self.note = "The saved settings could not be read at start-up and were ignored."
            return {}
        out: dict[str, Any] = {}
        for file in FILES:
            for f, value in self._leaves(file, raw.get(file) if isinstance(raw, dict) else None):
                if f.locked:
                    continue
                try:
                    if f.kind == "secret" and str(value).startswith(ENCRYPTED):
                        value = self._cipher().decrypt(str(value)[len(ENCRYPTED) :].encode("ascii"))
                    out[f.key] = _coerce(f, value)
                except (ValueError, TypeError) as exc:
                    log.error("%s: %s is ignored (%s)", self.path, f.path, exc)
        return out

    def _write_file(self, overrides: dict[str, Any]) -> None:
        tree: dict[str, Any] = {}
        for key in sorted(overrides):
            f, value = self.fields[key], overrides[key]
            if f.kind == "secret":
                value = ENCRYPTED + self._cipher().encrypt(value).decode("ascii")
            node = tree.setdefault(f.file, {})
            *head, last = f.path.split(".")
            for part in head:
                node = node.setdefault(part, {})
            node[last] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(_HEADER + yaml.safe_dump(tree, sort_keys=True, allow_unicode=True), encoding="utf-8")
        with contextlib.suppress(OSError):  # not every filesystem has modes (a Windows bind mount)
            tmp.chmod(0o600)
        os.replace(tmp, self.path)

    # -- the page ------------------------------------------------------------------------------------
    def _editable(self, key: Any) -> Field:
        f = self.fields.get(key) if isinstance(key, str) else None
        if f is None:
            raise ConfigError(f"unknown setting: {key}")
        if f.locked:
            raise ConfigError(f"{f.path} cannot be changed here: it is fixed in the configuration files")
        return f

    def save(self, changes: dict[str, Any], reset: list[Any], admin_id: int) -> dict[str, list[str]]:
        """Validate the whole configuration with the changes, write the file, change the running settings.
        ConfigError (nothing saved, nothing changed) when a value or the result is not valid."""
        new = dict(self.overrides)
        for key in reset:
            new.pop(self._editable(key).key, None)
        for key, value in changes.items():
            f = self._editable(key)
            try:
                value = _coerce(f, value)
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"{f.path}: {exc}") from None
            if f.kind == "secret" and not value:
                raise ConfigError(f"{f.path}: empty; use Reset to return to the configured value")
            if value == _get(self.base[f.file], f.path):
                new.pop(key, None)  # the same as the configuration files: nothing to keep
            else:
                new[key] = value
        candidate = copy.deepcopy(self.base)
        self._apply_to(candidate, new)
        try:
            self._validate(candidate)
        except (ValueError, TypeError) as exc:
            raise ConfigError(str(exc)) from exc
        allowed = [int(v) for v in candidate["evedm"].web.allowed_login_characters]
        if allowed and admin_id not in allowed:
            raise ConfigError(f"web.allowed_login_characters must keep your own character ({admin_id})")
        before = {key: copy.deepcopy(_get(self.live[f.file], f.path)) for key, f in self.fields.items()}
        self._write_file(new)
        self.overrides = new
        self._apply_to(self.live, new)
        self._validate(self.live)  # also brings IDs into their stored form
        self.note = None
        changed = [key for key, f in self.fields.items() if _get(self.live[f.file], f.path) != before[key]]
        return {"changed": changed, "restart_needed": [key for key in changed if self.fields[key].restart]}

    def state(self) -> dict[str, Any]:
        groups: dict[str, dict[str, Any]] = {}
        for key, f in self.fields.items():
            section = f.path.rpartition(".")[0]
            gid = f"{f.file}:{section}"
            group = groups.setdefault(
                gid, {"id": gid, "title": TITLES.get(gid, section), "source": SOURCES[f.file], "fields": []}
            )
            value, base = _get(self.live[f.file], f.path), _get(self.base[f.file], f.path)
            item: dict[str, Any] = {
                "key": key,
                "label": f.path.rpartition(".")[2],
                "type": f.kind,
                "value": value,
                "base": base,
                "overridden": key in self.overrides,
                "locked": f.locked,
                "restart": f.restart,
                "help": f.help,
                "env": self._env[f.file].get(f.path, ""),
            }
            if f.kind == "secret":
                item |= {"value": None, "base": None, "is_set": bool(value), "base_is_set": bool(base)}
            elif f.kind == "readonly":
                item |= {"value": "" if value is None else str(value), "base": "" if base is None else str(base)}
            group["fields"].append(item)
        rank = lambda g: 0 if g["id"] == "evedm:discord" else 1 if "discord" in g["id"] else 2  # noqa: E731
        ordered = sorted(groups.values(), key=rank)  # the bot, then the Discord sections, then the rest as declared
        return {"file": str(self.path), "note": self.note, "groups": ordered}
