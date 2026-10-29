"""Rich Discord embed builder."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .source import parse_esi_time

COLOR_DEFAULT = 0xC0392B   # red
COLOR_MUTUAL = 0x9B59B6    # purple
COLOR_ALLIES = 0xF39C12    # orange

TITLE_LIMIT = 256
FIELD_LIMIT = 1024
MAX_EMBEDS_PER_MESSAGE = 10

STATUS_TEXT = {
    "mutual": "Mutual war",
    "open_for_allies": "Open for allies",
    "none": "War declared",
}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _when(value: Optional[str]) -> Optional[str]:
    """ESI timestamp -> Discord timestamp tag (shown in each reader's local time)."""
    dt = parse_esi_time(value)
    return f"<t:{int(dt.timestamp())}:f>" if dt else None


def _isk(value: Optional[float]) -> str:
    v = float(value or 0)
    for unit, size in (("t", 1e12), ("b", 1e9), ("m", 1e6), ("k", 1e3)):
        if v >= size:
            return f"{v / size:.1f}{unit}"
    return f"{v:.0f}"


def _logo(party: Any, size: int = 128) -> Optional[str]:
    if not getattr(party, "entity_id", None):
        return None
    kind = "alliances" if party.type == "alliance" else "corporations"
    return f"https://images.evetech.net/{kind}/{party.entity_id}/logo?size={size}"


def _party_line(party: Any) -> str:
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


def build_embed(war: Any, s: Dict, banner_fname: Optional[str] = None,
                report: Optional[Dict] = None) -> Dict:
    color = {"mutual": COLOR_MUTUAL, "open_for_allies": COLOR_ALLIES}.get(war.status, COLOR_DEFAULT)
    report = report or {}

    fields: List[Dict] = [
        {"name": "🔴 Aggressor", "value": _clip(_party_line(war.aggressor), FIELD_LIMIT)},
        {"name": "🔵 Defender", "value": _clip(_party_line(war.defender), FIELD_LIMIT)},
    ]

    timeline = [("Declared", war.declared_at or report.get("declared")),
                ("Fighting starts", report.get("started")),
                ("Retracted", report.get("retracted")),
                ("Finished", report.get("finished"))]
    lines = [f"{label}: {_when(value)}" for label, value in timeline if _when(value)]
    if lines:
        fields.append({"name": "🕒 Timeline", "value": "\n".join(lines)})

    if report:
        ag, df = report.get("aggressor", {}), report.get("defender", {})
        if report.get("kills_total") or ag.get("ships_killed") or df.get("ships_killed"):
            eff = f" ({ag['efficiency']}%)" if ag.get("efficiency") is not None else ""
            fields.append({"name": "⚔ Score", "value": (
                f"Aggressor: {ag.get('ships_killed', 0)} kills, {_isk(ag.get('isk_destroyed'))} ISK{eff}\n"
                f"Defender: {df.get('ships_killed', 0)} kills, {_isk(df.get('isk_destroyed'))} ISK")})

        allies = ", ".join(a["name"] for a in report.get("allies", []))
        if allies:
            fields.append({"name": "🤝 Allies", "value": _clip(allies, FIELD_LIMIT)})

        hot = ", ".join(name for name, _ in report.get("top_systems", []))
        if hot:
            fields.append({"name": "📍 Hot systems", "value": _clip(hot, FIELD_LIMIT)})

        kills = [f"[{k['ship']}]({k['zkill_url']}) – {k['victim'] or k['victim_corp']} in {k['system']}"
                 for k in report.get("recent_kills", [])]
        if kills:
            fields.append({"name": "💥 Recent kills", "value": _clip("\n".join(kills), FIELD_LIMIT)})

        if report.get("zkill_war_url"):
            fields.append({"name": "🔗 Links", "value": f"[zKillboard]({report['zkill_war_url']})"})

    author = {"name": _clip(f"{war.aggressor.name}" + (f" [{war.aggressor.ticker}]" if war.aggressor.ticker else ""), TITLE_LIMIT),
              "url": war.aggressor.dotlan_url}
    if _logo(war.aggressor, 64):
        author["icon_url"] = _logo(war.aggressor, 64)

    embed: Dict[str, Any] = {
        "title": _clip(f"⚔ {war.aggressor.name} → {war.defender.name}", TITLE_LIMIT),
        "url": war.report_url,
        "color": color,
        "author": author,
        "description": STATUS_TEXT.get(war.status, war.status),
        "fields": fields,
        "footer": {"text": f"Source: ESI · War #{war.war_id}"},
        "timestamp": war.declared_at or datetime.now(timezone.utc).isoformat(),
    }
    if _logo(war.defender):
        embed["thumbnail"] = {"url": _logo(war.defender)}
    if banner_fname:
        embed["image"] = {"url": f"attachment://{banner_fname}"}
    return embed


def build_payload(embeds: List[Dict], s: Dict, mention_role_id: Optional[str]) -> Dict:
    payload: Dict[str, Any] = {"embeds": embeds[:MAX_EMBEDS_PER_MESSAGE]}
    if mention_role_id:
        payload["content"] = f"<@&{mention_role_id}>"
        payload["allowed_mentions"] = {"roles": [str(mention_role_id)]}
    return payload
