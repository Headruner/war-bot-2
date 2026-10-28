"""Data models for the EVE War Bot."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List

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
