# 03 · Testing

| Suite | Path | What runs | Count |
| --- | --- | --- | --- |
| Unit | `tests/unit` | In-process: universe/BFS, resolver, config, crypto, DB, SSO + ESI against the fake (ASGI transport), bundle, ranking, accounts (signed session cookies), war watcher, war census and high-sec structures, web API (TestClient), CLI, dependency validation | 190 |
| Integration | `tests/integration/test_e2e.py`, `test_multi_user.py` | Real `evedm serve` process + real fake EVE SSO/ESI process: sign-in, add characters, import, rank, ESI caching, export, revocation, second instance, restart, secrets-not-in-logs; several mains on one site; upgrade of a 0.3.0 database | 5 |
| Browser | `tests/integration/test_ui.py`, `test_ui_accounts.py` | Chromium (Playwright): list + map + route bar, autocomplete, paste, characters page, phone layout, Wars page (filter, rank a structure system, phone, statistics and structures tabs), browser-cookie settings, account UI (main badge, remove, delete account) | 10 |
| Footprint | `tests/perf` | 50 characters, 600 requests at concurrency 20, 40 ms ESI latency, server pinned to 2 CPUs; budget peak RSS ≤ 160 MB, p95 ≤ 1 s | 1 |
| Dependencies | `tests/unit/test_dependencies.py` | every import pinned in `requirements.txt`, exact stable pins, installed = pinned, absolute verified paths, WAL + checkpoint | 8 |
| Container | `scripts/docker-smoke.sh` | Image with `--cpus 2 --memory 1g`, host folder mounted at `/app/data`, health, ranking, WAL, `evedm check`, uid 10001, restart keeps data | script |

## Scenario matrix (accounts and war watcher)

Every row is an automated test. Rows marked *fix* found a defect that was corrected before release 0.4.0;
rows marked 0.5.0 cover the war census, the high-sec structure list and the browser-cookie settings.

| Area | Scenario | Expected | Test |
| --- | --- | --- | --- |
| Sign-in | new main | account created, main linked to itself, welcome page | test_sign_in_creates_the_account…, test_multi_user |
| Sign-in | returning main, renamed in game | links kept, name updated | test_sign_in_creates_the_account… |
| Sign-in | another main in the same browser | session switches account | test_sign_in_creates_the_account… |
| Sign-in | main not on the allow list | refused, nothing stored | test_allow_list_refuses_without_creating_anything |
| Sign-in | main removed from the allow list later | session ends (*fix*) | test_main_removed_from_the_allow_list_loses_its_session |
| Sign-in | reused / expired / missing state | `invalid_state`, nothing stored | test_add_needs_a_signed_in_main_and_a_fresh_state |
| Session | 90 days counted from the last visit | cookie re-signed once a day (*fix*: Starlette 1.x only re-sends a modified session) | test_session_is_renewed_daily… |
| Session | signature older than 90 days, forged, other key, malformed, unknown main | anonymous | test_expired_session_signature…, test_forged_or_malformed_sessions… |
| Session | cookie flags | HttpOnly, SameSite=Lax, Max-Age 90 days | test_session_is_renewed_daily… |
| Linking | add without a session | back to sign-in, nothing stored | test_add_needs_a_signed_in_main… |
| Linking | alt declines scopes | Limited with missing scopes | test_sign_in_creates_the_account… |
| Linking | redirect after add with spaces in the name | URL-encoded (*fix*) | test_sign_in_creates_the_account… |
| Isolation | list, rank, recent targets, export | only the caller's links | test_each_main_sees_and_ranks_only_its_own_links, test_export_contains_only… |
| Isolation | change or remove another main's link | 404 | test_foreign_links_cannot_be_changed… |
| Isolation | remove own main | 400 | test_foreign_links_cannot_be_changed… |
| Shared character | settings and tokens per link, snapshot shared, removed with the last link | as stated | test_links_are_scoped_per_main, test_foreign_links_cannot_be_changed… |
| Shared character | one main's link revoked | the other main sees no error (*fix*) | test_one_mains_broken_link_does_not_show… |
| Shared character | one link lacks the ship scope | does not blank the ship the other link fetched (*fix*) | test_link_without_ship_scope_keeps_the_ship… |
| Account | delete account | links revoked and removed, every session of that main ends, other mains untouched | test_account_deletion_ends_every_session…, test_ui_accounts |
| Input | invalid JSON, non-object JSON, non-boolean `enabled`, bad IDs, missing confirm | 400, never 500 (*fix*) | test_bad_input_is_a_4xx_never_a_500 |
| Input | state change without the X-EVEDM header | 403 | test_state_changes_need_the_csrf_header |
| Upgrade | 0.3.0 database (schema v1) | migrated in place to owner 0, FK check clean | test_v1_database_is_migrated_to_owner_0 |
| Upgrade | legacy main signs in | takes over the legacy characters and recent targets (*fix*: they were hidden) | test_upgrade_from_single_user…, test_upgrade_from_a_single_user_database |
| Upgrade | other main signs in first / demo characters | gets nothing / demo stays local | test_upgrade_from_single_user…, test_adopting_skips_demo… |
| Config | `${VAR:-default}` with VAR empty in `.env` | default applies (*fix*) | test_env_defaults_apply_to_empty_variables, test_more_config_cases |
| Config | broken wars.yaml (YAML, type, ranges, unknown war type, URL) | clear error, `serve` exits 2 (*fix*) | test_more_config_cases, test_serve_reports_a_broken_wars_config |
| Wars | clean database, re-issued old IDs, type filter, watermark ahead of ESI | as the bot | test_clean_db…, test_reissued…, test_statuses_filter, test_heals… |
| Wars | more new wars than `max_new_per_cycle` | rest next cycle | test_new_wars_beyond_the_cycle_limit… |
| Wars | ESI down / empty list / 5xx then OK / war gone | loop survives, retried, skipped | test_background_loop_survives…, test_empty_or_failing_war_list, test_esi_retries…, test_war_gone… |
| Wars | summary fails | retried, parked after 5 | test_summary_failure_is_retried_then_parked |
| Wars | stop during a stuck ESI call | cancelled promptly | test_stop_cancels_a_cycle_stuck_in_esi |
| Wars | no structure data | hot systems still offered | test_hot_systems_are_targets_without_structure_data |
| Structures | dataset in any chunking, BOM, multi-byte names, array or object, malformed entries | parsed (streamed) | test_streaming_parser_handles_any_chunk_boundary |
| Structures | truncated, invalid, oversized, server error | previous index kept | test_streaming_parser_rejects…, test_structure_refresh_failures… |
| Structures | memory | peak below the file size, under half of `json.loads` (*fix*: whole-file parse) | test_streaming_parser_peak_memory_stays_small |
| Discord | invalid webhook | wars stay queued and listed | test_invalid_webhook_keeps_wars_queued_and_visible |
| Discord | 429, 400, network error, dry run | retry / error / error / no network | test_notifier_errors, test_dry_run… |
| Discord | webhook URL in logs, errors, health | redacted; health shows no error text (*fix*) | test_discord_webhook_tokens_are_redacted, test_background_loop…, test_health_is_public… |
| Wars page | bounds, type filter, sign-in required, disabled watcher | as stated | test_wars_api_bounds, test_wars_api_needs_sign_in…, test_wars_disabled_hides_page |
| Wars page | demo wars when the database goes live | removed | test_demo_wars_are_removed…, test_demo_census_is_removed… |
| Census (0.5.0) | first scan in pages, new IDs, a page with a failed read, unknown IDs (404) | page retried, 404 kept as known and inactive | test_scan_walks_back…, test_unknown_war_is_a_tombstone… |
| Census | scan depth limit | stops at `census_max_ids` | test_scan_stops_at_census_max_ids |
| Census | war retracted / finished meanwhile, score changes | re-read when due, drops out of the active set | test_active_wars_are_reread_and_drop_out… |
| Statistics | totals, states, top aggressors/defenders, destructive wars, structures | as stated | test_statistics, test_stats_and_structures_api_in_demo |
| High-sec structures | both sides, allies, alliance member corps, low-sec and bystanders excluded, state precedence | as stated | test_loot_lists_high_sec_structures… |
| High-sec structures | jumps from the caller's nearest character, per main | multi-source BFS equals per-target BFS | test_structure_jumps_are_per_main, test_nearest_matches_single_source_bfs |
| High-sec structures | filters, sort, paging, bad parameter, structure data not loaded | as stated / 422 / empty with a note | test_loot_lists…, test_stats_and_structures_api_in_demo, test_loot_is_empty_until… |
| Census off | tabs and APIs hidden | 404 | test_census_off_hides_its_tabs |
| Browser settings | tab, filters, map options, auto-refresh survive a reload; other browser has defaults; broken cookie | remembered / defaults / defaults without errors | test_wars_tabs_and_filters_survive_a_reload, test_map_options_and_auto_refresh_are_remembered, test_broken_prefs_cookie… |

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
