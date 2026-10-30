# 03 · Testing

| Suite | Path | What runs | Count |
| --- | --- | --- | --- |
| Unit | `tests/unit` | In-process: universe/BFS, resolver, config, crypto, DB, SSO + ESI against the fake (ASGI transport), bundle, ranking, web API (TestClient), CLI, dependency validation | 95 |
| Integration | `tests/integration/test_e2e.py` | Real `evedm serve` process + real fake EVE SSO/ESI process: sign-in, add characters, import, rank, ESI caching, export, revocation, second instance, restart, secrets-not-in-logs | 3 |
| Browser | `tests/integration/test_ui.py` | Chromium (Playwright) against a demo server: list + map + route bar, autocomplete, paste, characters page, phone layout | 4 |
| Footprint | `tests/perf` | 50 characters, 600 requests at concurrency 20, 40 ms ESI latency, server pinned to 2 CPUs; budget peak RSS ≤ 160 MB, p95 ≤ 1 s | 1 |
| Dependencies | `tests/unit/test_dependencies.py` | every import pinned in `requirements.txt`, exact stable pins, installed = pinned, absolute verified paths, WAL + checkpoint | 8 |
| Container | `scripts/docker-smoke.sh` | Image with `--cpus 2 --memory 1g`, host folder mounted at `/app/data`, health, ranking, WAL, `evedm check`, uid 10001, restart keeps data | script |

## Fake EVE server

`tests/fake_eve.py` implements the EVE SSO endpoints (authorize, token, revoke, JWKS, metadata) with **real RS256 JWTs**
and the ESI endpoints the app uses (location, ship, online, universe/names) with `Expires`, `ETag`/304 and rate-limit
headers. Test-only `/_admin/*` endpoints mint tokens, revoke them, move characters and add ESI latency. It runs in-process
(httpx ASGI transport) for unit tests and as a separate process for integration and performance tests:

```
python tests/fake_eve.py --port 9100 --characters 50
```

## Running

```
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt && pip install -e . --no-deps && python -m playwright install chromium
scripts/run-tests.sh            # all suites; or: unit | integration | perf
```

## Logs

| File | Content |
| --- | --- |
| `logs/tests/<UTC>-<suite>.log` | full pytest output of each suite run by `scripts/run-tests.sh` |
| `logs/tests/<UTC>-<suite>.xml` | JUnit report |
| `logs/tests/test-runs.jsonl` | one line per pytest session (passed / failed / duration / platform), written by `tests/conftest.py` |
| `logs/perf/<UTC>-<label>.json/.md` | footprint reports (local process and container) |
| `logs/launches/` | sample launch logs: demo server and Docker smoke runs |

In CI every job uploads these folders as artifacts.
