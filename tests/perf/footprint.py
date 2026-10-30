"""Resource footprint benchmark for the 2 vCPU / 1 GB RAM budget.

Starts the fake EVE server (with ESI latency), starts evedm (local process pinned to 2 CPUs, or uses an
already running container), imports N characters, then drives concurrent ranking traffic and records
peak RSS, CPU and latency. Writes a JSON + Markdown report to logs/perf/.

  python tests/perf/footprint.py --label local
  python tests/perf/footprint.py --label docker --docker evedm:0.6.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from helpers import ROOT, evedm_env, evedm_server, fake_eve, free_port, wait_http  # noqa: E402

TARGETS = ["Jita", "Amarr", "Tama", "Rens", "Dodixie", "Hek", "1DQ1-A", "Uedama", "Perimeter", "Old Man Star"]
H = {"X-EVEDM": "1"}


def proc_status(pid: int) -> dict[str, float]:
    out = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            out[line.split(":")[0]] = int(line.split()[1]) / 1024
    return out


def proc_cpu_s(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    import os

    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def docker_status(name: str) -> dict[str, float]:
    text = subprocess.run(
        ["docker", "exec", name, "cat", "/proc/1/status"],
        capture_output=True,
        text=True,  # noqa: S603, S607
        check=True,
    ).stdout
    out = {}
    for line in text.splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            out[line.split(":")[0]] = int(line.split()[1]) / 1024
    return out


def docker_cpu_s(name: str) -> float:
    text = subprocess.run(
        ["docker", "exec", name, "cat", "/proc/1/stat"],
        capture_output=True,
        text=True,  # noqa: S603, S607
        check=True,
    ).stdout
    fields = text.rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / 100


async def drive(base: str, requests: int, concurrency: int, seed: int = 1) -> dict:
    rnd = random.Random(seed)
    latencies: list[float] = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=base, timeout=30, limits=httpx.Limits(max_connections=concurrency)) as c:

        async def one(k: int) -> None:
            nonlocal errors
            async with sem:
                t0 = time.perf_counter()
                try:
                    if k % 10 == 0:
                        r = await c.get("/api/universe/map", headers={"Accept-Encoding": "gzip"})
                    elif k % 10 == 1:
                        r = await c.get("/api/systems/search", params={"q": rnd.choice(TARGETS)[:3]})
                    else:
                        r = await c.get("/api/rank", params={"target": rnd.choice(TARGETS)})
                    if r.status_code != 200:
                        errors += 1
                except httpx.HTTPError:
                    errors += 1
                latencies.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        await asyncio.gather(*(one(k) for k in range(requests)))
        wall = time.perf_counter() - t0
    latencies.sort()
    pct = lambda p: round(latencies[min(len(latencies) - 1, int(p / 100 * len(latencies)))], 1)  # noqa: E731
    return {
        "requests": requests,
        "concurrency": concurrency,
        "errors": errors,
        "wall_s": round(wall, 2),
        "rps": round(requests / wall, 1),
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
        "max_ms": round(latencies[-1], 1),
        "mean_ms": round(statistics.mean(latencies), 1),
    }


def cold_rank(base: str) -> float:
    """First ranking after start: every character's state comes from ESI (no snapshots yet)."""
    t0 = time.perf_counter()
    httpx.get(f"{base}/api/rank", params={"target": "Jita", "refresh": 2}, timeout=60).raise_for_status()
    return round((time.perf_counter() - t0) * 1000, 1)


def import_characters(base: str, eve: str, n: int) -> dict:
    chars = httpx.get(f"{eve}/_admin/characters").json()[:n]
    entries = []
    for ch in chars:
        tok = httpx.post(f"{eve}/_admin/mint", json={"character_id": ch["character_id"]}).json()
        entries.append({"refresh_token": tok["refresh_token"]})
    bundle = {"schema_version": 1, "client_id": "test-client-id", "characters": entries}
    r = httpx.post(
        f"{base}/api/characters/import",
        headers=H,
        timeout=60,
        files={"file": ("b.json", json.dumps(bundle).encode(), "application/json")},
    )
    return r.json()["summary"]


def run(args: argparse.Namespace) -> dict:
    systems = "30000142,30002187,30002813,30002510,30002659,30002053,30004759,30000144,30005000,30045346"
    report: dict = {
        "label": args.label,
        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": {"python": platform.python_version(), "platform": platform.platform()},
        "params": {
            "characters": args.characters,
            "requests": args.requests,
            "concurrency": args.concurrency,
            "esi_delay_ms": args.esi_delay_ms,
        },
    }
    with fake_eve(characters=args.characters, systems=systems) as eve:
        httpx.post(f"{eve}/_admin/config", json={"esi_delay_s": args.esi_delay_ms / 1000})
        port = free_port()
        data = Path(args.data_dir) if args.data_dir else ROOT / ".perf-data" / args.label
        if data.exists():
            import shutil

            shutil.rmtree(data)
        env = evedm_env(data, eve, port, EVEDM_WEB_AUTH="none")
        if args.docker:
            name = f"evedm-perf-{port}"
            envs = [
                x
                for k, v in env.items()
                if k.startswith(("EVEDM_", "EVE_")) and k != "EVEDM_DATA_DIR"
                for x in ("-e", f"{k}={v}")
            ]
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)  # noqa: S603, S607
            data.mkdir(parents=True, exist_ok=True)
            data.chmod(0o777)  # container runs as uid 10001
            subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    "host",
                    "--memory",
                    "1g",  # noqa: S603, S607
                    "--cpus",
                    "2",
                    "-v",
                    f"{data}:/app/data",
                    *envs,
                    args.docker,
                ],
                check=True,
                capture_output=True,
            )
            base = f"http://127.0.0.1:{port}"
            try:
                wait_http(f"{base}/api/health", 40)
                time.sleep(1)
                report["idle"] = docker_status(name)
                report["import"] = import_characters(base, eve, args.characters)
                report["cold_rank_ms"] = cold_rank(base)
                cpu0 = docker_cpu_s(name)
                report["load"] = asyncio.run(drive(base, args.requests, args.concurrency))
                report["load"]["server_cpu_s"] = round(docker_cpu_s(name) - cpu0, 2)
                report["after"] = docker_status(name)
                stats = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{json .}}", name],  # noqa: S603, S607
                    capture_output=True,
                    text=True,
                ).stdout
                report["docker_stats"] = json.loads(stats) if stats.strip() else {}
                report["image"] = args.docker
            finally:
                subprocess.run(
                    ["docker", "logs", name],
                    stdout=open(data / "container.log", "w"),  # noqa: S603, S607, SIM115
                    stderr=subprocess.STDOUT,
                )
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)  # noqa: S603, S607
        else:
            data.parent.mkdir(parents=True, exist_ok=True)
            with evedm_server(env, cpus="0,1", log_path=data.parent / f"{args.label}-server.log") as (base, proc):
                time.sleep(1)
                report["idle"] = proc_status(proc.pid)
                report["import"] = import_characters(base, eve, args.characters)
                report["cold_rank_ms"] = cold_rank(base)
                cpu0 = proc_cpu_s(proc.pid)
                report["load"] = asyncio.run(drive(base, args.requests, args.concurrency))
                report["load"]["server_cpu_s"] = round(proc_cpu_s(proc.pid) - cpu0, 2)
                report["after"] = proc_status(proc.pid)
        report["fake_calls"] = httpx.get(f"{eve}/_admin/stats").json()["calls"]
    load = report["load"]
    load["server_cores_avg"] = round(load["server_cpu_s"] / load["wall_s"], 2)
    report["peak_rss_mb"] = round(report["after"].get("VmHWM", 0), 1)
    report["budget"] = {
        "peak_rss_mb_max": args.max_rss,
        "p95_ms_max": args.max_p95,
        "ok": report["peak_rss_mb"] <= args.max_rss and load["p95_ms"] <= args.max_p95 and load["errors"] == 0,
    }
    return report


def write(report: dict) -> Path:
    out = ROOT / "logs" / "perf"
    out.mkdir(parents=True, exist_ok=True)
    stamp = report["started_at"].replace(":", "").replace("-", "")
    path = out / f"{stamp}-{report['label']}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    load = report["load"]
    md = [
        f"# Footprint run `{report['label']}` ({report['started_at']})",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Characters | {report['params']['characters']} |",
        f"| Requests / concurrency | {load['requests']} / {load['concurrency']} |",
        f"| ESI latency (fake) | {report['params']['esi_delay_ms']} ms |",
        f"| Idle RSS | {report['idle'].get('VmRSS', 0):.1f} MB |",
        f"| Cold ranking (all characters from ESI) | {report.get('cold_rank_ms')} ms |",
        f"| Peak RSS | {report['peak_rss_mb']} MB |",
        f"| Throughput | {load['rps']} req/s |",
        f"| Latency p50 / p95 / p99 | {load['p50_ms']} / {load['p95_ms']} / {load['p99_ms']} ms |",
        f"| Server CPU (avg cores) | {load['server_cores_avg']} |",
        f"| Errors | {load['errors']} |",
        f"| Budget met | {'yes' if report['budget']['ok'] else 'NO'} |",
        "",
    ]
    (out / f"{stamp}-{report['label']}.md").write_text("\n".join(md), encoding="utf-8")
    return path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="local")
    p.add_argument("--docker", help="image to run with --memory 1g --cpus 2 instead of a local process")
    p.add_argument("--characters", type=int, default=50)
    p.add_argument("--requests", type=int, default=600)
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--esi-delay-ms", type=int, default=40)
    p.add_argument("--max-rss", type=float, default=200.0)
    p.add_argument("--max-p95", type=float, default=1500.0)
    p.add_argument("--data-dir")
    args = p.parse_args()
    report = run(args)
    path = write(report)
    print(json.dumps({k: report[k] for k in ("label", "idle", "peak_rss_mb", "load", "budget")}, indent=2))
    print(f"report: {path}")
    return 0 if report["budget"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
