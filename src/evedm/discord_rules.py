"""Discord rules: which structure alerts and character notifications go to which channel.

A rule is a filter plus a channel (``discord.rules`` in evedm.yaml, edited on the Settings page). Whatever matches
several rules is posted to each of their channels, once per channel. While a source (alerts, notifications) has no
rule, its single-channel settings (``alerts.discord``, ``notifications.discord``) are used as before; with at least
one rule, even a switched-off one, only the rules count for that source. An empty list in a filter means "any".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SOURCES = ("alerts", "notifications")
KINDS = ("destroyed", "combat", "firing")
SPACES = ("high", "low", "null", "wormhole")
MAX_RULES = 50
DEFAULTS: dict[str, Any] = {
    "name": "",
    "source": "alerts",
    "enabled": True,
    "channel_id": "",
    "mention_role_id": "",  # pinged with every message of the rule
    "space": [],
    "regions": [],
    "exclude_regions": [],
    "systems": [],
    "exclude_systems": [],
    # alerts only
    "kinds": [],
    "min_isk": 0.0,
    "core_drops_only": False,
    "capitals_only": False,
    "owners": [],  # corporation or alliance IDs of the structure's owner
    "exclude_owners": [],
    # notifications only
    "groups": [],
    "types": [],
}
_NAME_LISTS = ("regions", "exclude_regions", "systems", "exclude_systems", "types")
_ID_LISTS = ("owners", "exclude_owners")


class RuleError(ValueError):
    """A rule is not valid; the message names the rule."""


@dataclass(frozen=True)
class Target:
    """One channel an item goes to. ``channel`` None: the notifier's own channel (the single-channel settings),
    where the role is pinged as it always was (``role`` None); a rule pings its role with every message."""

    channel: str | None
    role: str | None


def _items(value: Any, what: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise RuleError(f"{what} must be a list")
    return list(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))


def _snowflake(value: Any, what: str, required: bool) -> str:
    text = "" if value is None else str(value).strip()
    if (text or required) and not text.isdigit():
        raise RuleError(f"{what} must be a numeric Discord ID")
    return text


def normalize(raw: Any, number: int, groups: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """One rule with every key in its type; RuleError says which rule and what is wrong."""
    label = f"rule {number}"
    if not isinstance(raw, dict):
        raise RuleError(f"{label}: must be a mapping")
    name = str(raw.get("name") or "").strip()[:60]
    label = f"rule {number} ({name})" if name else label
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise RuleError(f"{label}: unknown key(s) {unknown}")
    try:
        rule: dict[str, Any] = {"name": name, "source": str(raw.get("source") or "").strip()}
        if rule["source"] not in SOURCES:
            raise RuleError(f"source must be one of {list(SOURCES)}")
        for key in ("enabled", "core_drops_only", "capitals_only"):
            value = raw.get(key, DEFAULTS[key])
            if not isinstance(value, bool):
                raise RuleError(f"{key} must be true or false")
            rule[key] = value
        rule["channel_id"] = _snowflake(raw.get("channel_id"), "channel_id", required=True)
        rule["mention_role_id"] = _snowflake(raw.get("mention_role_id"), "mention_role_id", required=False)
        for key, allowed in (("space", SPACES), ("kinds", KINDS), ("groups", tuple(groups))):
            rule[key] = _items(raw.get(key), key)
            bad = [v for v in rule[key] if v not in allowed]
            if bad:
                raise RuleError(f"{key}: unknown {bad}; use {list(allowed)}")
        for key in _NAME_LISTS:
            rule[key] = _items(raw.get(key), key)
        for key in _ID_LISTS:
            ids = _items(raw.get(key), key)
            if not all(v.isdigit() for v in ids):
                raise RuleError(f"{key} must be corporation or alliance IDs (numbers)")
            rule[key] = [int(v) for v in ids]
        value = raw.get("min_isk") or 0
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise RuleError("min_isk must be a number")
        rule["min_isk"] = float(value)
        if rule["min_isk"] < 0:
            raise RuleError("min_isk must be 0 or more")
    except RuleError as exc:
        raise RuleError(f"{label}: {exc}") from None
    except ValueError:
        raise RuleError(f"{label}: min_isk must be a number") from None
    return {key: rule[key] for key in DEFAULTS}  # one key order: rules compare and store the same way


def normalize_all(raw: Any, groups: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise RuleError("discord.rules must be a list of rules")
    if len(raw) > MAX_RULES:
        raise RuleError(f"discord.rules: at most {MAX_RULES} rules")
    return [normalize(r, i, groups) for i, r in enumerate(raw, 1)]


def of_source(rules: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    return [r for r in rules if r.get("source") == source]


def _lower(values: list[str]) -> set[str]:
    return {v.lower() for v in values}


def place_matches(rule: dict[str, Any], place: dict[str, Any] | None) -> bool:
    """``place``: {name, region, space} of the solar system, or None when the item has none. An item without a
    place passes only a rule that asks for no place."""
    wanted = rule["space"] or rule["regions"] or rule["systems"]
    if place is None:
        return not wanted
    name, region = str(place.get("name") or "").lower(), str(place.get("region") or "").lower()
    if name in _lower(rule["exclude_systems"]) or region in _lower(rule["exclude_regions"]):
        return False
    if rule["space"] and place.get("space") not in rule["space"]:
        return False
    if rule["regions"] and region not in _lower(rule["regions"]):
        return False
    return not rule["systems"] or name in _lower(rule["systems"])


def alert_matches(rule: dict[str, Any], alert: dict[str, Any], place: dict[str, Any] | None, owners: set[int]) -> bool:
    """``alert``: kind, isk, core_drop, capitals. ``owners``: the owner corporation and the alliances it is in."""
    if rule["kinds"] and alert["kind"] not in rule["kinds"]:
        return False
    if float(alert.get("isk") or 0) < rule["min_isk"]:
        return False
    if rule["core_drops_only"] and not alert.get("core_drop"):
        return False
    if rule["capitals_only"] and not alert.get("capitals"):
        return False
    if owners & set(rule["exclude_owners"]) or (rule["owners"] and not owners & set(rule["owners"])):
        return False
    return place_matches(rule, place)


def notification_matches(rule: dict[str, Any], ntype: str, group: str, place: dict[str, Any] | None) -> bool:
    if (rule["groups"] or rule["types"]) and group not in rule["groups"] and ntype not in rule["types"]:
        return False
    return place_matches(rule, place)


def targets(matching: list[dict[str, Any]]) -> list[Target]:
    """One target per channel of the enabled rules given (they all matched): the first rule with a role names it."""
    by_channel: dict[str, str] = {}
    for rule in matching:
        if rule["enabled"]:
            by_channel[rule["channel_id"]] = by_channel.get(rule["channel_id"]) or rule["mention_role_id"]
    return [Target(channel, role) for channel, role in by_channel.items()]
