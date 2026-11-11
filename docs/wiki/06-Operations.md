# 06 · Operations

| Task | Command |
| --- | --- |
| Health | `curl http://localhost:8080/api/health` (status, RSS, ESI stats, universe build, war watcher, scout, alert feed, notification lag summary, event-loop lag) |
| Notification delay | `docker exec evedm evedm notifications lag [--hours 24]` (database only, no network; see "A notification arrives late") |
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
| Thousands of unknown structures, nearly all refused by ESI (403) | Older versions stored the characters' own ships and containers as structures (fittings, cargo and drone bays point at them). The scout now takes only Hangar, Deliveries and AssetSafety locations that are not the character's own items; every character is read again after the upgrade and the false entries are purged automatically on that scout run. `evedm structures stats` shows `filter_pending` (characters not yet re-read) and `purged_own_items` / `purged_orphans`; `evedm structures probe` shows what the asset locations are |
| Resolve unknown ends with "ESI kept answering with errors" | three passing ESI failures in a row (network, 5xx, 429): nothing was marked checked; start it again later |
| Thera / Turnur shows no systems for a hub | `GET /api/evescout`: check the per-hub counts, `EVEDM_EVESCOUT_URL` and `evescout.hubs`; the list is cached 15 minutes and J-space exits are skipped |
| A notification arrives late | run `docker exec evedm evedm notifications lag --hours 24` and read `reading`. The delay is the sum of: **ESI's cache window** (a character's list is asked again when ESI's copy expires, 600 s: median about 300 s, at most about 600 s, nothing can be done about it); **ESI data age** (`data_age_s`: how old the list already was when ESI sent it; high means ESI itself is late); **late reads** (`late_s` and the `read_late` split: the read started later than planned, because the app was down or restarted, the event loop was blocked (`/api/health` `event_loop.max_s`, WARNING `event loop blocked` in the log) or ESI answered slowly); **errors** (`reads.errors`, `characters.not_read_last_hour`: the character was not read at all; a 420 waits exactly until the error limit ends, other passing errors `retry_s` 60); **the gate** (`characters.excluded_by_gate`: see the next row). The timestamp of a notification is to the minute, so the measured delay is up to a minute too long |
| Some characters get no notifications | `evedm notifications lag` lists them in `characters.excluded_by_gate`: a character with fewer than `notifications.min_structures` (default 1; it was 10 earlier) player structures holding assets is not read (`EVEDM_NOTIFICATIONS_MIN_STRUCTURES=0` reads everybody, also before the first asset scan). Also needed: the Notifications tick on the Characters page, the scope `esi-characters.read_notifications.v1` and a valid token. The log says `notifications: reading N of M registered characters` whenever the count changes; `GET /api/health` shows `notifications.lag.excluded_by_gate` |
| `evedm structures stats` shows `dock_search.capped` above 0, `first_pass_incomplete` stays above 0, or "can dock, no assets" looks incomplete | `capped_terms` counts terms (a full system name) that still returned `dock_search_cap` structures or more: what came back is kept but the list for that system may be cut off. A character needs about 21 runs for its first pass (`structures.dock_search_max_terms` 200 per run); `characters_searched`, `passes` and `with_errors` show progress (403/401: the scope is missing or the token was revoked, Add character again). Prefixes with punctuation (H-PA29 gives "H-P") are not verified live: run `evedm structures search-probe --self-test` and apply its `recommendation` (`dock_search_punctuated: full`) |
| Ansiblex mode shows no gates | the character has not granted `esi-search.search_structures.v1` (Add character again) or sees no gate in the structure search; `ansiblex.refresh_hours` (6) controls re-reading |
| `data directory /app/data is not writable by uid 10001` | Linux host folder owned by another user: `sudo chown -R 10001:10001 data`, or use a named volume |

## Structure scout diagnostics

`docker exec evedm evedm structures stats` prints the resolver's numbers from the database (no network). `docker exec evedm evedm structures probe` reads the asset pages of a few characters (`--characters 5`, `--character ID`, `--max-pages 50`, `--samples 5`) and prints, per `location_type` and `location_flag`, how many distinct location IDs are resolved, refused, new, gone or unknown to the database, and how many are the character's own items, to show which asset locations are really Upwell structures; it only reads ESI asset pages and writes nothing to the structure tables.

**Dock search.** Besides assets, the scout asks ESI's character search for the structures each character may dock at (`esi-search.search_structures.v1` and `esi-universe.read_structures.v1` both needed). Live ESI matches the **start** of a structure name, needs 3 or more characters and finds nothing for punctuation-only terms (" - " and " » " return 0), so each character is searched with the 3-character prefixes of all solar system names (about 4 100 distinct prefixes), its own region first. Settings under `structures:` in `config/evedm.yaml` (an old `dock_search_term` is ignored with a log line):

| Setting | Default | Meaning |
|---|---|---|
| `dock_search_enabled` | true | switch the search off (rows already stored age out) |
| `dock_search_hours` | 24 | after its first complete pass a character is searched this often; until then every scout run goes on (a passing ESI error does not hold it back) |
| `dock_search_prefix_len` | 3 | prefix length of system names, 3 or more |
| `dock_search_cap` | 500 | a result this long counts as cut off: the prefix is split into the full system names behind it |
| `dock_search_max_terms` | 200 | searches per character and due run; a stored cursor goes on at the next one, the end of the list completes a pass |
| `dock_search_punctuated` | `prefix` | `full`: systems whose prefix has punctuation (H-PA29) are searched by full name instead |

Load with the defaults: 200 searches per character and hourly run, so 38 characters send about 7 600 calls per hour (2 per second) until the first pass is done, about 21 runs or 21 hours; afterwards one due run per day, 7 600 calls per day (about 320 per hour), and each character is fully refreshed about every 21 days. If that refresh is too slow lower `dock_search_hours` (6 gives about 5 days at 30 000 calls per day). Searches are not cached in memory and are plain 200 answers (they cost no ESI errors).

`evedm structures stats` has a `dock_search` block (`terms`, `eligible_characters`, `characters_searched`, `first_pass_incomplete`, `passes`, `capped`, `capped_terms`, `with_errors`, `access_rows`, `dockable`, `dockable_without_assets`, `dockable_unresolved`). `docker exec evedm evedm structures search-probe --character ID [--term TEXT ...]` runs the given terms (default: " » ", the prefix and the full name of the character's last known system) and prints the `count`, `hit_cap` and 5 `sample` IDs per term. `evedm structures search-probe --self-test [--character ID ...] [--limit 12]` (default: up to 5 characters with the most sightings) searches resolved structures the character sights or may dock at by their 3- and 4-character system prefix and full system name, grouped by name shape (plain letters, hyphen early or late, space, leading digit, J-space), reports for each shape how often the known structure ID was found, and prints a `recommendation` and `recommended_settings` (for example `dock_search_punctuated: full`) plus an `ansiblex` section: the live " » " count for up to 3 characters with stored gates next to the gates stored for them. A miss can also mean the character lost docking there (assets do not prove access), so only shapes where the full name hits and the prefix misses count against the prefix. Nothing is stored (a token refresh may be written).

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
