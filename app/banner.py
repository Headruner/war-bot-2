"""Generates a 'Aggressor VS Defender' banner image."""
from __future__ import annotations
import logging
from PIL import Image, ImageDraw, ImageFont
from typing import Optional
from .models import War

log = logging.getLogger(__name__)

def render_banner(war: War, s: Dict) -> Optional[bytes]:
    if not s.get("banner_enabled"):
        return None
    
    try:
        # Create a 640x220 canvas (Standard Discord Banner Size)
        # Use a dark background
        img = Image.new('RGB', (640, 220), color=(20, 20, 25))
        draw = ImageDraw.Draw(img)
        
        # Note: In a real prod env, we'd load a .ttf font. 
        # Here we use default or fallback.
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 40)
        except:
            font = ImageFont.load_default()

        # Text layout
        ag_name = war.aggressor.name[:30]
        df_name = war.defender.name[:30]
        
        # Draw Aggressor
        draw.text((50, 80), ag_name, font=font, fill=(200, 200, 200))
        # Draw VS
        draw.text((300, 95), "VS", font=font, fill=(255, 50, 50))
        # Draw Defender
        draw.text((350, 80), df_name, font=font, fill=(200, 200, 200))
        
        # Footer
        draw.text((50, 180), f"War ID: {war.war_id} | Report: {war.report_url}", font=Font(20), fill=(100, 100, 100))

        # Save to buffer
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        log.warning("Banner generation failed for %s: %s", war.war_id, e)
        return None

# Need to import io
import io
# Re-import font fix
try:
    from PIL import ImageFont
    Font = ImageFont.truetype("DejaVuSans.ttf", 40)
except:
    Font = ImageFont.load_default()
