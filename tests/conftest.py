"""Shared fixtures. Every test session appends a summary line to logs/tests/test-runs.jsonl."""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "tests", ROOT / "tests" / "integration"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from common import AMARR, FAKE, JITA, PERIMETER, TAMA, THERA  # noqa: E402
from evedm.characters import CharacterService  # noqa: E402
from evedm.config import Settings  # noqa: E402
from evedm.crypto import TokenCipher  # noqa: E402
from evedm.db import Database  # noqa: E402
from evedm.esi import EsiClient  # noqa: E402
from evedm.ranking import LiveProvider, RankingService  # noqa: E402
from evedm.sso import AuthService  # noqa: E402
from evedm.universe import GateGraph  # noqa: E402
from fake_eve import CLIENT_ID, CLIENT_SECRET, FakeCharacter, create_fake_app  # noqa: E402

# --------------------------------------------------------------------------- session log
_session: dict[str, float] = {}


def pytest_sessionstart(session: pytest.Session) -> None:
    _session["t0"] = time.time()


def pytest_terminal_summary(terminalreporter, exitstatus: int, config: pytest.Config) -> None:  # noqa: ANN001
    stats = {k: len(v) for k, v in terminalreporter.stats.items() if k in ("passed", "failed", "error", "skipped")}
    log_dir = Path(os.environ.get("EVEDM_TEST_LOG_DIR", ROOT / "logs" / "tests"))
    log_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(time.time() - _session.get("t0", time.time()), 2),
        "exit_status": int(exitstatus),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "args": config.invocation_params.args,
        **stats,
    }
    with open(log_dir / "test-runs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, default=str) + "\n")


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _no_live_alert_feed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apps built in unit tests never poll the real zKillboard feed; tests marked ``alerts_loop`` drive it."""
    if "alerts_loop" not in request.keywords:
        monkeypatch.setattr("evedm.alerts.AlertService.start", lambda self: None)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def graph() -> GateGraph:
    return GateGraph.load()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.secret_key = "unit-test-secret"
    s.eve.client_id = CLIENT_ID
    s.eve.client_secret = CLIENT_SECRET
    s.eve.sso_base = FAKE
    s.eve.esi_base = FAKE
    s.eve.callback_url = "http://testserver/sso/callback"
    return s


@pytest.fixture
def fake_characters() -> dict[int, FakeCharacter]:
    return {
        2100000001: FakeCharacter(2100000001, "Alpha One", JITA, 22456, "Fast", 60003760, False),
        2100000002: FakeCharacter(2100000002, "Bravo Two", PERIMETER, 29990, "Loki", None, True),
        2100000003: FakeCharacter(2100000003, "Charlie Three", AMARR, 670, "Pod", None, False),
        2100000004: FakeCharacter(2100000004, "Delta Four", THERA, 587, "Rifter", None, False),
        2100000005: FakeCharacter(2100000005, "Echo Five", TAMA, 99999, "Proto", None, False),
    }


@pytest.fixture
def fake_app(fake_characters):  # noqa: ANN001, ANN201
    return create_fake_app(FAKE, fake_characters)


@pytest.fixture
async def http(fake_app):  # noqa: ANN001, ANN201
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_app), base_url=FAKE) as client:
        yield client


@pytest.fixture
def db(settings: Settings) -> Database:
    d = Database(settings.db_path, settings.sqlite_journal)
    yield d
    d.close()


@pytest.fixture
def services(settings, http, db, graph):  # noqa: ANN001, ANN201
    auth = AuthService(settings.eve, http)
    esi = EsiClient(settings.eve, http)
    chars = CharacterService(db, TokenCipher(settings.secret_key), auth, settings)
    ranking = RankingService(
        graph, db, LiveProvider(chars, esi), esi=esi, avoid_names=["Zarzakh"], refresh_interval_s=0, max_wait_age_s=0
    )
    return {"auth": auth, "esi": esi, "chars": chars, "ranking": ranking, "db": db, "http": http}
