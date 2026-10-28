"""Rich Discord embed builder."""
from __future__ import annotations
import json
from datetime import datetime
from typing import List, Dict, Any, Optional

def build_embed(war: Any, s: Dict, banner_fname: Optional[str], 
                 report: Optional[Dict], 
                 structures: Optional[Dict] = None) -> Dict:
    
    # Determine colors
    color = 12597547 # Red default
    if war.status == "mutual":
        color = 15158332 # Purple
    elif war.status == "open_for_allies":
        color = 16776960 # Orange
    
    # Build fields
    fields = []
    if report:
        # Timeline
        fields.append({"name": "🕒 Timeline", "value": f"Declared: {war.declared}\nStarts: {war.started}\nFinished: {war.finished}"})
        
        # Scorecard
        ag = report.get("aggressor", {})
        df = report.get("defender", {})
        score_text = (f"🔴 Aggressor: {ag.get('ships_killed')} kills, {ag.get('isk_destroyed'):,.0f} ISK\n"
                       f"🔵 Defender: {df.get('ships_killed')} kills, {df.get('isk_destroyed'):,.0f} ISK\n"
                       f"Efficiency: {ag.get('efficiency')}%")
        fields.append({"name": "⚔ Scorecard", "value": score_text})
        
        # Allies
        allies_list = ", ".join([a["name"] for a in report.get("allies", [])])
        fields.append({"name": "🤝 Allies", "value": allies_list if allies_list else "None"})
        
        # Hot Systems
        hot_sys = ", ".join([sys[0] for sys in report.get("top_systems", [])])
        fields.append({"name": "📍 Hot Systems", "value": hot_sys if hot_sys else "N/A"})
        
        # Links
        links = f"zKillboard war: {report.get('zkill_war_url')}"
        fields.append({"name": "🔗 Links", "value": links})

    # Structure Summary
    if structures and "structure_summary" in structures:
        summary = structures["structure_summary"]
        if summary.get("name") and summary.get("count"):
            fields.append({"name": "🏗 Structures", "value": f"{summary['name']}: {summary['count']}\n{summary.get('corporations', 'N/A')}"})

    # Construct Embed
    embed = {
        "title": f"⚔ {war.aggressor.name} → {war.defender.name}",
        "url": war.report_url,
        "color": color,
        "author": {
            "name": f"{war.aggressor.name} ({war.aggressor.ticker})",
            "url": f"https://evemaps.dotlan.net/{war.aggressor.type}/{war.aggressor.name}",
            "icon_url": f"https://images.evetech.net/{war.aggressor.entity_id}/logo?size=64"
        },
        "thumbnail": {
            "url": f"https://images.evetech.net/{war.defender.entity_id}/logo?size=128"
        },
        "description": f"Status: {war.status}",
        "fields": fields,
        "image": {"url": f"attachment://{banner_fname}"} if banner_fname else None,
        "footer": {"text": f"Source: ESI · War #{war.war_id}"},
        "timestamp": datetime.now().isoformat()
    }
    
    return embed

def build_payload(embeds: List[Dict], s: Dict, mention_role_id: Optional[str]) -> Dict:
    content = ""
    if mention_role_id:
        content = f"<@&{mention_role_id}>"
    
    return {
        "content": content,
        "embeds": embeds
    }
