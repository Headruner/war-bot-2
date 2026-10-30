# 06 · Operations

| Task | Command |
| --- | --- |
| Health | `curl http://localhost:8080/api/health` (status, RSS, ESI stats, universe build) |
| Launch history | `docker exec evedm evedm launches` or `GET /api/launches` |
| Logs | `/app/data/logs/evedm.log` (rotating), `docker logs evedm` |
| Self-check | `docker exec evedm evedm check` (dependencies, absolute resource paths, data dir writable) |
| Rank from the console | `docker exec evedm evedm rank Tama` |
| Export characters (backup of tokens) | Characters page > Export JSON, or `docker exec evedm evedm export --out /app/data/backup.json` |
| Backup the DB | stop the container (the WAL is checkpointed on shutdown, so `evedm.sqlite3` is one complete file) and copy it; while running use `sqlite3 evedm.sqlite3 ".backup b.sqlite3"` |
| Update the universe after a game update | `docker exec evedm evedm universe fetch --out /app/data/universe.json.gz` then set `EVEDM_UNIVERSE_FILE=/app/data/universe.json.gz` and restart |

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `missing required settings` at start | set `EVEDM_SECRET_KEY`, `EVE_CLIENT_ID`, `EVE_CLIENT_SECRET` (demo needs only the key) |
| `cannot decrypt token: wrong EVEDM_SECRET_KEY?` | the key changed; restore it or re-add the characters |
| Character shows Re-auth required | its refresh token was revoked (EVE website, password change); use Add character again |
| Sign-in refused (`not_allowed`) | add your character ID to `EVEDM_ALLOWED_LOGIN_CHARACTERS` |
| `invalid_state` after sign-in | the login took longer than 10 minutes or the page was reloaded; start again |
| Log says `journal_mode=WAL unavailable` | the mounted filesystem cannot share WAL memory (some Windows bind mounts); the app already fell back to `delete`. Use a named volume to get WAL |
| `data directory /app/data is not writable by uid 10001` | Linux host folder owned by another user: `sudo chown -R 10001:10001 data`, or use a named volume |
