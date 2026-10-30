"""War rows in the main SQLite database (tables from evedm.db.WAR_SCHEMA); keys in `meta` start with "wars."."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..db import Database

log = logging.getLogger("evedm.wars.store")

MAX_POST_ATTEMPTS = 5  # a war that fails to post this many times is parked as 'failed'
SHOWN = ("pending", "ready", "notified", "failed")  # rows with a summary appear on the Wars page


class WarStore:
    def __init__(self, db: Database):
        self.db = db

    # -- meta ---------------------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        return self.db.get_meta(f"wars.{key}")

    def set_meta(self, key: str, value: str) -> None:
        self.db.set_meta(f"wars.{key}", value)

    def bootstrapped(self) -> bool:
        return self.get_meta("bootstrapped") == "true"

    def set_bootstrapped(self) -> None:
        self.set_meta("bootstrapped", "true")
        self.set_meta("bootstrapped_at", str(time.time()))

    # -- wars -----------------------------------------------------------------------------------
    def has(self, war_id: int) -> bool:
        return bool(self.db.query("SELECT 1 FROM wars WHERE war_id=?", (war_id,)))

    def add_if_new(self, war_id: int, status: str, declared: str | None = None, war_type: str | None = None) -> int:
        if self.has(war_id):
            return 0
        self.db.execute(
            "INSERT INTO wars(war_id,status,declared,war_type,updated_at) VALUES(?,?,?,?,?)",
            (war_id, status, declared, war_type, time.time()),
        )
        return 1

    def mark_marker(self, war_id: int, status: str) -> None:
        self.db.execute(
            "INSERT INTO wars(war_id,status,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(war_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
            (war_id, status, time.time()),
        )

    def pending_ids(self, limit: int | None = None) -> list[int]:
        sql = "SELECT war_id FROM wars WHERE status='pending' ORDER BY war_id ASC"
        rows = self.db.query(sql + " LIMIT ?", (int(limit),)) if limit else self.db.query(sql)
        return [r[0] for r in rows]

    def save_summary(self, war_id: int, summary: dict[str, Any], status: str | None = None) -> None:
        self.db.execute(
            "UPDATE wars SET summary_json=?, declared=COALESCE(?, declared), war_type=?, aggressor=?, defender=?,"
            " status=COALESCE(?, status), updated_at=? WHERE war_id=?",
            (
                json.dumps(summary, separators=(",", ":")),
                summary.get("declared"),
                summary.get("type"),
                summary["aggressor"]["name"],
                summary["defender"]["name"],
                status,
                time.time(),
                war_id,
            ),
        )

    def mark_sent(self, war_ids: list[int], msg_id: str | None) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.db.tx() as c:
            for wid in war_ids:
                c.execute(
                    "UPDATE wars SET status='notified', notified_at=?, discord_message_id=?, last_error=NULL,"
                    " updated_at=? WHERE war_id=?",
                    (now, msg_id, time.time(), wid),
                )

    def mark_failed_attempt(self, war_ids: list[int], error_msg: str, max_attempts: int = MAX_POST_ATTEMPTS) -> None:
        with self.db.tx() as c:
            for wid in war_ids:
                c.execute(
                    "UPDATE wars SET attempts=attempts+1, last_error=?, "
                    "status=CASE WHEN attempts+1>=? THEN 'failed' ELSE status END, updated_at=? WHERE war_id=?",
                    (error_msg[:500], max_attempts, time.time(), wid),
                )
        log.error("failed to post war(s) %s: %s", war_ids, error_msg)

    def purge_wars_above(self, war_id: int) -> int:
        """Delete rows with IDs ESI has never issued (e.g. test data). Returns the count."""
        return self.db.execute("DELETE FROM wars WHERE war_id > ?", (war_id,))

    def max_war_id(self) -> int:
        row = self.db.query("SELECT MAX(war_id) FROM wars")[0][0]
        return int(row or 0)

    def stats(self) -> dict[str, int]:
        return {r[0] or "unknown": r[1] for r in self.db.query("SELECT status, COUNT(*) FROM wars GROUP BY status")}

    # -- reading ----------------------------------------------------------------------------------
    def summaries(self, limit: int = 50, offset: int = 0, war_type: str | None = None) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(SHOWN))
        sql = f"SELECT war_id, status, summary_json FROM wars WHERE summary_json IS NOT NULL AND status IN ({marks})"  # noqa: S608
        params: list[Any] = list(SHOWN)
        if war_type:
            sql += " AND war_type=?"
            params.append(war_type)
        sql += " ORDER BY declared DESC, war_id DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        out = []
        for r in self.db.query(sql, tuple(params)):
            item = json.loads(r["summary_json"])
            item["post_status"] = r["status"]
            out.append(item)
        return out

    def get_summary(self, war_id: int) -> dict[str, Any] | None:
        rows = self.db.query("SELECT status, summary_json FROM wars WHERE war_id=?", (war_id,))
        if not rows or not rows[0]["summary_json"]:
            return None
        return json.loads(rows[0]["summary_json"]) | {"post_status": rows[0]["status"]}

    def monthly(self, months: int = 12) -> list[dict[str, Any]]:
        """Wars declared per calendar month (newest first): total, by war type, and how many were shown/posted."""
        rows = self.db.query(
            "SELECT substr(declared,1,7) AS month, COUNT(*) AS total,"
            " SUM(war_type='mutual') AS mutual, SUM(war_type='open_for_allies') AS open_for_allies,"
            " SUM(status IN ('ready','notified')) AS shown, SUM(status='notified') AS posted"
            " FROM wars WHERE declared IS NOT NULL GROUP BY month ORDER BY month DESC LIMIT ?",
            (int(months),),
        )
        return [dict(r) for r in rows]
