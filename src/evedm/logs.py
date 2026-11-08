"""Logging (rotating file on the data volume, secrets redacted) and launch records."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .crypto import redact
from .db import Database


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            try:
                record.msg = record.getMessage()
            except (TypeError, ValueError):
                return True
            record.args = None
        record.msg = redact(str(record.msg))
        return True


def setup_logging(settings: Settings) -> Path:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.log_dir / "evedm.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    redactor = RedactingFilter()
    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for h in (file_handler, console):
        h.setFormatter(fmt)
        h.addFilter(redactor)
    root = logging.getLogger()
    for old in root.handlers[:]:
        old.close()  # release file handles (matters on Windows)
    root.handlers[:] = [file_handler, console]
    root.setLevel(settings.log_level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return log_file


def trim_memory() -> None:
    """Return freed heap pages to the OS after startup (glibc only; no-op elsewhere)."""
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def rss_mb() -> float | None:
    """Resident memory of this process in MB (Linux /proc; None elsewhere)."""
    return _proc_status_kb("VmRSS")


def peak_rss_mb() -> float | None:
    return _proc_status_kb("VmHWM")


def _proc_status_kb(field: str) -> float | None:
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith(field + ":"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LaunchRecorder:
    """Writes one row per launch to the DB and one JSON line to logs/launches.jsonl."""

    def __init__(self, db: Database, settings: Settings, mode: str):
        self.db = db
        self.settings = settings
        self.mode = mode
        self.id: int | None = None
        self.started = time.time()
        self.requests = 0

    def start(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        info = {
            "event": "start",
            "started_at": _now(),
            "version": __version__,
            "pid": os.getpid(),
            "mode": self.mode,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
            "cpu_count": os.cpu_count(),
            "rss_mb_start": rss_mb(),
            "config": self.settings.summary() | (extra or {}),
        }
        self.id = self.db.record_launch(
            started_at=info["started_at"],
            version=__version__,
            pid=info["pid"],
            mode=self.mode,
            python=info["python"],
            platform=info["platform"],
            cpu_count=info["cpu_count"],
            rss_mb_start=info["rss_mb_start"],
            config=json.dumps(info["config"]),
        )
        self._append(info)
        logging.getLogger("evedm").info("launch #%s: %s", self.id, json.dumps(info, default=str))
        return info

    def stop(self) -> None:
        if self.id is None:
            return
        info = {
            "event": "stop",
            "launch_id": self.id,
            "stopped_at": _now(),
            "rss_mb_peak": peak_rss_mb(),
            "requests": self.requests,
            "uptime_s": round(time.time() - self.started),
        }
        self.db.finish_launch(
            self.id, stopped_at=info["stopped_at"], rss_mb_peak=info["rss_mb_peak"], requests=self.requests
        )
        self._append(info)
        logging.getLogger("evedm").info("stop #%s: %s", self.id, json.dumps(info))

    def _append(self, info: dict[str, Any]) -> None:
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        with open(self.settings.log_dir / "launches.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(info, default=str) + "\n")
