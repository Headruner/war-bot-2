# Architecture Guidelines

* **Persistent Storage**: Use a standalone SQLite one-file database mapped as a Docker volume. Ensure the database is persisted in the `data/` directory (mapped to `/app/data`) between runs.
* **SQLite Optimization**: Enable WAL (Write-Ahead Logging) mode for the SQLite database to improve concurrency and prevent database locks.
* **Stable Dependencies**: Only use stable, well-maintained modules, libraries, and APIs. Verify stability before integrating.
* **Dependency Validation**: Explicitly verify that all necessary dependencies are included in `requirements.txt` and that all resource paths are fully qualified and verified.
* **Docker Best Practices**: Use multi-stage builds and non-root users where possible to enhance security.
