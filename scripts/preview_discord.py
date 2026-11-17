"""Renders the Discord messages of a few sample notifications into one HTML page that looks like Discord, so the
layout can be checked without posting. The embeds come from ``evedm.notifications.discord_embed``, the same code the
bot posts with.

    .venv\\Scripts\\python scripts\\preview_discord.py [out.html] [--site https://evedm.example]

Writes ``discord-preview.html`` in the current folder by default and prints the path.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from evedm.notifications import discord_embed, title_of  # noqa: E402

GROUPS = {
    "StructureUnderAttack": "structure_attack",
    "StructureLostShields": "structure_attack",
    "StructureItemsMovedToSafety": "asset_safety",
    "TowerAlertMsg": "tower",
}


def _system(name: str, security: float, region: str) -> dict:
    return {"name": name, "security": security, "region": region}


def samples(now: datetime) -> list[dict]:
    """(describe-like dict, recipients, space, hub, role) of each sample message."""

    def at(minutes: int) -> str:
        return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def d(ntype: str, system: dict | None, minutes: int, **kw) -> dict:  # noqa: ANN003
        return {
            "type": ntype,
            "group": GROUPS.get(ntype, "other"),
            "title": title_of(ntype, system["name"] if system else ""),
            "timestamp": at(minutes),
            "system": system,
            "structure_name": None,
            "structure_type": None,
            "show_structure_type": False,
            "zkb": None,
            **kw,
        }

    kill = "https://zkillboard.com/kill/128734512/"
    return [
        {
            "label": "Structure attack, named structure, Thera closer, role ping",
            "d": d(
                "StructureUnderAttack",
                _system("Amarr", 1.0, "Domain"),
                2,
                structure_name="Amarr - Freeport Trade Hub",
                structure_type="Astrahus",
                show_structure_type=True,
            ),
            "who": ["Shadowed Mio", "Mio Alt One"],
            "space": "high",
            "hub": {"hub": "Thera", "jumps": 5, "entry": {"name": "Sarum Prime"}},
            "role": "123456789012345678",
        },
        {
            "label": "Asset safety, kill found on zKillboard",
            "d": d(
                "StructureItemsMovedToSafety",
                _system("Tama", 0.3, "The Citadel"),
                9,
                structure_name="Tama - Pirate Den",
                structure_type="Raitaru",
                show_structure_type=True,
                zkb={
                    "status": "found",
                    "text": f"zKillboard: Raitaru destroyed in Tama at {(now - timedelta(minutes=10)):%H:%M}",
                    "url": kill,
                },
            ),
            "who": ["Mio Alt Two"],
            "space": "low",
            "hub": {"hub": "Turnur", "jumps": 3, "entry": {"name": "Nourvukaiken"}},
            "role": None,
        },
        {
            "label": "Asset safety, check still running, structure not known",
            "d": d(
                "StructureItemsMovedToSafety",
                _system("C6CG-W", -0.4, "Perrigen Falls"),
                1,
                show_structure_type=True,
                zkb={"status": "pending", "text": "zKillboard: checking… (2 of 6)", "url": None},
            ),
            "who": ["Mio Alt Three", "Mio Alt Four", "Mio Alt Five"],
            "space": "null",
            "hub": None,
            "role": None,
        },
        {
            "label": "Wormhole space: no gates, so the Notifications page instead of Find route",
            "d": d(
                "StructureLostShields",
                _system("J123605", -1.0, "C-R00005"),
                30,
                structure_type="Fortizar",
                show_structure_type=True,
            ),
            "who": ["Mio Scanner"],
            "space": "wormhole",
            "hub": None,
            "role": None,
        },
    ]


_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def markdown(text: str) -> str:
    """The bits of Discord markdown the embeds use: links, bold, line breaks."""
    out = html.escape(text, quote=False)
    out = _LINK.sub(lambda m: f'<a href="{html.escape(m[2])}" target="_blank" rel="noopener">{m[1]}</a>', out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return out.replace("\n", "<br>")


def message(sample: dict, embed: dict, payload: dict) -> str:
    color = f"#{embed['color']:06x}"
    raw = html.escape(json.dumps(payload, indent=2, ensure_ascii=False))
    when = datetime.fromisoformat(embed["timestamp"].replace("Z", "+00:00"))
    title = html.escape(embed["title"])
    if embed.get("url"):
        title = f'<a href="{html.escape(embed["url"])}" target="_blank" rel="noopener">{title}</a>'
    ping = '<div class="content"><span class="mention">@role</span></div>' if payload.get("content") else ""
    return f"""
<section>
  <div class="label">{html.escape(sample["label"])}</div>
  <div class="msg">
    <div class="avatar">DM</div>
    <div class="body">
      <div class="head"><span class="name">EVE Distance Meter</span><span class="bot">APP</span>
        <span class="stamp">Today at {when.astimezone():%H:%M}</span></div>
      {ping}
      <div class="embed" style="border-left-color:{color}">
        <div class="title">{title}</div>
        <div class="desc">{markdown(embed["description"])}</div>
        <div class="footer">{html.escape(embed["footer"]["text"])} • {when.astimezone():%d.%m.%Y %H:%M}</div>
      </div>
    </div>
  </div>
  <details><summary>payload JSON</summary><pre>{raw}</pre></details>
</section>"""


CSS = """
body{margin:0;background:#313338;color:#dbdee1;font:16px/1.375 "gg sans","Noto Sans","Helvetica Neue",Arial,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;color:#f2f3f5;margin:0 0 4px}
p.note{color:#949ba4;font-size:14px;margin:0 0 20px}
section{margin:0 0 28px}
.label{color:#949ba4;font-size:12px;text-transform:uppercase;letter-spacing:.02em;margin:0 0 6px 56px}
.msg{display:flex;gap:16px}
.avatar{flex:0 0 40px;height:40px;border-radius:50%;background:#5865f2;color:#fff;display:grid;place-items:center;
  font-weight:600;font-size:14px}
.head{display:flex;align-items:center;gap:6px}
.name{color:#f2f3f5;font-weight:500}
.bot{background:#5865f2;color:#fff;font-size:10px;font-weight:600;padding:0 4px;border-radius:3px;line-height:15px}
.stamp{color:#949ba4;font-size:12px;margin-left:4px}
.content{margin:2px 0}
.mention{background:rgba(88,101,242,.3);color:#c9cdfb;border-radius:3px;padding:0 2px;font-weight:500}
.embed{margin-top:4px;background:#2b2d31;border-left:4px solid;border-radius:4px;padding:8px 16px 12px 12px;
  max-width:520px;font-size:14px;line-height:1.45}
.title{font-weight:600;color:#f2f3f5;margin:4px 0 8px;font-size:16px}
.title a,.desc a{color:#00a8fc;text-decoration:none}
.title a:hover,.desc a:hover{text-decoration:underline}
.desc strong{color:#f2f3f5}
.footer{color:#949ba4;font-size:12px;margin-top:8px}
details{margin:6px 0 0 56px;color:#949ba4;font-size:12px}
pre{background:#1e1f22;padding:8px;border-radius:4px;overflow:auto;color:#dbdee1}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", nargs="?", default="discord-preview.html")
    parser.add_argument("--site", default="https://evedm.capstext.space", help="public root of the site")
    args = parser.parse_args()
    site = args.site.rstrip("/")
    now = datetime.now(UTC)
    parts = []
    for s in samples(now):
        embed = discord_embed(
            s["d"], s["who"], space=s["space"], hub=s["hub"], page_url=f"{site}/notifications", site_url=site
        )
        payload: dict = {"embeds": [embed]}
        if s["role"]:
            payload["content"] = f"<@&{s['role']}>"
        parts.append(message(s, embed, payload))
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Discord preview · EVE Distance Meter</title>
<style>{CSS}</style></head><body><main>
<h1>Discord notification preview</h1>
<p class="note">Rendered {now:%Y-%m-%d %H:%M} EVE time from <code>discord_embed</code>.
Discord cannot colour embed text, so the key values are bold. A rule's role is pinged on top (shown as @role).</p>
{"".join(parts)}
</main></body></html>
"""
    out = Path(args.out)
    out.write_text(page, encoding="utf-8")
    print(out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
