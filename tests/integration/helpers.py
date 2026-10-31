"""Process helpers for integration and performance tests (real sockets, real processes)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
FAKE_EVE = ROOT / "tests" / "fake_eve.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code < 500:
                return
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(0.1)
    raise TimeoutError(f"{url} did not come up: {last}")


@contextmanager
def fake_eve(characters: int = 20, rotate: bool = False, systems: str | None = None):
    port = free_port()
    cmd = [sys.executable, str(FAKE_EVE), "--port", str(port), "--characters", str(characters)]
    if rotate:
        cmd.append("--rotate-refresh")
    if systems:
        cmd += ["--systems", systems]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)  # noqa: S603
    base = f"http://127.0.0.1:{port}"
    try:
        wait_http(f"{base}/_admin/stats")
        yield base
    finally:
        proc.terminate()
        proc.wait(10)


def evedm_env(data_dir: Path, fake_base: str, port: int, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EVEDM_", "EVE_"))}
    env.update(
        {
            "EVEDM_DATA_DIR": str(data_dir),
            "EVEDM_SECRET_KEY": "integration-secret",
            "EVE_CLIENT_ID": "test-client-id",
            "EVE_CLIENT_SECRET": "test-client-secret",
            "EVE_CALLBACK_URL": f"http://127.0.0.1:{port}/sso/callback",
            "EVEDM_SSO_BASE": fake_base,
            "EVEDM_ESI_BASE": fake_base,
            "EVEDM_PORT": str(port),
            "EVEDM_HOST": "127.0.0.1",
            "EVEDM_WEB_AUTH": "sso",
            "EVEDM_WARS_ENABLED": "0",  # the war watcher has its own tests; no network from these processes
            "EVEDM_ALERTS_FEED_URL": f"{fake_base}/r2z2",  # the fake's killmail feed, never zKillboard itself
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.update(extra)
    return env


@contextmanager
def evedm_server(env: dict[str, str], cpus: str | None = None, log_path: Path | None = None):
    cmd = [sys.executable, "-m", "evedm", "serve"]
    if cpus and sys.platform.startswith("linux") and Path("/usr/bin/taskset").exists():
        cmd = ["taskset", "-c", cpus, *cmd]
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL  # noqa: SIM115
    proc = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT)  # noqa: S603
    base = f"http://127.0.0.1:{env['EVEDM_PORT']}"
    try:
        wait_http(f"{base}/api/health")
        yield base, proc
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        if log_path:
            out.close()


def sso_flow(client: httpx.Client, start_url: str, character_id: int) -> httpx.Response:
    """Drive the browser part of EVE SSO against the fake: start -> authorize(character) -> callback."""
    r = client.get(start_url, follow_redirects=False)
    assert r.status_code == 303, r.text
    authorize = r.headers["location"]
    r = httpx.get(httpx.URL(authorize).copy_add_param("character", str(character_id)), follow_redirects=False)
    assert r.status_code == 302, r.text
    callback = r.headers["location"]
    return client.get(callback, follow_redirects=False)
