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
| Ranking row shows `esi:420` | ESI's error limit was exhausted (often by the structure scout hitting 403s). The client pauses all ESI calls until the window resets and the rows recover by themselves; the scout stops at 60 errors left, the Resolve unknown job pauses there and goes on when the window resets |
| Resolve unknown says "Stopped: ESI error limit reached" (old versions) or shows "Waiting N s for ESI's error window" | No repeated clicks are needed any more: the button starts a background job that waits for ESI's error window and goes on until every structure is done. Leave the page open or come back later (the page shows a running job again after a reload); **Stop** cancels it. A long run is normal when most structures are refused: `evedm structures stats` shows how many unknown structures can still be asked (`askable`) and how many refusals are remembered (`refusal_pairs`); a refused (structure, character) pair is not asked again for `structures.no_access_refresh_hours` (336) |
| Resolve unknown ends with "ESI kept answering with errors" | three passing ESI failures in a row (network, 5xx, 429): nothing was marked checked; start it again later |
| Thera / Turnur shows no systems for a hub | `GET /api/evescout`: check the per-hub counts, `EVEDM_EVESCOUT_URL` and `evescout.hubs`; the list is cached 15 minutes and J-space exits are skipped |
| A notification arrives late | each character is read at ESI's cache expiry (`notifications.min_interval_s` 30 .. `character_interval_s` 600) and retried after `retry_s` 60 on a transient error; check the character's Notifications tick on the Characters page, and that it holds assets in at least `notifications.min_structures` (10) player structures (Structures page, or `GET /api/characters` field `structures`): one with fewer is not polled |
| Ansiblex mode shows no gates | the character has not granted `esi-search.search_structures.v1` (Add character again) or sees no gate in the structure search; `ansiblex.refresh_hours` (6) controls re-reading |
| `data directory /app/data is not writable by uid 10001` | Linux host folder owned by another user: `sudo chown -R 10001:10001 data`, or use a named volume |

## Structure scout diagnostics

`docker exec evedm evedm structures stats` prints the resolver's numbers from the database (no network). `docker exec evedm evedm structures probe` reads the asset pages of a few characters (`--characters 5`, `--character ID`, `--max-pages 50`, `--samples 5`) and prints, per `location_type` and `location_flag`, how many distinct location IDs are resolved, refused, new, gone or unknown to the database, and how many are the character's own items, to show which asset locations are really Upwell structures; it only reads ESI asset pages and writes nothing to the structure tables.

## Local structure index (names for structures nobody here may read)

`scripts/build_structure_index.py` (Python 3.10+, standard library only) builds `data/structure-index.jsonl`, the flat
list the Structures page falls back on. Run it on a machine that can reach the dataset hosts; the `data/` folder is
not in git and not in the Docker image, so copy the result into the volume mounted at `/app/data`.

```
python scripts/build_structure_index.py              # download everything, then build
python scripts/build_structure_index.py download     # only download; finished files are skipped on a re-run
python scripts/build_structure_index.py build        # only merge what is on disk
python scripts/build_structure_index.py --year 2025 --only everef,sde
python scripts/build_structure_index.py --list-file urls.txt   # EVE Ref history URLs, one per line, if the listing fails
```

| Downloaded to `data/structure-sources/` | From |
| --- | --- |
| `adam4eve/*.csv` | every file of `https://static.adam4eve.eu/IDs/` (refreshed on each run, one at a time: the host answers 429 to bursts) |
| `everef/structures-latest.v2.json` | `https://data.everef.net/structures/structures-latest.v2.json` (refreshed with If-Modified-Since) |
| `everef/history/<year>/...` | every file in every folder of `https://data.everef.net/structures/history/<year>/` (a finished file is never fetched again) |
| `sde/eve-online-static-data-<build>-jsonl.zip` | the latest CCP Static Data Export (`static-data/tranquility/latest.jsonl` names the build; older copies are removed) |

A structure ID seen in several files becomes one line: the newest observation wins field by field and older ones only
fill what it lacks. `data/structure-index.meta.json` records when it was built and how many observations each source
gave. The scout reads the list on every run; `structures.index_file` points elsewhere.
