# 06 · Operations

| Task | Command |
| --- | --- |
| Health | `curl http://localhost:8080/api/health` (status, RSS, ESI stats, universe build, war watcher, scout, alert feed) |
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
| Alerts page says "live feed not running" or shows `last error` | `docker logs evedm` for `zKillboard`: no internet from the container, or 403/429 (the feed pauses 10 minutes by itself). `GET /api/health` shows `alerts.polls` growing while it works |
| No **combat** alerts for a structure | combat needs the structure's position: it comes from the Structures page (a registered character has assets there) or the public dataset. Destroyed and firing work without it |
| **Set destination** greyed out | offline character, or it has not granted `esi-ui.write_waypoint.v1`: tick the scope on the EVE application, then Add character again |
| **Set destination** answers "ESI refused (404): is the character online?" | ESI only accepts waypoints for a logged-in character; the online state shown can be up to a minute old |
| `data directory /app/data is not writable by uid 10001` | Linux host folder owned by another user: `sudo chown -R 10001:10001 data`, or use a named volume |
