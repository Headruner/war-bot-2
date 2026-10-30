# 01 · Requirements (SRS v0.3)

EVE Distance Meter answers one question: **when a notification names a solar system, which of your characters is the
fewest stargate jumps away, what is it flying, and which way would it go?** You then log that character in yourself.

Requirements use EARS phrasing. Priority: **M** Must, **S** Should, **C** Could. *Status* and *Verified by* trace each
requirement to the code and to the automated tests in this repository (0.3.0).

## Decisions

| ID | Decision |
| --- | --- |
| D1 | The existing bot is not modified or integrated. The user reads its Discord channel and gives the app the target system by typing it or pasting the message text. |
| D2 | One shared Python core with two front ends: a Web UI in Docker (built, 0.3.0) and a Desktop UI in PySide6 (planned, M3). |
| D3 | Every token comes from the user's own registered EVE application (client_id + secret known). |
| D4 | Routes are computed in the app: breadth-first search over the SDE stargate graph returns jump count and the full route. |
| D5 | Each instance keeps its own encrypted local store; characters move between instances with a JSON bundle. |
| D6 | Both UIs use the same SSO flow (authorization code + client secret), so bundles are interchangeable. |
| D7 | The star map is a 2D top-down projection of SDE coordinates, drawn by the app (canvas). No external map or route site. |
| D8 | The app never writes to the game client (no autopilot or waypoint calls); characters are assumed to be logged off. |

## Use cases

| ID | Use case | Built in 0.3.0 |
| --- | --- | --- |
| UC-01 | Add a character through EVE SSO | yes |
| UC-02 | Import characters from a JSON bundle | yes |
| UC-03 | Export characters to a JSON bundle | yes |
| UC-04 | Rank characters for a target system and see each route on the star map | yes |
| UC-05 | Get the target system from pasted text | yes |
| UC-06 | Manage characters (enable/disable, tags, remove) | yes |
| UC-07 | Re-authorize a character whose token was revoked | yes (via Add character) |
| UC-08 | Sign in to the Web UI | yes |

## Functional requirements

### SSO: add a character (UC-01, UC-07)

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| SSO-01 | The system shall provide an Add character action that starts the EVE SSO v2 authorization-code flow with the configured client_id, required scopes and a single-use random state. | M | done | test_sso_esi::test_authorize_url…, test_e2e step 4 |
| SSO-02 | The system shall leave character selection and scope approval to the EVE SSO page and never ask for EVE credentials. | M | done | design (no credential fields) |
| SSO-03 | When the callback arrives with a valid state, the system shall exchange the code, validate the JWT (JWKS signature, iss, aud = client_id, exp), take character ID and name from the token and store the character. | M | done | test_code_exchange_and_identity, test_verify_* |
| SSO-04 | If the callback state is missing, unknown, used or older than 10 minutes, then the system shall reject the callback. | M | done | test_sso_state_single_use_and_expiry, test_e2e step 5 |
| SSO-05 | If granted scopes lack a required scope, then the system shall store the character as Limited and list the missing scopes. | M | done | test_import_adds_updates_limits_and_rejects |
| SSO-06 | When a stored character is authorized again, the system shall replace its tokens and scopes and keep local settings. | M | done | test_upsert_keeps_local_settings |
| SSO-07 | Where the instance is the Web UI, the callback shall be `https://<host>/sso/callback`. | M | done | config `EVE_CALLBACK_URL` |
| SSO-08 | Where the instance is the Desktop UI, the system shall use a loopback callback listener. | M | planned (M3) | — |

### JSON bundle (UC-02, UC-03)

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| IMP-01 | The system shall import characters from a UTF-8 JSON file that conforms to schema `evedm-characters` v1. | M | done | test_example_bundle_from_the_spec_is_valid |
| IMP-02 | If the file is over 1 MB, not JSON, or not schema-valid, then the system shall reject it whole and report each violation with its JSON path. | M | done | test_file_level_rejections, test_schema_errors_report_json_paths |
| IMP-03 | If the bundle client_id differs from the configured one, then the system shall reject the whole file. | M | done | test_bundle_from_another_application_is_rejected |
| IMP-04 | When an entry has an access token, the system shall verify it (expired allowed for identity) and reject the entry if character_id disagrees. | M | done | test_import_adds_updates_limits_and_rejects |
| IMP-05 | The system shall refresh every entry before storing it and reject only that entry on failure. | M | done | same + test_e2e step 12 |
| IMP-06 | When a refresh returns a new refresh token, the system shall store the new one. | M | done | test_import_stores_rotated_refresh_token |
| IMP-07 | When an imported character exists, the system shall update tokens and keep local enabled/tags. | M | done | test_import_adds_updates… (second import) |
| IMP-08 | The system shall show a report: Added, Updated, Limited, Rejected (with reason). | M | done | test_e2e step 6, UI characters page |
| IMP-09 | The system shall not keep the uploaded file nor log token values. | M | done | test_e2e step 13 (logs scanned for tokens) |
| EXP-01 | The system shall export all or selected characters with refresh tokens and still-valid access tokens. | M | done | test_export_then_import_round_trip |
| EXP-02 | Before exporting, the system shall warn that the file contains credentials and require confirmation. | M | done | test_export_requires_confirmation (`confirm: true`) |
| EXP-03 | The system shall offer a passphrase-encrypted bundle (schema v2). | C | planned | — |

### Token lifecycle

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| TOK-01 | The system shall store tokens encrypted at rest (key from `EVEDM_SECRET_KEY`). | M | done | test_cipher_roundtrip_and_wrong_key |
| TOK-02 | When a token has < 60 s left, the system shall refresh it before use and persist the result. | M | done | test_expiring_token_is_refreshed… |
| TOK-03 | The system shall refresh one character at a time (per-character lock). | M | done | code review (asyncio.Lock per character) |
| TOK-04 | If refresh returns invalid_grant, then the system shall mark the character Re-auth required and exclude it from ranking. | M | done | test_ranking_excludes_disabled_and_reauth, test_e2e step 10 |
| TOK-05 | When a character is removed, the system shall delete its tokens and revoke the refresh token. | S | done | test_remove_revokes_and_deletes |

### Target input (UC-04, UC-05)

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| INP-01 | The system shall autocomplete solar system names (case-insensitive, from 2 characters). | M | done | test_resolver::test_autocomplete_*, test_ui |
| INP-02 | The system shall extract candidate systems from pasted text: Dotlan links, in-game `showinfo:5//` links, zKillboard system links, whole-word names including multi-word names. | M | done | test_resolver::* |
| INP-03 | With one candidate the system shall rank immediately; with several it shall offer a choice; with none it shall say so. | M | done | test_ui::test_autocomplete_and_paste |
| INP-04 | The system shall keep the last 20 targets for one-tap reuse. | S | done | test_recent_targets_keeps_last_20 |
| INP-05 | Where the instance is the Web UI, the system shall accept `?target=<name>` and keep it in the address bar. | S | done | test_ui (url check) |

### Routing

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| RTE-01 | The system shall compute jumps as the shortest stargate path and return the ordered route for each character. | M | done | test_shortest_routes |
| RTE-02 | The system shall run one BFS per target and read every character's route from it. | M | done | ranking.py (`_parents`, cached per target) |
| RTE-03 | If there is no stargate path (J-space, Pochven), then the system shall show No gate route and rank the character last. | M | done | test_wormhole_and_pochven_are_unreachable, test_ranking_order… |
| RTE-04 | The system shall not route through systems in `avoid_systems` (default Zarzakh); they stay valid origins/targets. | S | done | test_avoided_system_is_not_used_for_transit… |
| RTE-05 | The system shall load the universe from CCP static data and rebuild it with `evedm universe fetch`. | M | done (manual command) | test_build_from_current_sde_jsonl |
| RTE-07 | The system shall offer Safer / LessSecure routes through ESI `POST /route`. | C | planned | — |

### Ranking and star map (UC-04)

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| RNK-01 | When a target is set, the system shall fetch location, ship and online state for all enabled characters in parallel and show the ranked list. | M | done | test_ranking_order_route_and_flags |
| RNK-02 | Rows shall be sorted: reachable first, then jumps, then online before offline, then name. | M | done | same |
| RNK-03 | Each row shall show rank, name, jumps, system (security, region), docked / in space, ship type and name, online / last seen. | M | done | test_ui |
| RNK-04 | The system shall mark the first reachable character as Closest (jumps only; v0.3). | M | done | test_ranking_order… |
| RNK-05 | The system shall mark a character in a capsule as In pod. | S | done | test_ranking_order… |
| RNK-06 | If data is older than `stale_after_s`, then the row shall show Stale. | S | done | ranking.py |
| RNK-07 | If ESI fails for a character, then the system shall keep its last known data with an error badge. | M | done | test_ranking_keeps_last_known_data_on_esi_error |
| RNK-08 | The system shall provide Refresh (waits for fresh data) and auto-refresh every 30 s while visible. | M | done | rank.js |
| RNK-10 | The system shall reuse ESI responses until Expires and revalidate with ETag. | M | done | test_esi_cache_then_etag_revalidation, test_e2e step 8 |
| MAP-01 | The ranking view shall show the character list on the left and a 2D star map on the right; on phones the map is above the list. | M | done | test_ui::test_phone_layout… |
| MAP-02 | The map shall draw all k-space systems at their SDE positions (north up), coloured by security, with gate links. | M | done | test_ui (canvas pixels), screenshots |
| MAP-03 | When a character is selected, the map shall draw its route, mark origin and target, and zoom to fit. | M | done | test_ui::test_ranking_list_and_map |
| MAP-04 | When ranking completes, the Closest character shall be selected automatically. | M | done | same |
| MAP-05 | The map shall support wheel / pinch zoom, drag pan, and +, −, fit buttons. | M | done | map.js |
| MAP-06 | Hovering a system shall show its name, security, region and characters there. | S | done | map.js |
| MAP-07 | The map shall mark every character's current system. | S | done | map.js (diamonds) |
| MAP-08 | A route bar shall list the route system by system with security, and count high / low / null systems. | M | done | test_ui (route bar text) |

### Characters and Web UI

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| CHR-01 | The system shall list characters with status, scopes, tags and the last 4 characters of the refresh token. | M | done | test_ui::test_characters_page… |
| CHR-02..04 | Enable/disable, tags, remove with confirmation. | M | done | test_character_patch_and_delete |
| WEB-01 | The Web UI shall require EVE SSO sign-in and admit only `allowed_login_characters`. | M | done | test_e2e steps 1–3, test_sso_mode_requires_sign_in |
| WEB-02 | Sessions use HttpOnly, SameSite=Lax cookies (Secure when `cookie_secure`), expiring after `session_days`. | M | done | app.py |
| WEB-03 | The ranking view shall work on a 360 px screen without horizontal scrolling. | M | done | test_ui::test_phone_layout… |
| WEB-04 | The Web UI shall ship as one Docker image (multi-stage, non-root user 10001) with a one-file SQLite database in WAL mode and logs on a volume mounted at `/app/data` (`data/` on the host). | M | done | scripts/docker-smoke.sh |
| ARC-01 | The system shall pin every runtime dependency to a stable release in `requirements.txt` and verify imports against it. | M | done | tests/unit/test_dependencies.py |
| ARC-02 | At start-up and at image build, the system shall resolve every resource path to an absolute path, verify it exists (data dir writable) and log it. | M | done | test_selfcheck_*, `evedm check` |
| WEB-05 | The Web UI shall refuse to start without `EVEDM_SECRET_KEY`, `EVE_CLIENT_ID`, `EVE_CLIENT_SECRET` (demo mode needs only the key). | M | done | test_validate_for_serve_requires_secrets |
| LOG-01 | Every launch shall be recorded in the `launches` table and in `logs/launches.jsonl` (start and stop, RSS, requests). | M | done | test_launch_is_recorded, test_restart_keeps_data… |

### Accounts: mains and linked characters (UC-09, added after 0.3.0)

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| USR-01 | When a character signs in through EVE SSO, the system shall create an account for it (the main), ask for the required scopes and link the main to itself. | M | done | test_multi_user step 1 |
| USR-02 | When a signed-in user adds or imports a character, the system shall link it to that user's main. | M | done | test_multi_user step 2, test_users |
| USR-03 | The system shall show, rank, export and change only the characters linked to the signed-in main; recent targets are per main. | M | done | test_multi_user steps 3–4, test_links_are_scoped_per_main |
| USR-04 | The same EVE character may be linked to several mains; each link keeps its own tokens, tags and enabled flag, and removing it for one main keeps the others. | M | done | test_links_are_scoped_per_main |
| USR-05 | The session cookie shall keep a main signed in for `session_days` (default 90) after the last visit; it carries only the main's ID, tokens stay encrypted in the database. | M | done | test_multi_user step 5 |
| USR-06 | The main cannot be removed on its own; Delete account removes and revokes every link of the main and ends its sessions. | S | done | test_multi_user steps 4 and 7 |
| USR-07 | A v1 database migrates in place; its characters belong to owner 0 (local user, demo, CLI) until `evedm users adopt --to <main>`. | M | done | test_v1_database_is_migrated_to_owner_0, test_reassign_owner… |

### War watcher (UC-10, merged from eve-war-bot-2)

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| WAR-01 | Where `enabled` in `config/wars.yaml`, the system shall poll ESI `/wars/` every `poll_interval_min` minutes in the background. | M | done | test_wars (cycle tests), WarService.start |
| WAR-02 | On a clean database, the system shall keep wars declared in the last `bootstrap_hours`; later it shall keep only new IDs and heal a watermark ahead of ESI. | M | done | test_clean_db_posts_last_24h_once, test_heals_test_data_watermark |
| WAR-03 | If a new war ID was declared more than `max_war_age_hours` ago (re-issued war), then the system shall store it as stale and not show it. | M | done | test_reissued_old_wars_are_stale |
| WAR-04 | Where `statuses` is set, the system shall keep only those war types. | S | done | test_statuses_filter |
| WAR-05 | For each kept war, the system shall build parties, report (score, allies, recent kills, hot systems) and public structures of both sides with their systems. | M | done | test_structures_give_rankable_systems |
| WAR-06 | The Wars page shall list wars newest first with the systems of their structures and hot systems; choosing one opens the ranking for that system. | M | done | test_wars_page_and_api_in_demo, browser check |
| WAR-07 | Where `discord.enabled`, the system shall post new wars in batches, honour 429 `retry_after`, and park a war as failed after 5 failed posts. | S | done | test_failed_post_is_retried_then_parked, test_notifier_waits… |
| WAR-08 | War settings shall live in their own file (`config/wars.yaml`, `$EVEDM_WARS_CONFIG`), secrets in `.env` (`EVEDM_WARS_DISCORD_WEBHOOK`). | M | done | test_wars_config_file_env_and_validation |
| WAR-09 | The system shall offer `evedm wars run|list|stats|check`, including monthly statistics. | S | done | test_cli_wars_list_stats_check |
| WAR-10 | A transient ESI error shall not drop a war; it is retried on the next cycle. | M | done | test_transient_esi_error_does_not_skip_war |

## Non-functional requirements

| ID | Requirement | Result (0.3.0) |
| --- | --- | --- |
| NFR-01 | Tokens encrypted at rest; secrets from env; no tokens in logs; HTTPS on a private network. | Fernet at rest; log redaction filter; integration test scans logs |
| NFR-02 | BFS ≤ 50 ms; ranking of 50 characters ≤ 3 s cold; UI never blocks. | BFS 2 ms; cold ranking 0.48 s (50 characters, 40 ms ESI latency) |
| NFR-03 | ESI: User-Agent, X-Compatibility-Date, rate-limit headers, Retry-After on 429, back-off on 420. | implemented, tested |
| NFR-04 | Works without access to CCP static data at runtime. | universe file bundled in the package |
| NFR-08 | Runs within 2 vCPU / 1 GB RAM. | container peak RSS 77.5 MB (cgroup 55 MiB), p95 83 ms at 422 req/s (WAL) |
| NFR-06 | Core unit tests for BFS, resolver, bundle, sort order, token refresh. | 95 unit, 7 integration (3 process + 4 browser), 1 footprint |

## Acceptance criteria → tests

| AC | Scenario | Test |
| --- | --- | --- |
| AC-01 | Add character via SSO | test_e2e::test_full_user_journey (step 4) |
| AC-02 | Import a valid bundle | test_e2e step 6 |
| AC-03 | Bundle from another application rejected | test_bundle_from_another_application_is_rejected |
| AC-04 | One revoked token in a bundle | test_import_adds_updates_limits_and_rejects |
| AC-05 | Order: Jita 0, Perimeter 1, Amarr n, J-space last | test_ranking_order_route_and_flags |
| AC-06 | Tie at equal jumps: online first | test_ranking_order_route_and_flags |
| AC-07 | Paste a Dotlan link → ranked immediately | test_ui::test_autocomplete_and_paste |
| AC-08 | Ambiguous paste offers a choice | test_ambiguous_text_returns_all_candidates_in_order + rank.js |
| AC-09 | Expired access token refreshed transparently | test_expiring_token_is_refreshed… |
| AC-10 | Revoked token → Re-auth required, others still ranked | test_e2e step 10 |
| AC-11 | Export / import round trip between instances | test_e2e step 12 |
| AC-12 | Web UI access control | test_e2e steps 1–3, test_sso_mode_requires_sign_in |
| AC-13 | Clicking a character draws its route on the map | test_ui::test_ranking_list_and_map |
| AC-14 | Runs within 2 vCPU / 1 GB | tests/perf + scripts/docker-smoke.sh |
