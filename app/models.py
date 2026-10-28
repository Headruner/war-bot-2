"""Data models for the EVE War Bot."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List

class NotifyError(Exception):
    """Exception raised for errors during Discord notification."""
    pass

class WebhookInvalid(Exception):
    """Exception raised when the Discord webhook URL is invalid."""
    pass

@dataclass
class Party:
    name: str
    type: str
    entity_id: Optional[int]
    members: Optional[int]
    dotlan_url: str
    ticker: Optional[str]
    corporations: Optional[int]

@dataclass
class War:
    war_id: int
    declared_at: Any
    aggressor: Party
    defender: Party
    status: str
    report_url: str
