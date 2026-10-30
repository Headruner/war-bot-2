"""SQLite storage: one database file (WAL mode) on the data volume (data/ locally, /app/data in Docker)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 4
log = logging.getLogger("evedm.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,              -- the main character's ID
    name           TEXT NOT NULL,
    created_at     REAL NOT NULL,
    last_login_at  REAL NOT NULL
);
-- one row per (main, linked character): the same EVE character may be linked to several mains,
-- each link with its own tokens and local settings. owner_id 0 = local user (web.auth none, demo, CLI).
CREATE TABLE IF NOT EXISTS characters (
    owner_id       INTEGER NOT NULL DEFAULT 0,
    character_id   INTEGER NOT NULL,
    name           TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    tags           TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'OK',      -- OK | LIMITED | REAUTH | DISABLED
    scopes         TEXT NOT NULL DEFAULT '[]',
    owner_hash     TEXT,
    demo           INTEGER NOT NULL DEFAULT 0,
    added_at       REAL NOT NULL,
    PRIMARY KEY (owner_id, character_id)
);
CREATE INDEX IF NOT EXISTS characters_by_id ON characters(character_id);
CREATE TABLE IF NOT EXISTS tokens (
    owner_id          INTEGER NOT NULL DEFAULT 0,
    character_id      INTEGER NOT NULL,
    client_id         TEXT NOT NULL,
    access_token      BLOB,
    access_expires_at REAL NOT NULL DEFAULT 0,
    refresh_token     BLOB NOT NULL,
    refresh_tail      TEXT NOT NULL DEFAULT '',
    updated_at        REAL NOT NULL,
    PRIMARY KEY (owner_id, character_id),
    FOREIGN KEY (owner_id, character_id) REFERENCES characters(owner_id, character_id) ON DELETE CASCADE
);
-- last known state of an EVE character (shared by every main that links it)
CREATE TABLE IF NOT EXISTS snapshots (
    character_id    INTEGER PRIMARY KEY,
    solar_system_id INTEGER,
    station_id      INTEGER,
    structure_id    INTEGER,
    ship_type_id    INTEGER,
    ship_item_id    INTEGER,
    ship_name       TEXT,
    online          INTEGER,
    last_login      TEXT,
    last_logout     TEXT,
    fetched_at      REAL,
    error           TEXT
);
CREATE TABLE IF NOT EXISTS type_names (
    type_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS target_history (
    owner_id        INTEGER NOT NULL DEFAULT 0,
    solar_system_id INTEGER NOT NULL,
    used_at         REAL NOT NULL,
    PRIMARY KEY (owner_id, solar_system_id)
);
CREATE TABLE IF NOT EXISTS sso_states (
    state      TEXT PRIMARY KEY,
    purpose    TEXT NOT NULL,          -- login | add
    created_at REAL NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS launches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    stopped_at   TEXT,
    version      TEXT NOT NULL,
    pid          INTEGER NOT NULL,
    mode         TEXT NOT NULL,
    python       TEXT NOT NULL,
    platform     TEXT NOT NULL,
    cpu_count    INTEGER,
    rss_mb_start REAL,
    rss_mb_peak  REAL,
    requests     INTEGER,
    config       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# war tables (filled by evedm.wars, the merged eve-war-bot-2 logic)
WAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS wars (
    war_id             INTEGER PRIMARY KEY,
    status             TEXT NOT NULL,      -- pending | notified | ready | failed | stale | filtered | skipped
    declared           TEXT,
    war_type           TEXT,               -- none | mutual | open_for_allies
    aggressor          TEXT,
    defender           TEXT,
    summary_json       TEXT,               -- parties, report and structures shown on the Wars page
    notified_at        TEXT,
    discord_message_id TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS wars_by_declared ON wars(declared);
-- every war the census has seen (active = finished is NULL or in the future)
CREATE TABLE IF NOT EXISTS war_census (
    war_id          INTEGER PRIMARY KEY,
    declared        TEXT,
    started         TEXT,
    finished        TEXT,
    retracted       TEXT,
    mutual          INTEGER NOT NULL DEFAULT 0,
    open_for_allies INTEGER NOT NULL DEFAULT 0,
    aggressor_id    INTEGER,
    aggressor_type  TEXT,
    aggressor_ships INTEGER NOT NULL DEFAULT 0,
    aggressor_isk   REAL NOT NULL DEFAULT 0,
    defender_id     INTEGER,
    defender_type   TEXT,
    defender_ships  INTEGER NOT NULL DEFAULT 0,
    defender_isk    REAL NOT NULL DEFAULT 0,
    allies          TEXT NOT NULL DEFAULT '[]',
    checked_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS war_census_by_finished ON war_census(finished);
CREATE TABLE IF NOT EXISTS entity_names (
    entity_id  INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alliance_corps (
    alliance_id INTEGER PRIMARY KEY,
    corps       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""

# v1 (single user) -> v2 (characters and tokens owned by a main; v1 rows go to owner 0)
_MIGRATE_V1_V2 = """
ALTER TABLE characters RENAME TO characters_v1;
ALTER TABLE tokens RENAME TO tokens_v1;
ALTER TABLE snapshots RENAME TO snapshots_v1;
ALTER TABLE target_history RENAME TO target_history_v1;
"""
_COPY_V1_V2 = """
INSERT INTO characters(owner_id,character_id,name,enabled,tags,status,scopes,owner_hash,demo,added_at)
    SELECT 0,character_id,name,enabled,tags,status,scopes,owner_hash,demo,added_at FROM characters_v1;
INSERT INTO tokens(owner_id,character_id,client_id,access_token,access_expires_at,refresh_token,refresh_tail,updated_at)
    SELECT 0,character_id,client_id,access_token,access_expires_at,refresh_token,refresh_tail,updated_at FROM tokens_v1;
INSERT INTO snapshots SELECT * FROM snapshots_v1;
INSERT INTO target_history(owner_id,solar_system_id,used_at) SELECT 0,solar_system_id,used_at FROM target_history_v1;
DROP TABLE tokens_v1;
DROP TABLE snapshots_v1;
DROP TABLE characters_v1;
DROP TABLE target_history_v1;
"""


@dataclass
class CharacterRow:
    character_id: int
    name: str
    enabled: bool
    tags: list[str]
    status: str
    scopes: list[str]
    owner_hash: str | None
    demo: bool
    added_at: float
    owner_id: int = 0


class Database:
    """Thin, thread-safe wrapper around one sqlite3 connection (the app runs one process)."""

    def __init__(self, path: str | Path, journal_mode: str = "wal"):
        self.path = Path(path) if str(path) == ":memory:" else Path(path).resolve()
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path) if str(path) != ":memory:" else ":memory:", check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.journal_mode = self._set_journal(journal_mode)
        self._conn.execute("PRAGMA foreign_keys=ON")
        # NORMAL is safe with WAL (no corruption, at most the last commits lost on power failure)
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA cache_size=-2000")  # ~2 MB page cache
        self.migrate()

    def _set_journal(self, wanted: str) -> str:
        """Enable WAL (default). Fall back to DELETE where the filesystem cannot share WAL memory."""
        wanted = wanted.lower()
        try:
            mode = str(self._conn.execute(f"PRAGMA journal_mode={wanted.upper()}").fetchone()[0]).lower()
            if mode == "wal":
                self._conn.execute("CREATE TABLE IF NOT EXISTS _wal_probe(x)")  # a real write through WAL
                self._conn.execute("DROP TABLE _wal_probe")
        except sqlite3.OperationalError as exc:
            log.warning("WAL is not supported on %s (%s); using journal_mode=DELETE", self.path, exc)
            mode = "delete"
        if wanted == "wal" and mode != "wal":
            log.warning("journal_mode=WAL unavailable for %s (got %s); using DELETE", self.path, mode)
            mode = str(self._conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        return mode

    # -- plumbing --------------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    def _script(self, sql: str) -> None:
        for stmt in (s.strip() for s in sql.split(";\n")):
            if stmt and not all(line.strip().startswith("--") or not line.strip() for line in stmt.splitlines()):
                self._conn.execute(stmt)

    def migrate(self) -> None:
        """0 -> create; 1 -> 2 per-main ownership (v1 rows to owner 0); 2 -> 3 war tables. One transaction."""
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= SCHEMA_VERSION:
                return
            log.info("migrating database schema v%s -> v%s", version, SCHEMA_VERSION)
            self._conn.execute("PRAGMA foreign_keys=OFF")  # table rebuilds; cannot change inside a transaction
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if version == 1:
                    self._script(_MIGRATE_V1_V2)
                    self._script(SCHEMA)
                    self._script(_COPY_V1_V2)
                    log.info("database migrated to per-main ownership: existing characters belong to owner 0")
                else:
                    self._script(SCHEMA)
                self._script(WAR_SCHEMA)
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        """Checkpoint the WAL into the main file so the database is one self-contained file again."""
        with self._lock:
            if self.journal_mode == "wal":
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error as exc:
                    log.warning("WAL checkpoint failed: %s", exc)
            self._conn.close()

    def ok(self) -> bool:
        try:
            return self.query("SELECT 1")[0][0] == 1
        except sqlite3.Error:
            return False

    # -- meta ---------------------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )

    # -- characters ---------------------------------------------------------------------------
    @staticmethod
    def _char(row: sqlite3.Row) -> CharacterRow:
        return CharacterRow(
            character_id=row["character_id"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            tags=json.loads(row["tags"]),
            status=row["status"],
            scopes=json.loads(row["scopes"]),
            owner_hash=row["owner_hash"],
            demo=bool(row["demo"]),
            added_at=row["added_at"],
            owner_id=row["owner_id"],
        )

    def list_characters(self, owner_id: int = 0) -> list[CharacterRow]:
        rows = self.query("SELECT * FROM characters WHERE owner_id=? ORDER BY name COLLATE NOCASE", (owner_id,))
        return [self._char(r) for r in rows]

    def get_character(self, character_id: int, owner_id: int = 0) -> CharacterRow | None:
        rows = self.query("SELECT * FROM characters WHERE owner_id=? AND character_id=?", (owner_id, character_id))
        return self._char(rows[0]) if rows else None

    def owners_of(self, character_id: int) -> list[int]:
        return [r[0] for r in self.query("SELECT owner_id FROM characters WHERE character_id=?", (character_id,))]

    def upsert_character(
        self,
        character_id: int,
        name: str,
        status: str,
        scopes: list[str],
        owner_hash: str | None = None,
        demo: bool = False,
        enabled: bool | None = None,
        tags: list[str] | None = None,
        *,
        owner_id: int = 0,
    ) -> bool:
        """Insert or update the link owner -> character; True when new. Local enabled/tags are kept."""
        with self.tx() as c:
            exists = c.execute(
                "SELECT 1 FROM characters WHERE owner_id=? AND character_id=?", (owner_id, character_id)
            ).fetchone()
            if exists:
                c.execute(
                    "UPDATE characters SET name=?, status=?, scopes=?, owner_hash=COALESCE(?, owner_hash) "
                    "WHERE owner_id=? AND character_id=?",
                    (name, status, json.dumps(scopes), owner_hash, owner_id, character_id),
                )
                return False
            c.execute(
                "INSERT INTO characters(owner_id,character_id,name,enabled,tags,status,scopes,owner_hash,demo,"
                "added_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    owner_id,
                    character_id,
                    name,
                    1 if enabled is None else int(enabled),
                    json.dumps(tags or []),
                    status,
                    json.dumps(scopes),
                    owner_hash,
                    int(demo),
                    time.time(),
                ),
            )
            return True

    def set_enabled(self, character_id: int, enabled: bool, owner_id: int = 0) -> None:
        self.execute(
            "UPDATE characters SET enabled=? WHERE owner_id=? AND character_id=?",
            (int(enabled), owner_id, character_id),
        )

    def set_tags(self, character_id: int, tags: list[str], owner_id: int = 0) -> None:
        clean = sorted({t.strip()[:32] for t in tags if t.strip()})
        self.execute(
            "UPDATE characters SET tags=? WHERE owner_id=? AND character_id=?",
            (json.dumps(clean), owner_id, character_id),
        )

    def set_status(self, character_id: int, status: str, owner_id: int = 0) -> None:
        self.execute(
            "UPDATE characters SET status=? WHERE owner_id=? AND character_id=?", (status, owner_id, character_id)
        )

    def delete_character(self, character_id: int, owner_id: int = 0) -> None:
        """Remove one link (its tokens cascade); the shared snapshot goes with the last link."""
        with self.tx() as c:
            c.execute("DELETE FROM characters WHERE owner_id=? AND character_id=?", (owner_id, character_id))
            if not c.execute("SELECT 1 FROM characters WHERE character_id=?", (character_id,)).fetchone():
                c.execute("DELETE FROM snapshots WHERE character_id=?", (character_id,))

    # -- users (mains) --------------------------------------------------------------------------
    def upsert_user(self, user_id: int, name: str) -> bool:
        """Record a sign-in of a main; True when the main is new."""
        now = time.time()
        with self.tx() as c:
            if c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
                c.execute("UPDATE users SET name=?, last_login_at=? WHERE user_id=?", (name, now, user_id))
                return False
            c.execute(
                "INSERT INTO users(user_id,name,created_at,last_login_at) VALUES(?,?,?,?)", (user_id, name, now, now)
            )
            return True

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM users WHERE user_id=?", (user_id,))
        return rows[0] if rows else None

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT u.*, (SELECT COUNT(*) FROM characters c WHERE c.owner_id=u.user_id) AS characters "
            "FROM users u ORDER BY u.name COLLATE NOCASE"
        )
        return [dict(r) for r in rows]

    def delete_user(self, user_id: int) -> None:
        """Delete a main with all its links and tokens (snapshots of no longer linked characters too)."""
        with self.tx() as c:
            c.execute("DELETE FROM characters WHERE owner_id=?", (user_id,))
            c.execute("DELETE FROM target_history WHERE owner_id=?", (user_id,))
            c.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM snapshots WHERE character_id NOT IN (SELECT character_id FROM characters)")

    def reassign_owner(self, from_owner: int, to_owner: int) -> int:
        """Move the links of one owner to another (legacy owner 0 -> a main). Existing links of the receiving
        main win; demo characters stay where they are. Returns the number of characters moved."""
        with self.tx() as c:
            c.execute(
                "DELETE FROM characters WHERE owner_id=? AND demo=0 AND character_id IN "
                "(SELECT character_id FROM characters WHERE owner_id=?)",
                (from_owner, to_owner),
            )
            c.execute("PRAGMA defer_foreign_keys=ON")
            c.execute(
                "UPDATE tokens SET owner_id=? WHERE owner_id=? AND character_id IN "
                "(SELECT character_id FROM characters WHERE owner_id=? AND demo=0)",
                (to_owner, from_owner, from_owner),
            )
            n = c.execute(
                "UPDATE characters SET owner_id=? WHERE owner_id=? AND demo=0", (to_owner, from_owner)
            ).rowcount
            c.execute(
                "INSERT OR IGNORE INTO target_history(owner_id, solar_system_id, used_at) "
                "SELECT ?, solar_system_id, used_at FROM target_history WHERE owner_id=?",
                (to_owner, from_owner),
            )
            return n

    # -- tokens -------------------------------------------------------------------------------
    def save_tokens(
        self,
        character_id: int,
        client_id: str,
        access_token: bytes | None,
        expires_at: float,
        refresh_token: bytes,
        refresh_tail: str,
        *,
        owner_id: int = 0,
    ) -> None:
        self.execute(
            "INSERT INTO tokens(owner_id,character_id,client_id,access_token,access_expires_at,refresh_token,"
            "refresh_tail,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,character_id) DO UPDATE SET"
            " client_id=excluded.client_id, access_token=excluded.access_token,"
            " access_expires_at=excluded.access_expires_at, refresh_token=excluded.refresh_token,"
            " refresh_tail=excluded.refresh_tail, updated_at=excluded.updated_at",
            (owner_id, character_id, client_id, access_token, expires_at, refresh_token, refresh_tail, time.time()),
        )

    def get_tokens(self, character_id: int, owner_id: int = 0) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM tokens WHERE owner_id=? AND character_id=?", (owner_id, character_id))
        return rows[0] if rows else None

    # -- snapshots ----------------------------------------------------------------------------
    def save_snapshot(self, character_id: int, **fields: Any) -> None:
        cols = ["character_id", *fields.keys()]
        updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
        self.execute(
            f"INSERT INTO snapshots({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "  # noqa: S608 - fixed columns
            f"ON CONFLICT(character_id) DO UPDATE SET {updates}",
            (character_id, *fields.values()),
        )

    def snapshot_error(self, character_id: int, error: str) -> None:
        self.execute(
            "INSERT INTO snapshots(character_id, error) VALUES(?,?) "
            "ON CONFLICT(character_id) DO UPDATE SET error=excluded.error",
            (character_id, error),
        )

    def save_snapshots(self, items: list[tuple[int, dict[str, Any] | None, str | None]]) -> None:
        """Write a whole refresh round in one transaction (one fsync instead of one per character)."""
        if not items:
            return
        with self.tx() as c:
            for character_id, fields, error in items:
                if fields is None:
                    c.execute(
                        "INSERT INTO snapshots(character_id, error) VALUES(?,?) "
                        "ON CONFLICT(character_id) DO UPDATE SET error=excluded.error",
                        (character_id, error),
                    )
                    continue
                cols = ["character_id", *fields.keys()]
                updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
                c.execute(
                    f"INSERT INTO snapshots({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "  # noqa: S608
                    f"ON CONFLICT(character_id) DO UPDATE SET {updates}",
                    (character_id, *fields.values()),
                )

    def get_snapshots(self, character_ids: list[int] | None = None) -> dict[int, sqlite3.Row]:
        if character_ids is None:
            return {r["character_id"]: r for r in self.query("SELECT * FROM snapshots")}
        if not character_ids:
            return {}
        marks = ",".join("?" * len(character_ids))
        rows = self.query(f"SELECT * FROM snapshots WHERE character_id IN ({marks})", tuple(character_ids))  # noqa: S608
        return {r["character_id"]: r for r in rows}

    # -- type names, targets, SSO states --------------------------------------------------------
    def get_type_names(self, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.query(f"SELECT type_id, name FROM type_names WHERE type_id IN ({marks})", tuple(ids))  # noqa: S608
        return {r["type_id"]: r["name"] for r in rows}

    def save_type_names(self, names: dict[int, str]) -> None:
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO type_names(type_id,name) VALUES(?,?)", list(names.items()))

    def remember_target(self, system_id: int, owner_id: int = 0) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO target_history(owner_id, solar_system_id, used_at) VALUES(?,?,?) "
                "ON CONFLICT(owner_id, solar_system_id) DO UPDATE SET used_at=excluded.used_at",
                (owner_id, system_id, time.time()),
            )
            c.execute(
                "DELETE FROM target_history WHERE owner_id=? AND solar_system_id NOT IN "
                "(SELECT solar_system_id FROM target_history WHERE owner_id=? ORDER BY used_at DESC LIMIT 20)",
                (owner_id, owner_id),
            )

    def recent_targets(self, limit: int = 20, owner_id: int = 0) -> list[int]:
        rows = self.query(
            "SELECT solar_system_id FROM target_history WHERE owner_id=? ORDER BY used_at DESC LIMIT ?",
            (owner_id, limit),
        )
        return [r[0] for r in rows]

    def create_state(self, state: str, purpose: str) -> None:
        self.execute("INSERT INTO sso_states(state,purpose,created_at) VALUES(?,?,?)", (state, purpose, time.time()))
        self.execute("DELETE FROM sso_states WHERE created_at < ?", (time.time() - 3600,))

    def consume_state(self, state: str, max_age_s: float = 600) -> str | None:
        """SSO-04: single use, at most 10 minutes old. Returns the purpose or None."""
        with self.tx() as c:
            row = c.execute("SELECT purpose, created_at, used FROM sso_states WHERE state=?", (state,)).fetchone()
            if row is None or row["used"] or time.time() - row["created_at"] > max_age_s:
                return None
            c.execute("UPDATE sso_states SET used=1 WHERE state=?", (state,))
            return row["purpose"]

    # -- launches -------------------------------------------------------------------------------
    def record_launch(self, **fields: Any) -> int:
        cols = list(fields)
        return self.execute(
            f"INSERT INTO launches({','.join(cols)}) VALUES({','.join('?' * len(cols))})",  # noqa: S608
            tuple(fields.values()),
        )

    def finish_launch(self, launch_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE launches SET {sets} WHERE id=?", (*fields.values(), launch_id))  # noqa: S608

    def launches(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query("SELECT * FROM launches ORDER BY id DESC LIMIT ?", (limit,))]
