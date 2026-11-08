"""Discord embeds for wars (from eve-war-bot-2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .source import Party, War, parse_esi_time

COLOR_DEFAULT = 0xC0392B  # red
COLOR_MUTUAL = 0x9B59B6  # purple
COLOR_ALLIES = 0xF39C12  # orange

TITLE_LIMIT = 256
FIELD_LIMIT = 1024
MAX_EMBEDS_PER_MESSAGE = 10

STATUS_TEXT = {"mutual": "Mutual war", "open_for_allies": "Open for allies", "none": "War declared"}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _when(value: str | None) -> str | None:
    """ESI timestamp -> Discord timestamp tag (shown in each reader's local time)."""
    dt = parse_esi_time(value)
    return f"<t:{int(dt.timestamp())}:f>" if dt else None


def isk(value: float | None) -> str:
    v = float(value or 0)
    for unit, size in (("t", 1e12), ("b", 1e9), ("m", 1e6), ("k", 1e3)):
        if v >= size:
            return f"{v / size:.1f}{unit}"
    return f"{v:.0f}"


def _logo(party: Party, size: int = 128) -> str | None:
    if not party.entity_id:
        return None
    kind = "alliances" if party.type == "alliance" else "corporations"
    return f"https://images.evetech.net/{kind}/{party.entity_id}/logo?size={size}"


def _party_line(party: Party) -> str:
    line = f"[{party.name}]({party.dotlan_url})"
    if party.ticker:
        line += f" [{party.ticker}]"
    extra = []
    if party.members is not None:
        extra.append(f"{party.members:,} members")
    if party.corporations:
        extra.append(f"{party.corporations} corps")
    if extra:
        line += " · " + ", ".join(extra)
    return line


def _lines_within(lines: list[str], limit: int) -> str:
    """Join lines, dropping the tail with a '+N more' note so the field fits."""
    out: list[str] = []
    for i, line in enumerate(lines):
        rest = len(lines) - i
        candidate = "\n".join([*out, line])
        if len(candidate) + (len(f"\n…+{rest - 1} more") if rest > 1 else 0) > limit:
            out.append(f"…+{rest} more")
            break
        out.append(line)
    return "\n".join(out)


def _structure_value(summary: dict, names: dict) -> str:
    if not summary.get("total"):
        return "No public structures known"
    by_type = sorted(summary["by_type"].items(), key=lambda kv: -kv[1])
    lines = [f"**{summary['total']}** public: " + ", ".join(f"{n}× {names.get(int(t), t)}" for t, n in by_type)]
    if summary.get("corp_count", 1) > 1 and summary.get("corps"):
        corps = " · ".join(f"{names.get(int(c), c)}: {n}" for c, n in summary["corps"])
        more = summary.get("corps_with_structures", 0) - len(summary["corps"])
        lines.append(f"↳ {corps}" + (f" (+{more} more corps)" if more > 0 else ""))
    highsec = summary.get("highsec") or []
    if highsec:
        lines.append(f"__High-sec ({len(highsec)}):__")
        lines += [
            f"• {h['system']} ({h['security']}) – {h['name']} [{names.get(int(h['type_id']), h['type_id'])}]"
            for h in highsec
        ]
    else:
        lines.append("High-sec: none")
    return _lines_within(lines, FIELD_LIMIT)


def build_embed(
    war: War, report: dict | None = None, structures: dict | None = None, page_url: str | None = None
) -> dict:
    color = {"mutual": COLOR_MUTUAL, "open_for_allies": COLOR_ALLIES}.get(war.status, COLOR_DEFAULT)
    report = report or {}
    fields: list[dict] = [
        {"name": "🔴 Aggressor", "value": _clip(_party_line(war.aggressor), FIELD_LIMIT)},
        {"name": "🔵 Defender", "value": _clip(_party_line(war.defender), FIELD_LIMIT)},
    ]
    timeline = [
        ("Declared", war.declared_at or report.get("declared")),
        ("Fighting starts", report.get("started")),
        ("Retracted", report.get("retracted")),
        ("Finished", report.get("finished")),
    ]
    lines = [f"{label}: {_when(value)}" for label, value in timeline if _when(value)]
    if lines:
        fields.append({"name": "🕒 Timeline", "value": "\n".join(lines)})

    if report:
        ag, df = report.get("aggressor", {}), report.get("defender", {})
        if report.get("kills_total") or ag.get("ships_killed") or df.get("ships_killed"):
            eff = f" ({ag['efficiency']}%)" if ag.get("efficiency") is not None else ""
            fields.append(
                {
                    "name": "⚔ Score",
                    "value": f"Aggressor: {ag.get('ships_killed', 0)} kills, {isk(ag.get('isk_destroyed'))} ISK{eff}\n"
                    f"Defender: {df.get('ships_killed', 0)} kills, {isk(df.get('isk_destroyed'))} ISK",
                }
            )
        allies = ", ".join(a["name"] for a in report.get("allies", []))
        if allies:
            fields.append({"name": "🤝 Allies", "value": _clip(allies, FIELD_LIMIT)})
        hot = ", ".join(name for name, _ in report.get("top_systems", []))
        if hot:
            fields.append({"name": "📍 Hot systems", "value": _clip(hot, FIELD_LIMIT)})
        kills = [
            f"[{k['ship']}]({k['zkill_url']}) – {k['victim'] or k['victim_corp']} in {k['system']}"
            for k in report.get("recent_kills", [])
        ]
        if kills:
            fields.append({"name": "💥 Recent kills", "value": _clip("\n".join(kills), FIELD_LIMIT)})

    if structures:
        names = structures.get("names", {})
        for side, label in (("aggressor", "🏗 Aggressor structures"), ("defender", "🏗 Defender structures")):
            if structures.get(side) is not None:
                fields.append({"name": label, "value": _structure_value(structures[side], names)})

    links = []
    if report.get("zkill_war_url"):
        links.append(f"[zKillboard]({report['zkill_war_url']})")
    if page_url:
        links.append(f"[Closest characters]({page_url})")
    if links:
        fields.append({"name": "🔗 Links", "value": " · ".join(links)})

    author = {
        "name": _clip(war.aggressor.name + (f" [{war.aggressor.ticker}]" if war.aggressor.ticker else ""), TITLE_LIMIT),
        "url": war.aggressor.dotlan_url,
    }
    if _logo(war.aggressor, 64):
        author["icon_url"] = _logo(war.aggressor, 64)
    embed: dict[str, Any] = {
        "title": _clip(f"⚔ {war.aggressor.name} → {war.defender.name}", TITLE_LIMIT),
        "url": war.report_url,
        "color": color,
        "author": author,
        "description": STATUS_TEXT.get(war.status, war.status),
        "fields": fields,
        "footer": {"text": f"Source: ESI · War #{war.war_id}"},
        "timestamp": war.declared_at or datetime.now(UTC).isoformat(),
    }
    if _logo(war.defender):
        embed["thumbnail"] = {"url": _logo(war.defender)}
    return embed


def build_payload(embeds: list[dict], mention_role_id: str | None) -> dict:
    payload: dict[str, Any] = {"embeds": embeds[:MAX_EMBEDS_PER_MESSAGE]}
    if mention_role_id:
        payload["content"] = f"<@&{mention_role_id}>"
        payload["allowed_mentions"] = {"roles": [str(mention_role_id)]}
    return payload
