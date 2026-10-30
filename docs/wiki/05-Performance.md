# 05 · Performance (2 vCPU / 1 GB RAM)

Measured with `tests/perf/footprint.py`: fake EVE server with 40 ms ESI latency, characters imported through a bundle,
then mixed traffic (80 % ranking for 10 targets, 10 % map payload, 10 % autocomplete). The server is pinned to 2 CPUs
(`taskset`) or runs as a container with `--cpus 2 --memory 1g`. The load generator shares the same 2 CPUs, so the numbers
are conservative.

## Final results (0.3.0)

| Run | Chars | Idle RSS | Peak RSS | Throughput | p50 / p95 / p99 | Cold ranking |
| --- | --- | --- | --- | --- | --- | --- |
| Container, `--cpus 2 --memory 1g`, SQLite WAL, non-root | 50 | 73.8 MB | 77.5 MB (cgroup 55.2 MiB) | 422 req/s | 39 / 83 / 123 ms | 466 ms |
| Container, same, rollback journal (before WAL) | 50 | 73.9 MB | 77.2 MB (cgroup 54.5 MiB) | 246 req/s | 69 / 134 / 172 ms | 481 ms |
| Local process, 2 CPUs | 50 | 78.4 MB | 82.2 MB | 288 req/s | 60 / 115 / 154 ms | 464 ms |
| Soak, 6 000 requests, 59 s | 200 | 78.3 MB | 87.0 MB | 102 req/s (conc. 10) | 86 / 171 / 204 ms | 2.8 s* |

\* measured before ESI concurrency was raised from 10 to 20.

Memory stays under 8 % of 1 GB, and the container serves 400+ requests per second on 2 CPUs, far above what one player generates.

## Tuning log

| Step | Change | Effect (50 characters, 600 requests) |
| --- | --- | --- |
| Baseline | every ranking refreshed every character | 5.7 req/s, p95 9.4 s, ESI thundering herd |
| 1 | single-flight refresh round shared by concurrent rankings; ESI request coalescing; one DB transaction per round; `synchronous=NORMAL`; target history written at most once a minute | 185 req/s, p95 134 ms |
| 2 | stale-while-revalidate: answer from snapshots < 30 s old, refresh in the background every ≥ 5 s; Refresh button waits for fresh data | p99 1.3 s → 150 ms (cold start measured separately) |
| 3 | ESI concurrency 10 → 20, HTTP pool 32 | cold ranking 725 → 464 ms |
| 4 | CSR adjacency arrays, `malloc_trim` after start, map payload pre-built and gzip-cached with ETag | idle RSS −2 MB, map served in < 1 ms |
| 5 | SQLite WAL (architecture guidelines) with `synchronous=NORMAL`, checkpoint on shutdown | container 246 → 422 req/s, p95 134 → 83 ms |
| Image | single process, `MALLOC_ARENA_MAX=2`, bytecode precompiled, pip removed from the venv, non-root user | 65 MB compressed image |

Reports: `logs/perf/*.json` and `*.md`.
