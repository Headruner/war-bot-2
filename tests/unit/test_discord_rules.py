"""Discord rules: a filter plus a channel each; what matches several rules goes to each of their channels."""

from __future__ import annotations

import pytest

from evedm.config import NOTIFICATION_GROUPS, ConfigError, Settings
from evedm.db import Database
from evedm.discord_rules import (
    DEFAULTS,
    RuleError,
    Target,
    alert_matches,
    normalize,
    normalize_all,
    notification_matches,
    of_source,
    place_matches,
    targets,
)

GROUPS = list(NOTIFICATION_GROUPS)
JITA = {"name": "Jita", "region": "The Forge", "space": "high"}
TAMA = {"name": "Tama", "region": "The Citadel", "space": "low"}


def rule(**kw) -> dict:
    return normalize({"source": "alerts", "channel_id": "1"} | kw, 1, GROUPS)


def test_a_rule_is_brought_into_one_form():
    r = rule(name="  Null-sec  ", systems="Jita, Tama ,Jita", owners=["98000001", 99000001], min_isk="5e9")
    assert list(r) == list(DEFAULTS)  # every key, one order: rules compare and store the same way
    assert r["name"] == "Null-sec" and r["systems"] == ["Jita", "Tama"] and r["owners"] == [98000001, 99000001]
    assert r["min_isk"] == 5e9 and r["enabled"] is True and r["kinds"] == [] and r["channel_id"] == "1"
    assert normalize_all(None, GROUPS) == [] and normalize_all([r], GROUPS) == [r]
    assert rule(channel_id=555000111222333444)["channel_id"] == "555000111222333444"  # YAML reads it as a number


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"source": "wars"}, "source must be one of"),
        ({"channel_id": ""}, "channel_id must be a numeric"),
        ({"channel_id": "#alerts"}, "channel_id must be a numeric"),
        ({"mention_role_id": "@fc"}, "mention_role_id"),
        ({"kinds": ["boom"]}, r"kinds: unknown \['boom'\]"),
        ({"space": ["pochven"]}, "space: unknown"),
        ({"groups": ["nope"]}, "groups: unknown"),
        ({"owners": ["Goonswarm"]}, "owners must be corporation or alliance IDs"),
        ({"min_isk": -1}, "min_isk"),
        ({"min_isk": "a lot"}, "min_isk"),
        ({"enabled": "yes"}, "enabled must be true or false"),
        ({"colour": "red"}, "unknown key"),
        ({"systems": {"a": 1}}, "systems must be a list"),
    ],
)
def test_a_wrong_rule_is_refused_by_name(raw, message):
    with pytest.raises(RuleError, match=message) as exc:
        normalize({"source": "alerts", "channel_id": "1", "name": "mine"} | raw, 3, GROUPS)
    assert str(exc.value).startswith("rule 3 (mine)")
    with pytest.raises(RuleError, match="must be a list"):
        normalize_all({"source": "alerts"}, GROUPS)
    with pytest.raises(RuleError, match="at most"):
        normalize_all([{}] * 51, GROUPS)
    with pytest.raises(RuleError, match="rule 1: must be a mapping"):
        normalize_all(["alerts"], GROUPS)


def test_rules_in_the_configuration_file_are_validated_at_start():
    s = Settings()
    s.secret_key, s.demo = "k", True
    s.discord.rules = [{"source": "notifications", "channel_id": 42, "groups": ["asset_safety"]}]
    s.validate_for_serve()
    assert s.discord.rules[0]["channel_id"] == "42" and s.discord.rules[0]["types"] == []
    s.discord.rules = [{"source": "notifications", "channel_id": "x"}]
    with pytest.raises(ConfigError, match=r"discord.rules: rule 1: channel_id"):
        s.validate_for_serve()


def test_place_filters():
    assert place_matches(rule(), JITA) and place_matches(rule(), None)  # no place asked for
    assert place_matches(rule(space=["high", "low"]), JITA) and not place_matches(rule(space=["null"]), JITA)
    assert place_matches(rule(regions=["the forge"]), JITA) and not place_matches(rule(regions=["Domain"]), JITA)
    assert place_matches(rule(systems=["jita", "Amarr"]), JITA) and not place_matches(rule(systems=["Amarr"]), JITA)
    not_jita = rule(exclude_systems=["Jita"])
    assert not place_matches(not_jita, JITA) and place_matches(not_jita, TAMA)
    assert not place_matches(rule(exclude_regions=["The Forge"]), JITA)
    assert not place_matches(rule(space=["high"], exclude_systems=["Jita"]), JITA)  # an exclusion always wins
    assert not place_matches(rule(space=["high"]), None)  # a place is asked for, the item has none
    assert place_matches(rule(exclude_regions=["The Forge"]), None)


def test_alert_filters():
    kill = {"kind": "destroyed", "isk": 2e9, "core_drop": 1, "capitals": 0}
    fight = {"kind": "combat", "isk": 8e8, "core_drop": 0, "capitals": 1}
    owner = {98000001, 99000001}  # the corporation and its alliance
    assert alert_matches(rule(), kill, JITA, owner) and alert_matches(rule(), fight, None, set())
    assert alert_matches(rule(kinds=["destroyed", "firing"]), kill, JITA, owner)
    assert not alert_matches(rule(kinds=["destroyed"]), fight, JITA, owner)
    big = rule(min_isk=1e9)
    assert alert_matches(big, kill, JITA, owner) and not alert_matches(big, fight, JITA, owner)
    assert alert_matches(rule(core_drops_only=True), kill, JITA, owner)
    assert not alert_matches(rule(core_drops_only=True), fight, JITA, owner)
    assert alert_matches(rule(capitals_only=True), fight, JITA, owner)
    assert not alert_matches(rule(capitals_only=True), kill, JITA, owner)
    assert alert_matches(rule(owners=[99000001]), kill, JITA, owner)  # by alliance
    assert not alert_matches(rule(owners=[98000009]), kill, JITA, owner)
    assert not alert_matches(rule(owners=[98000001]), kill, JITA, set())  # owner unknown: not theirs
    assert not alert_matches(rule(exclude_owners=[98000001]), kill, JITA, owner)
    assert alert_matches(rule(exclude_owners=[98000001]), kill, JITA, set())
    assert not alert_matches(rule(kinds=["destroyed"], space=["null"]), kill, JITA, owner)  # every filter must pass


def test_notification_filters():
    n = dict(source="notifications")
    assert notification_matches(rule(**n), "InsurancePayoutMsg", "other", None)  # nothing asked: everything
    attack = rule(**n, groups=["structure_attack"])
    assert notification_matches(attack, "StructureUnderAttack", "structure_attack", JITA)
    assert not notification_matches(attack, "StructureItemsMovedToSafety", "asset_safety", JITA)
    either = rule(**n, groups=["structure_attack"], types=["StructureItemsMovedToSafety"])  # a group or a single type
    assert notification_matches(either, "StructureItemsMovedToSafety", "asset_safety", JITA)
    assert not notification_matches(either, "StructureFuelAlert", "structure_state", JITA)
    assert not notification_matches(rule(**n, regions=["Domain"]), "StructureUnderAttack", "structure_attack", JITA)


def test_one_message_per_channel_and_the_first_role_named():
    rules = [
        rule(channel_id="11"),
        rule(channel_id="22", mention_role_id="7"),
        rule(channel_id="11", mention_role_id="9"),
        rule(channel_id="11", mention_role_id="5"),
        rule(channel_id="33", enabled=False),
        rule(source="notifications", channel_id="44"),
    ]
    assert targets(of_source(rules, "alerts")) == [Target("11", "9"), Target("22", "7")]
    assert targets([]) == [] and [r["channel_id"] for r in of_source(rules, "notifications")] == ["44"]


def test_a_message_per_event_and_channel_is_remembered(tmp_path):
    db = Database(tmp_path / "evedm.sqlite3")
    key = "StructureUnderAttack:1000000000001:2026-11-13T10:00:00Z"
    assert db.notification_post(key) is None and db.notification_post(key, "11") is None
    db.save_notification_post(key, "m1", ["Alpha"])  # the single channel, as before the rules
    assert db.notification_post(key)["message_id"] == "m1"
    assert db.notification_post(key, "11")["message_id"] == "m1"  # no message there yet: the old one is tried first
    db.save_notification_post(key, "m2", ["Alpha", "Beta"], "11")
    db.save_notification_post(key + "0", "m9", [], "11")  # another event
    assert db.notification_post(key, "11")["message_id"] == "m2" and db.notification_post(key)["message_id"] == "m1"
    assert db.notification_post(key, "22")["message_id"] == "m1"
    assert sorted((c, r["message_id"]) for c, r in db.notification_posts_of(key)) == [("", "m1"), ("11", "m2")]
    db.close()
