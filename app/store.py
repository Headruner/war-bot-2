"""Async SQLite storage for war data."""
from __future__ import annotations
import aiosqlite
import json
import logging
import os
import sqlite3
from typing import Dict, List, Optional, Any, Union
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Project root (/app inside the container). Relative database paths are
# anchored here so "data/wars.db" works regardless of the working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = "data/wars.db"


def resolve_db_path(db_path: Union[str, os.PathLike, None] = None) -> Path:
    """Return an absolute database path; relative paths resolve against PROJECT_ROOT."""
    path = Path(db_path or DEFAULT_DB_PATH).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


class WarStore:
    def __init__(self, db_path: Union[str, os.PathLike, None] = None):
        self.db_path = resolve_db_path(db_path)
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        db_dir = self.db_path.parent
        log.info("Using database: %s", self.db_path)
        db_dir.mkdir(parents=True, exist_ok=True)

        # SQLite needs write access to the directory (journal files), not just the file.
        if not os.access(db_dir, os.W_OK | os.X_OK):
            uid = os.getuid() if hasattr(os, "getuid") else "n/a"
            raise PermissionError(
                f"Database directory {db_dir} is not writable by uid {uid}; "
                "check ownership of the mounted data directory."
            )

        try:
            self.db = await aiosqlite.connect(str(self.db_path))
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
        except sqlite3.OperationalError:
            log.error("Failed to open database at %s", self.db_path)
            raise

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
        await self.set_meta("bootstrapped", "true")
        await self.set_meta("bootstrapped_at", datetime.now(timezone.utc).isoformat())

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
