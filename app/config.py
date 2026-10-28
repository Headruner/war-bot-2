"""Configuration loading and validation."""
import yaml
import json
import os
from pathlib import Path
from typing import Dict, Any

class ConfigError(Exception):
    pass

def load_settings() -> Dict[str, Any]:
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        # Return defaults if missing
        return {
            "poll_interval_min": 15,
            "max_new_per_cycle": 50,
            "batch_embeds": 5,
            "report_enabled": True,
            "live_updates": True,
            "report_recent_kills": 5,
            "report_kill_details_limit": 25,
            "report_max_killmail_pages": 1,
            "alliance_member_count_max_corps": 20,
            "structures_enabled": True,
            "structures_refresh_hours": 24,
            "log_level": "INFO",
            "esi_base_url": "https://esi.evetech.net",
            "esi_compatibility_date": "2025-08-26",
            "structures_url": "https://data.everef.net/structures/structures-latest.v2.json",
            "sqlite_path": "data/wars.db",
            "heartbeat_file": "heartbeat.txt"
        }
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def load_credentials(required: bool = True) -> Dict[str, Any]:
    path = Path("config/credentials.json")
    if not path.exists():
        if required:
            raise ConfigError("config/credentials.json not found.")
        return {}
    with open(path, "r") as f:
        return json.load(f)
