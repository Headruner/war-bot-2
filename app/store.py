"""Async SQLite storage for war data."""
from __future__ import annotations
import aiosqlite
import json
import logging
import os
import sqlite3
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

class WarStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        import os
        db_path = Path(self.db_path)
        
        log.info("DEBUG: Using database path: %s", db_path)
        
        if not db_path.parent.exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # Ensure directory is writable by the current user
            # This can help if the directory was created by root but we are running as botuser
            try:
                os.chmod(db_path.parent, 0o777)
            except Exception as e:
                log.warning("Could not chmod %s: %s", db_path.parent, e)

        try:
            self.db = await aiosqlite.connect(str(db_path))
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS wars (
                    war_id INTEGER PRIMARY KEY,
                    status TEXT,
                    notified_at TEXT,
                    discord_message_id TEXT,
                    attachments TEXT,
                    report_json TEXT,
                    watermark_war_id INTEGER
                )
            """)
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await self.db.commit()
        except sqlite3.OperationalError as e:
            if "unable to open database file" in str(e):
                log.error("Failed to open database at %s. Please ensure the directory is writable.", db_path)
            raise e

    async def close(self):
        if self.db:
            await self.db.close()

    async def get_meta(self, key: str) -> Optional[str]:
        async with self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_meta(self, key: str, value: str):
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        await self.db.commit()

    async def has(self, war_id: int) -> bool:
        async with self.db.execute("SELECT 1 FROM wars WHERE war_id = ?", (war_id,)) as cursor:
            return await cursor.fetchone() is not None

    async def add_if_new(self, war: Any, status: str) -> int:
        # war.war_id is used as primary key
        count = 0
        if not await self.has(war.war_id):
            await self.db.execute(
                "INSERT INTO wars (war_id, status, report_json, watermark_war_id) VALUES (?, ?, ?, ?)",
                (war.war_id, status, json.dumps({}), war.war_id)
            )
            await self.db.commit()
            count = 1
        return count

    async def mark_sent(self, war_ids: List[int], msg_id: str, attachments: List[Dict]):
        for wid in war_ids:
            await self.db.execute(
                "UPDATE wars SET notified_at = ?, discord_message_id = ?, attachments = ? WHERE war_id = ?",
                (datetime.now().isoformat(), msg_id, json.dumps(attachments), wid)
            )
        await self.db.commit()

    async def mark_failed_attempt(self, war_ids: List[int], error_msg: str):
        for wid in war_ids:
            # This logic would need a 'attempts' column in DB, 
            # simplifying for now by just logging.
            log.error("Failed to notify war %s: %s", wid, error_msg)

    async def pending(self) -> List[Any]:
        async with self.db.execute("SELECT * FROM wars WHERE status = 'pending' ORDER BY war_id ASC") as cursor:
            rows = await cursor.fetchall()
            return [War(**dict(zip(cursor.description, row))) for row in rows]

    async def mark_live_done(self, war_ids: List[int]):
        for wid in war_ids:
            await self.db.execute("UPDATE wars SET live_done = 1 WHERE war_id = ?", (wid,))
        await self.db.commit()

    async def expire_live(self, days: int):
        # Logic to reset live_done if too old
        pass

    async def get_report(self, war_id: int) -> Optional[Dict]:
        async with self.db.execute("SELECT report_json FROM wars WHERE war_id = ?", (war_id,)) as cursor:
            row = await cursor.fetchone()
            return json.loads(row[0]) if row else None

    async def save_report(self, war_id: int, report: Dict):
        await self.db.execute("UPDATE wars SET report_json = ? WHERE war_id = ?", (json.dumps(report), war_id))
        await self.db.commit()

    async def max_war_id(self) -> int:
        async with self.db.execute("SELECT MAX(war_id) FROM wars") as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else 0

    async def set_meta_watermark(self, value: int):
        await self.set_meta("watermark_war_id", str(value))

    async def get_meta_watermark(self) -> Optional[int]:
        v = await self.get_meta("watermark_war_id")
        return int(v) if v is not None else None

    async def bootstrapped(self) -> bool:
        v = await self.get_meta("bootstrapped")
        return v == "true"

    async def set_bootstrapped(self):
        await self.set_meta("bootstrapped_at", datetime.now().isoformat())

    async def mark_marker(self, war_id: int, status: str):
        await self.db.execute("INSERT OR REPLACE INTO wars (war_id, status) VALUES (?, ?)", (war_id, status))
        await self.db.commit()

# Mock War class for typing
from dataclasses import dataclass
@dataclass
class War:
    war_id: int
    declared_at: Any
    aggressor: Any
    defender: Any
    status: str
    report_url: str
