# Architecture Guidelines

* **Persistent Storage**: Use a standalone SQLite one-file database mapped as a Docker volume. Ensure the database is persisted in the `data/` directory (mapped to `/app/data`) between runs.
* **SQLite Optimization**: Enable WAL (Write-Ahead Logging) mode for the SQLite database to improve concurrency and prevent database locks.
* **Stable Dependencies**: Only use stable, well-maintained modules, libraries, and APIs. Verify stability before integrating.
* **Dependency Validation**: Explicitly verify that all necessary dependencies are included in `requirements.txt` and that all resource paths are fully qualified and verified.
* **Docker Best Practices**: Use multi-stage builds and non-root users where possible to enhance security.

## How EVE Distance Meter applies them

| Guideline | Implementation | Verified by |
| --- | --- | --- |
| Persistent storage | One file `evedm.sqlite3` in `data/` locally and `/app/data` in the image (`VOLUME ["/app/data"]`, compose `./data:/app/data` or a named volume) | `scripts/docker-smoke.sh` (restart keeps data), `test_restart_keeps_data_and_records_launches` |
| WAL | `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`; a probe write confirms WAL works, otherwise it falls back to `DELETE` with a warning; `wal_checkpoint(TRUNCATE)` on shutdown leaves one self-contained file | `test_database_defaults_to_wal_and_checkpoints_on_close`, smoke test checks `journal_mode` = `wal` |
| Stable dependencies | Only long-standing, maintained libraries (FastAPI, Starlette, Pydantic, uvicorn, httpx, Jinja2, PyYAML, cryptography, PyJWT, jsonschema); exact pins of stable releases in `requirements.txt` | `test_every_requirement_is_an_exact_stable_pin` |
| Dependency validation | `requirements.txt` covers every third-party import and every `pyproject.toml` dependency; installed versions equal the pins; `pip check` in the image build | `tests/unit/test_dependencies.py`, Dockerfile build stage |
| Fully qualified, verified paths | `evedm check` resolves data dir, database, logs, universe file, schema, templates and static files to absolute paths, checks they exist and that the data dir is writable; the server runs it before starting and logs the paths; the image runs `evedm check --no-data` at build time | `test_selfcheck_*`, Docker build, smoke test |
| Docker | Multi-stage build (virtualenv in the build stage, only the virtualenv copied), runs as `USER 10001:10001`, healthcheck, small context via `.dockerignore` | `scripts/docker-smoke.sh` (uid check) |

Linux host folders mounted at `/app/data` must be writable by uid 10001 (`sudo chown -R 10001:10001 data`); named volumes
and Docker Desktop folders on Windows need no action.
