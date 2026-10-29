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

# A war that fails to post this many times is parked as 'failed'
MAX_POST_ATTEMPTS = 5

# Columns added after the first release: (name, SQL type/default)
_MIGRATIONS = [
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("last_error", "TEXT"),
]


def resolve_db_path(db_path: Union[str, os.PathLike, None] = None) -> Path:
    """Return an absolute database path; relative paths resolve against PROJECT_ROOT."""
    path = Path(db_path or DEFAULT_DB_PATH).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            await self._migrate()
            await self.db.commit()
        except sqlite3.OperationalError:
            log.error("Failed to open database at %s", self.db_path)
            raise

    async def _migrate(self):
        async with self.db.execute("PRAGMA table_info(wars)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        for name, decl in _MIGRATIONS:
            if name not in existing:
                await self.db.execute(f"ALTER TABLE wars ADD COLUMN {name} {decl}")
                log.info("Database migrated: added wars.%s", name)

    async def close(self):
        if self.db:
            await self.db.close()

    # --- meta ------------------------------------------------------------

    async def get_meta(self, key: str) -> Optional[str]:
        async with self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_meta(self, key: str, value: str):
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        await self.db.commit()

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
        await self.set_meta("bootstrapped_at", _now())

    # --- wars ------------------------------------------------------------

    async def has(self, war_id: int) -> bool:
        async with self.db.execute("SELECT 1 FROM wars WHERE war_id = ?", (war_id,)) as cursor:
            return await cursor.fetchone() is not None

    async def add_if_new(self, war: Any, status: str) -> int:
        if await self.has(war.war_id):
            return 0
        await self.db.execute(
            "INSERT INTO wars (war_id, status, report_json, watermark_war_id) VALUES (?, ?, ?, ?)",
            (war.war_id, status, json.dumps({}), war.war_id),
        )
        await self.db.commit()
        return 1

    async def mark_marker(self, war_id: int, status: str):
        await self.db.execute("INSERT OR REPLACE INTO wars (war_id, status) VALUES (?, ?)", (war_id, status))
        await self.db.commit()

    async def pending_ids(self, limit: Optional[int] = None) -> List[int]:
        sql = "SELECT war_id FROM wars WHERE status = 'pending' ORDER BY war_id ASC"
        params: tuple = ()
        if limit:
            sql += " LIMIT ?"
            params = (int(limit),)
        async with self.db.execute(sql, params) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def mark_sent(self, war_ids: List[int], msg_id: Optional[str], attachments: List[Any]):
        for wid in war_ids:
            await self.db.execute(
                "UPDATE wars SET status = 'notified', notified_at = ?, discord_message_id = ?, "
                "attachments = ?, last_error = NULL WHERE war_id = ?",
                (_now(), msg_id, json.dumps(attachments), wid),
            )
        await self.db.commit()

    async def mark_failed_attempt(self, war_ids: List[int], error_msg: str,
                                  max_attempts: int = MAX_POST_ATTEMPTS):
        for wid in war_ids:
            await self.db.execute(
                "UPDATE wars SET attempts = attempts + 1, last_error = ?, "
                "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END "
                "WHERE war_id = ?",
                (error_msg[:500], max_attempts, wid),
            )
            log.error("Failed to notify war %s: %s", wid, error_msg)
        await self.db.commit()

    async def purge_wars_above(self, war_id: int) -> int:
        """Delete rows with IDs ESI has never issued (e.g. test data). Returns the count."""
        cursor = await self.db.execute("DELETE FROM wars WHERE war_id > ?", (war_id,))
        await self.db.commit()
        return cursor.rowcount

    async def max_war_id(self) -> int:
        async with self.db.execute("SELECT MAX(war_id) FROM wars") as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else 0

    async def stats(self) -> Dict[str, int]:
        async with self.db.execute("SELECT status, COUNT(*) FROM wars GROUP BY status") as cursor:
            return {status or "unknown": count for status, count in await cursor.fetchall()}

    # --- reports ---------------------------------------------------------

    async def get_report(self, war_id: int) -> Optional[Dict]:
        async with self.db.execute("SELECT report_json FROM wars WHERE war_id = ?", (war_id,)) as cursor:
            row = await cursor.fetchone()
            return json.loads(row[0]) if row and row[0] else None

    async def save_report(self, war_id: int, report: Dict):
        await self.db.execute("UPDATE wars SET report_json = ? WHERE war_id = ?", (json.dumps(report), war_id))
        await self.db.commit()
