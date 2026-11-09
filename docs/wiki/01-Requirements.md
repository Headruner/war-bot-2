# 01 · Requirements (SRS v0.3)

EVE Distance Meter answers one question: **when a notification names a solar system, which of your characters is the
fewest stargate jumps away, what is it flying, and which way would it go?** You then log that character in yourself.

Requirements use EARS phrasing. Priority: **M** Must, **S** Should, **C** Could. *Status* and *Verified by* trace each
requirement to the code and to the automated tests in this repository (0.3.0, extended up to 0.8.0, plus the members-only tabs, Thera / Turnur, the structure scout rework, ESI error-limit handling and Python 3.13 / published-image deployment that follow it, marked "after 0.8.0").

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
| D8 | The app writes to the game client only when the user asks: **Set destination** sets the autopilot destination of an online character (`esi-ui.write_waypoint.v1`, revised in 0.7.0; until then the app never wrote to the client). Nothing else is sent, and nothing is sent on its own. |
| D9 | Structure attack timers are not public (only the owner corporation's directors get them). Structure alerts use public killmails from zKillboard's live feed instead: destroyed, combat on grid, structure firing (0.7.0). |
| D10 | A static roster entry is never sent to ESI: it has no tokens, no ship, no online state and never enters the refresh or rate-limit paths. Its only data are a name, a system and the moment it was uploaded (0.8.0). |
| D11 | One roster pool per main, merged on upload and keyed by the normalised character name: an upload adds new names and moves names already there. No named sets, no versions. |
| D12 | A roster entry whose system does not resolve is stored unresolved with its reason and up to three suggestions, not rejected; only the file as a whole can be rejected. |
| D13 | Roster characters are ranked on their own tab of the ranking view and listed on the Roster page; they never mix into the live list and never take the place of a live character, on the Alerts page included. |
| D14 | ESI notifications are used after all (this revises the 0.5.0 decision): asset-safety and structure notices reach the character itself, so no corporation role is needed. `esi-characters.read_notifications.v1` joins the **required** scopes; a character that does not grant it is Limited and is not polled. |
| D15 | ESI caches notifications 600 s per character, so the poller runs every minute and asks only for the characters whose cached copy has expired (the ESI client revalidates with ETag). A new notification is on the page within about a minute of ESI publishing it. |
| D16 | The **Notifications** and **Structures** tabs belong to one alliance (BB, ID 99010320): both are shown only to a main whose alliance is in `access.alliance_ids`. The alliance is read from ESI `/characters/{id}` at sign-in, stored on the account and re-read every `affiliation_hours`. For every other character the links are hidden and the pages and APIs answer 404 (after 0.8.0; before it only Notifications was gated, by `notifications.alliance_ids`). |
| D17 | The Notifications page is **site-wide**: it lists the notifications of every registered character that has a valid refresh token (granted the notifications scope, not Re-auth required), whoever linked it and whatever its ranking enabled switch says. Any member sees all of them, not only those of its own characters; the closest-character lists stay personal (after 0.8.0). |
| D18 | The Structures page lists every structure in which **any** registered character with a valid token holds an asset: ESI assets give the structure IDs (read page by page, only locations of type `other`), then ESI `/universe/structures/{id}` gives name, owner, type and system. Every structure found is **saved in the database at once and kept until it is destroyed or unanchored** (404); access to it is not needed to keep the row. The list is pooled for all members; which character revealed a structure is never shown. |
| D19 | The Ranking choice **Gates only / Thera / Turnur** (one radio button per hub) uses EVE-Scout's public signature list. For a character and a target the route through the chosen hub is: gates to the listed system **closest to the character**, the wormhole into the hub, the wormhole out to the listed system **closest to the target**, gates on to the target; among equally close listed systems the one with the **higher security** wins. The wormhole counts as two jumps. Each button uses only its own hub as the mid-system. The hub route replaces the gate route only when it is **strictly shorter**; when entry and exit are the same system the plain gate route is shorter and the hub is skipped. Connections marked *End Of Life* are kept; J-space exits are skipped (no gates). The list is cached for 15 minutes. The map shows two gate legs joined by a wormhole leg. Roster ranking is not changed. |

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
| UC-09 | Sign in with a main; link alts to it; stay signed in | 0.4.0 |
| UC-10 | Follow wars and high-sec structures of warring corporations | 0.4.0, statistics 0.5.0 |
| UC-11 | React to a structure alert: see what happened where, pick the closest character, set its destination | 0.7.0 |
| UC-12 | Upload a static roster (character name + system) and rank it against a target without ESI | 0.8.0 |
| UC-13 | Watch the ESI notifications of every registered character (asset safety, structures under attack), with the closest characters; members of the configured alliance only | 0.8.0, site-wide and gated after 0.8.0 |
| UC-14 | Browse the structures where any registered character has assets (assets, then structure info, both from ESI); members of the configured alliance only | 0.6.0, gated after 0.8.0 |
| UC-15 | Rank the characters through the Thera / Turnur wormholes listed by EVE-Scout and see the two-leg route on the map | after 0.8.0 |

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
| SSO-09 | The system shall request `esi-characters.read_notifications.v1` as a required scope; a character that does not grant it is stored Limited and is never polled for notifications. | M | done (0.8.0) | test_only_characters_that_granted_the_scope_are_polled, test_sign_in_creates_the_account_and_links_the_main |

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
| RTE-08 | When the user presses **Set destination** for a character, the system shall call ESI `POST /ui/autopilot/waypoint` with `destination_id` (the target system), `clear_other_waypoints=true`, `add_to_beginning=false`, using that character's token. The button is offered in the ranking route bar and on the Alerts page, disabled for offline characters and for characters without `esi-ui.write_waypoint.v1` (with the reason shown). Only the signed-in main's own characters can be steered (404 otherwise); ESI refusal → 502 "is the character online?", token problem → 409; demo mode answers "simulated". | S | done (0.7.0) | test_destination_requires_the_scope…, test_set_destination_in_demo_and_bad_input, test_esi_post_auth_sets_the_waypoint_in_the_fake, test_alerts_e2e, browser test |

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
| USR-07 | A v1 database migrates in place; its characters belong to owner 0 (local user, demo, CLI). | M | done | test_v1_database_is_migrated_to_owner_0 |
| USR-08 | When the first main that is one of the owner-0 characters signs in, the system shall move every non-demo owner-0 character and recent target to it; `evedm users adopt --to <main>` does the same by hand. | M | done | test_upgrade_from_single_user…, test_upgrade_from_a_single_user_database, test_cli_users_list_and_adopt |
| USR-09 | If a main is no longer on a non-empty `allowed_login_characters`, then its sessions shall end. | M | done | test_main_removed_from_the_allow_list_loses_its_session |
| USR-10 | A ranking shall show each character's error from the caller's own refresh, and a link without a scope shall not blank data another link fetched. | M | done | test_one_mains_broken_link…, test_link_without_ship_scope… |
| USR-11 | Malformed requests shall get a 4xx answer, never a 500. | M | done | test_bad_input_is_a_4xx_never_a_500 |

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
| WAR-11 | The structure dataset shall be parsed while it downloads (bounded memory); a failed, truncated or oversized download keeps the previous data. | M | done | test_streaming_parser_*, test_structure_refresh_failures… |
| WAR-12 | Discord webhook tokens shall never appear in logs, stored errors or the public health endpoint. | M | done | test_discord_webhook_tokens_are_redacted, test_health_is_public… |
| WAR-13 | Where `census_enabled`, the system shall follow every active war in New Eden: walk back through the newest `census_max_ids` war IDs (paged, `census_pages_per_cycle` per cycle, a failed page retried), add new IDs every cycle and re-read active wars every `census_refresh_hours`. A war is active while `finished` is empty or in the future. | M | done (0.5.0) | test_scan_walks_back…, test_unknown_war_is_a_tombstone…, test_scan_stops_at_census_max_ids, test_active_wars_are_reread… |
| WAR-14 | The Statistics tab shall show active wars (fighting, starting within 24 h, ending, mutual, declared in 24 h / 7 days), ships and ISK destroyed, top aggressors and defenders, most destructive wars, high-sec structures at war by type and region, and the census coverage. | M | done (0.5.0) | test_statistics, test_stats_and_structures_api_in_demo, browser test |
| WAR-15 | The High-sec structures tab shall list public high-sec structures (security ≥ 0.5) owned by the aggressor, defender or allies of an active war (alliance members included), with war state and jumps from the signed-in main's nearest character (one multi-source BFS), filterable by type, region, side, war state and security, sortable, paged. | M | done (0.5.0) | test_loot_lists_high_sec_structures…, test_structure_jumps_are_per_main, test_nearest_matches_single_source_bfs |
| STR-01 | The system shall ask at sign-in and Add character for the optional scopes `esi-universe.read_structures.v1` and `esi-assets.read_assets.v1` (`eve.optional_scopes`, `EVE_OPTIONAL_SCOPES`); characters without them are ranked as before and marked "structures not shared". | M | done (0.6.0) | test_sign_in_asks_for_structure_scopes…, test_optional_scopes_setting |
| STR-02 | The structure scout shall read the assets of every registered character that granted the asset scope (one link per character, re-read every `assets_refresh_hours`) **page by page**, save the structures of each page in the database immediately, skip structures already known, and take only locations of type `other` (structures) as candidates; containers, ships and NPC stations never qualify. A read cut short by `max_asset_pages` (default 1,000,000) is counted as truncated, not as an error. | M | done | test_scout (page-by-page saving, skip known, truncation), test_assets_reveal_structures_for_everyone |
| STR-03 | Candidates shall be read from `/universe/structures/{id}` with the token of a character that has assets there, trying the next on 403; 404 marks a non-structure; no access keeps the row and its last data. New candidates are read first; known ones are re-read every `structure_refresh_hours` (default 720; 0 = never) and refused ones every `no_access_refresh_hours` (default 336). Known structures are never pruned. | M | done | test_scout, test_unreadable_by_one_character_is_read_with_another |
| STR-04 | The Structures page shall list every resolved structure in all space (high, low, null, wormhole) to every member (ACC-01), with type, owner (zKillboard link), wars of the owner, last seen and how many characters see it, sorted by jumps from the caller's nearest character; search, space, region, type, at-war filters and sorting; filters kept in the browser cookie. Which character revealed a structure is never shown. | M | done (0.6.0) | test_structures_page_and_api_in_demo, test_structures_found_through_assets_are_shared, browser test |
| STR-05 | Structures found by the scout shall also feed the war structure lists (deduplicated with the public dataset). | S | done (0.6.0) | test_discovered_structures_join_the_war_lists, test_structures_page_and_api_in_demo |
| STR-06 | EVE SSO errors (e.g. `invalid_scope`) shall be shown with a hint instead of "invalid_state". | S | done (0.6.0) | test_sign_in_asks_for_structure_scopes_and_reports_sso_errors |
| STR-07 | The Structures link, page and API shall be members-only (ACC-01). | M | done (after 0.8.0) | test_notifications_and_structures_are_hidden_until_the_alliance_matches, test_sign_in_reads_the_alliance_so_both_members_tabs_show |
| STR-08 | The Structures page shall list every structure in which any registered character with a valid token holds an asset: the assets are read from ESI `/characters/{id}/assets`, then each structure from ESI `/universe/structures/{id}`; the ranking enabled switch and the main that linked the character do not matter. | M | done (since 0.6.0, confirmed after 0.8.0) | test_assets_reveal_structures_for_everyone, test_unreadable_by_one_character_is_read_with_another |
| STR-09 | While ESI is error limited (HTTP 420) the scout shall stop at 60 errors left in the window and resume on its next run, and a passing ESI failure (0, 420, 429, 5xx) shall not mark a structure as checked. | M | done | test_scout, test_sso_esi |
| WAR-16 | War statistics and lists shall show names, never bare IDs (names resolved during the scan and on demand, 3 s limit, stand-in "Corporation 98000001" until known), and link wars, corporations and alliances to zKillboard. | M | done (0.6.0) | test_names_are_resolved_on_demand…, test_names_appear_page_by_page…, test_stats_api_resolves_missing_names_first |
### Structure alerts (UC-11, added in 0.7.0)

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| ALR-01 | Where `alerts.enabled`, the system shall follow zKillboard's live feed R2Z2 (`sequence.json`, then `{n}.json`), start at the published position, keep its position across restarts (restart at the pointer when older than 20 h), skip a missing file when newer ones exist, wait `poll_s` (≥ 6 s) when caught up, read at most `max_per_poll` files per poll and send a User-Agent with version and `alerts.contact`. | M | done | test_feed_starts_at_the_pointer…, test_feed_skips_gaps…, test_feed_restarts_at_the_pointer… |
| ALR-02 | When R2Z2 answers 403 or 429, the system shall pause the feed for 10 minutes; network errors and odd files shall not stop it. | M | done | test_feed_limits_each_poll_and_backs_off_when_refused, test_poll_once_reads_the_feed_and_posts |
| ALR-03 | When a killmail's victim is an Upwell structure (inventory category 65), the system shall raise a **destroyed** alert, marked **core drop** for citadels, engineering complexes and refineries, matched to the known structure of that system, owner and type (nearest by position), and mark that structure destroyed. | M | done | test_destroyed_citadel_is_matched_and_drops_a_core |
| ALR-04 | When a ship dies within `grid_km` (default 1000 km) of a known structure with a known position, the system shall raise a **combat** alert for that structure with the distance. | M | done | test_combat_on_grid_uses_positions |
| ALR-05 | When an Upwell structure is on a killmail's attacker list, the system shall raise a **firing** alert (known structure matched, otherwise shown as "unknown <type>"). | M | done | test_structures_on_the_attacker_list_are_firing |
| ALR-06 | Known structures shall come from the public structure dataset and the structure scout (positions from ESI and EVE Ref); killmails without a body are read from ESI by ID and hash; type groups are cached in the database; each killmail raises each alert once. | M | done | test_killmail_body_is_fetched…, test_type_lookups_are_cached_in_the_database |
| ALR-07 | The Alerts page and `GET /api/alerts` shall group alerts by structure, newest first, with kind counts, owner and killmails linked to zKillboard, and the signed-in main's three closest characters by gates (last known location, online first on ties) with a Set destination button each; filters for kinds, core drops only, space and period, kept in the browser cookie. | M | done | test_alerts_page_and_api_in_demo, test_destination_requires_the_scope…, browser test |
| ALR-08 | The page shall refresh every 30 s and, when the user allows it, show a desktop notification and/or play a sound for alerts newer than the last one seen in this browser; a Discord link `#alert-<id>` opens the card of that alert. | S | done | test_alerts_page_filters_deep_links_and_offline_characters |
| ALR-09 | Where `alerts.discord.enabled`, the system shall post alerts of `alerts.discord.kinds` to the webhook with a link to the Alerts page, at most one combat/firing post per structure within `combat_cooldown_min`, pinging `mention_role_id` on destroyed only; a failed post stays queued, an invalid webhook is reported (redacted). | S | done | test_discord_posts_kinds_cooldown_and_mentions, test_discord_failures_keep_alerts_queued |
| ALR-10 | Alerts shall be kept `retention_days` (default 7); settings are validated at start-up (`poll_s` ≥ 6, positive grid and retention, known kinds, webhook present when Discord is on). | M | done | test_names_describe_and_purge, test_alert_settings_are_validated |
| ALR-11 | The alert card shall keep the three closest live characters with **Set destination** and show the closest static roster characters below them, with jumps only and no button; a roster character shall never replace a live one. | S | done (0.8.0) | test_closest_static_characters_for_an_alert, alerts.js |

### Static roster: uploaded character / system list (UC-12, added in 0.8.0)

A main uploads a JSON file of `char_name` + `star_system_name` pairs (schema `evedm-static-roster` v1, typically
50-700 entries). The entries are **static**: no SSO, no tokens, no ESI. They are resolved to systems once and ranked
by gate jumps against the same target as the live ranking, on the **Roster** tab; the **Roster** page manages them.

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| ROS-01 | The system shall provide an Import roster action that accepts a UTF-8 JSON file conforming to schema `evedm-static-roster` v1 and links every entry to the signed-in main. | M | done | test_roster_api_import_rank_and_manage |
| ROS-02 | If the file is not UTF-8 JSON or not schema-valid, then the system shall reject it whole and report each violation with its JSON path. | M | done | test_file_level_rejections |
| ROS-03 | The system shall accept both the enveloped form and a bare array of entries as v1. | M | done | test_a_bare_array_is_the_same_schema |
| ROS-04 | When an entry's name is new, the system shall add it; when the name is already in the pool, the system shall update its system, note and upload time, keep its enabled flag, and keep its tags unless the file carries tags for that entry. | M | done | test_import_merges_and_keeps_local_settings |
| ROS-05 | If an entry's name is not 3-37 letters, digits, spaces, hyphens or apostrophes, or repeats a name used earlier in the same file, then the system shall reject that entry alone and report it with its JSON path and reason. | M | done | test_bad_entries_are_rejected_one_by_one |
| ROS-06 | If `star_system_name` does not resolve to exactly one solar system, then the system shall store the entry unresolved with the raw name, the reason (not found / ambiguous) and up to three suggestions; a station or structure name that contains exactly one system name resolves to that system. | M | done | test_unknown_system_is_stored_unresolved_with_suggestions, test_several_systems_in_one_field_are_ambiguous, test_a_station_name_resolves_to_its_system |
| ROS-07 | The system shall report Added, Updated, Unresolved and Rejected counts and one line per entry that needs attention. | M | done | test_roster_api_import_rank_and_manage |
| ROS-08 | The system shall not keep the uploaded file and shall not log entry contents; only counts and reasons are logged. | M | done | roster.py (`import_file`), code review |
| ROS-09 | The system shall hold one pool per main, with no limit on its size, and shall show, rank, change and export only the pool of the signed-in main. | M | done | test_there_is_no_limit_on_file_size_or_entries, test_the_pool_is_per_main_and_goes_with_the_account |
| ROS-10 | A roster entry shall never be sent to ESI: no tokens, never refreshed, no ship, online or docked state, never marked Stale, and no share of the ESI rate-limit budget. | M | done | test_ranking_order_route_and_no_esi (a provider that fails the test if it is called) |
| ROS-11 | The system shall provide Re-resolve for unresolved entries and shall resolve them again after a universe rebuild. | S | done | test_re_resolve_picks_up_a_corrected_universe, test_roster_page_fixes_an_unresolved_entry |
| ROS-12 | The system shall let the user enable/disable an entry, edit its tags, set its system (by name or by clicking a suggestion), remove one entry and clear the pool behind a confirmation. | S | done | test_roster_api_import_rank_and_manage, test_roster_page_fixes_an_unresolved_entry |
| ROS-13 | When the account is deleted, the system shall delete the pool of that main. | M | done | test_the_pool_is_per_main_and_goes_with_the_account |
| ROS-14 | The system shall offer a download of the pool in schema `evedm-static-roster` v1; it carries no credentials, so no confirmation is required. | S | done | test_export_import_round_trip |
| ROS-15 | If an entry's name equals the name of a character linked to the same main, then the system shall mark it "also linked" and leave it out of the Alerts card, while keeping it on the Roster tab. | S | done | test_closest_static_characters_for_an_alert |
| ROS-16 | The Roster tab shall rank the enabled entries against the current target, reading every distance from the BFS already computed for that target; no extra BFS shall be run. | M | done | test_ranking_order_route_and_no_esi, test_ranking_can_be_filtered_by_tag |
| ROS-17 | Rows shall be sorted reachable first by jumps, then by name; entries with no gate route and unresolved entries shall be last, with their reason. | M | done | test_ranking_order_route_and_no_esi |
| ROS-18 | Each row shall show rank, name, system (security, region), jumps, tags, note and the Static badge, and no ship, online, docked or Set destination control. | M | done | test_ranking_order_route_and_no_esi, test_roster_tab_ranks_static_characters_and_draws_a_route |
| ROS-19 | The Roster tab shall take the target from the shared target input and from `?target=<name>`, and a new target shall rank both lists. | M | done | test_roster_tab_ranks_static_characters_and_draws_a_route |
| ROS-20 | When a roster row is selected, the map shall draw its route, mark origin and target and zoom to fit, with the roster entries' systems marked. | S | done | test_roster_tab_ranks_static_characters_and_draws_a_route (canvas pixels) |
| ROS-21 | The Roster page shall offer filters for state (unresolved / resolved), tag and ranked-only, and the chosen ranking tab and these filters shall be kept in the `evedm_prefs` cookie. | S | done | test_roster_page_fixes_an_unresolved_entry (filter survives a reload) |
| ROS-22 | Entry names, tags and notes shall be rendered as text, never as markup, on every page and in every report. | M | done | roster.js / rank.js (`textContent` only), code review |

### Members-only tabs: Notifications and Structures (D16, added after 0.8.0)

Both tabs are hidden unless the main that signed in belongs to the configured alliance (BB). The check uses the main
(the character that signed in), not the characters linked to it.

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| ACC-01 | The system shall show the Notifications and Structures links, pages and APIs only to a main whose alliance is in `access.alliance_ids` (default 99010320); for any other main the links are absent and the pages and APIs answer 404. | M | done (after 0.8.0) | test_notifications_and_structures_are_hidden_until_the_alliance_matches, test_sign_in_reads_the_alliance_so_both_members_tabs_show, test_the_page_and_api_are_hidden_from_other_alliances |
| ACC-02 | When a main signs in, the system shall read its corporation and alliance from ESI `/characters/{id}` (public) and store them on the account; it shall read them again when older than `access.affiliation_hours` (default 6) and never block a sign-in on an ESI failure. | M | done (after 0.8.0) | test_the_alliance_is_read_again_only_after_affiliation_hours, test_sign_in_reads_the_alliance_so_both_members_tabs_show |
| ACC-03 | If the alliance of a main is not known (not read yet, or ESI unreachable), then the system shall treat the main as a non-member. | M | done (after 0.8.0) | test_an_unknown_alliance_keeps_the_tabs_hidden |
| ACC-04 | Where `access.alliance_ids` is empty, or in demo mode, both tabs shall be open to every signed-in main. | S | done (after 0.8.0) | test_only_a_main_of_the_configured_alliance_passes |
| ACC-05 | The settings of 0.8.0 shall keep working: `notifications.alliance_ids` and `notifications.affiliation_hours` in YAML and `EVEDM_NOTIFICATIONS_ALLIANCES` are read as `access.*` when the new names are absent; `EVEDM_ACCESS_ALLIANCES` and an `access` section win. | S | done (after 0.8.0) | test_the_alliance_gate_lives_under_access_and_the_old_names_still_work |

### Notifications: asset safety and structures under attack (UC-13, added in 0.8.0)

The ESI notifications of **every registered character with a valid token** are read with that character's own token
and shown on one page, newest first, in the shape of the in-game notification: title, system with its region, EVE
time (D17). Who may open the page is decided by ACC-01. Asset-safety and structure types are
shown by default; every other type is one tick away in the filters.

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| NTF-01 | *Superseded by ACC-01*: the Notifications link, page and API are members-only, together with Structures. | M | done | see ACC-01 |
| NTF-02 | Where notifications are enabled, the system shall poll ESI `/characters/{id}/notifications` every `poll_s` seconds (default 60) for the characters whose cached copy has expired, at most `concurrency` at a time. | M | done | test_poll_stores_new_notifications_once |
| NTF-03 | The system shall ask for a character again only after `character_interval_s` (default 600, the ESI cache window) and shall store each notification once. | M | done | test_poll_stores_new_notifications_once |
| NTF-04 | The system shall poll every registered character that granted `esi-characters.read_notifications.v1` and whose refresh token is still valid (status is not Re-auth required), one link per character, whoever linked it and whatever its ranking enabled switch says. | M | done (site-wide after 0.8.0) | test_every_registered_character_with_a_valid_token_is_polled, test_only_characters_that_granted_the_scope_are_polled |
| NTF-05 | The system shall read the solar system, structure and structure type from the notification's YAML text (a starbase's `moonID` resolved through ESI); an unreadable text or a notification without a system shall still be stored. | M | done | test_body_is_parsed_and_a_broken_one_is_empty, test_a_notification_without_a_system_is_still_stored |
| NTF-06 | If one character's notifications cannot be read, then the system shall record the reason against that character and continue with the others. | M | done | test_a_broken_token_is_recorded_and_does_not_stop_the_round |
| NTF-07 | The page shall list each notification with its title, type, the character that received it, the system with security and region, EVE time and local time. | M | done | test_the_api_lists_notifications_with_region_and_closest_characters, test_notifications_page_shows_region_and_closest_characters |
| NTF-08 | Each notification shall show the three closest characters from the Characters set (jumps, system, online) and the three closest from the Roster set, from the same cached BFS the ranking uses. | M | done | test_the_api_lists_notifications_with_region_and_closest_characters, browser test |
| NTF-09 | By default only the configured groups shall be shown (asset safety, structure attacked, structure state and fuel, starbases); every other type shall be filtered out until it is ticked. | M | done | test_the_api_lists_notifications_with_region_and_closest_characters, test_notification_filters_open_other_types_and_survive_a_reload |
| NTF-10 | The filter list shall offer the grouped ESI notification types and, under "Other notifications", every type ESI has actually sent that is not in a group; the selection and the period are kept in the `evedm_prefs` cookie. | M | done | test_the_catalogue_groups_the_known_types_and_adds_what_was_seen, test_notification_filters_open_other_types_and_survive_a_reload |
| NTF-11 | The page shall refresh itself every `poll_s` seconds while visible and show how many characters are being read and which could not be. | S | done | notifications.js, test_the_api_lists_notifications_with_region_and_closest_characters |
| NTF-12 | Notifications shall be kept `retention_days` (default 30) and the page shall work on a 360 px screen. | S | done | test_old_notifications_are_purged, test_notifications_fit_a_phone |
| NTF-13 | The Notifications API and page shall list the notifications of all characters of NTF-04, not only those linked by the signed-in main; the status line shall show how many of the registered characters are being read and which could not be. | M | done (after 0.8.0) | test_the_api_lists_the_notifications_of_every_registered_character |
| NTF-14 | Each notification shall name the character that received it and, when that is not the main itself, the main it is linked to; a character whose token was revoked shall no longer be listed. | S | done (after 0.8.0) | test_the_api_lists_the_notifications_of_every_registered_character, notifications.js |

### Thera / Turnur through EVE-Scout (UC-15, D19, added after 0.8.0)

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| EVS-01 | Where `evescout.enabled` (default on), the Ranking page shall offer one radio button per configured hub next to *Gates only* (Thera, Turnur); the choice shall be kept in the `evedm_prefs` cookie. `GET /api/rank?hub=thera|turnur` selects the hub (case-insensitive, unknown = 422); `thera=1` keeps meaning any configured hub. Where disabled, the buttons, `GET /api/evescout` (404) and the hub routes shall be absent. | M | done | test_evescout_endpoint_and_disabled, test_evescout (`?hub=`), test_ui (radio buttons) |
| EVS-02 | The system shall read the list from `evescout.url` (default `https://api.eve-scout.com/v2/public/signatures`) on demand, at most every `refresh_s` (default **900 s**, 15 minutes), one request at a time, with the evedm User-Agent. When the answer lacks a configured hub, that hub shall be requested by name (`system_name=`) in the same refresh. | M | done | test_default_refresh_is_fifteen_minutes, test_missing_hub_is_fetched_by_name, test_ensure_fresh_ttl_and_keep_last_on_error |
| EVS-03 | The system shall keep the rows of the configured hubs (`evescout.hubs`, default Thera and Turnur), and drop J-space and unknown systems; connections marked End Of Life (0 hours left) are kept; one row per system, the longest-lived wins. | M | done | test_load_filters_hubs_wspace_keeps_end_of_life |
| EVS-04 | For each character the system shall find the shortest gate route to the closest listed system and, from the target, the closest listed system, and offer the route character → entry → hub → exit → target (wormhole = 2 jumps). The hub route shall replace the gate route only when strictly shorter; a hub with the same entry and exit shall be skipped; with a hub selected only that hub is used, with `thera=1` the shortest of the hubs wins. | M | done | test_via_uses_closest_listed_system_each_side, test_same_entry_and_exit_is_skipped, test_best_hub_is_the_shorter_one, test_thera_route_replaces_the_gate_route_only_when_shorter |
| EVS-05 | Among equally close listed systems the one with the higher security shall be chosen. | M | done | test_equal_distance_prefers_higher_security, test_load_orders_by_security_then_name |
| EVS-06 | The route shall avoid the systems on the avoid list as transit, like the gate route. | S | done | test_avoided_system_is_not_used_as_transit |
| EVS-07 | A ranking row shall carry `route_legs` (gate, wormhole, gate) and `wormhole` {hub, entry, exit, gates_to_entry, gates_from_exit, direct_jumps, used}; the response shall carry the EVE-Scout status (fetch time, error, connections per hub). | M | done | test_rank_with_thera_adds_wormhole_route, test_closest_listed_system_and_security_tie_break_follow_the_list |
| EVS-08 | When EVE-Scout cannot be read, the last list shall be kept, the error shown under the filter, and the server shall not be asked again before `refresh_s` has passed. | M | done | test_ensure_fresh_ttl_and_keep_last_on_error, test_list_is_kept_when_eve_scout_goes_down_and_off_switch_hides_the_filter |
| EVS-09 | The star map shall draw the route as two gold gate legs joined by a dashed purple wormhole leg labelled with the hub; the route bar shall name the hub, the entry and exit systems and the gate route it replaces; a "via Hub" badge shall mark the row. | M | done | test_thera_filter_draws_a_two_leg_route_and_is_remembered |
| EVS-10 | In demo mode the list shall be a built-in sample (no network). | S | done | test_rank_with_thera_adds_wormhole_route, test_evescout_endpoint_and_disabled |

### Browser preferences

| ID | Requirement | P | Status | Verified by |
| --- | --- | --- | --- | --- |
| PRF-01 | The browser shall remember the Wars tab and filters, the Structures and Alerts filters, the Alerts notification and sound choice, the map options (names, other characters, stargate links), the ranking auto-refresh interval, the ranking tab (Characters or Roster), the Roster page filters and the Notifications types and period in one first-party cookie (`evedm_prefs`, one year, SameSite=Lax); nothing is stored on the server; an unreadable cookie means defaults. | S | done (0.5.0) | test_wars_tabs_and_filters_survive_a_reload, test_map_options_and_auto_refresh_are_remembered, test_broken_prefs_cookie… |

## Non-functional requirements

| ID | Requirement | Result (0.3.0) |
| --- | --- | --- |
| NFR-01 | Tokens encrypted at rest; secrets from env; no tokens in logs; HTTPS on a private network. | Fernet at rest; log redaction filter; integration test scans logs |
| NFR-02 | BFS ≤ 50 ms; ranking of 50 characters ≤ 3 s cold; UI never blocks. | BFS 2 ms; cold ranking 0.48 s (50 characters, 40 ms ESI latency) |
| NFR-03 | ESI: User-Agent, X-Compatibility-Date, rate-limit headers, Retry-After on 429. On HTTP 420 (error limit) the client reads `X-Esi-Error-Limit-Remain/Reset`, stops sending requests until the window resets and fails fast; ranking rows keep their last data with an `esi:420` badge; callers stop at 5 errors left, the structure scout at 60. | implemented, tested (test_sso_esi) |
| NFR-04 | Works without access to CCP static data at runtime. | universe file bundled in the package |
| NFR-08 | Runs within 2 vCPU / 1 GB RAM. | container peak RSS 77.5 MB (cgroup 55 MiB), p95 83 ms at 422 req/s (WAL) |
| NFR-09 | Static roster: importing 700 entries resolves them without any network call; ranking them adds little to a ranking that already has its BFS. | import 700 entries (46 KB) 54 ms; ranking 700 entries 16 ms; closest-for-an-alert over 700 entries 5.6 ms; 700 rows ≈ 44 KB in SQLite |
| NFR-10 | Notifications: the minute loop costs one ESI call per character per 600 s, and nothing while every character is cached. | 7 demo characters: at most 7 calls per 10 minutes; a round with nothing due makes no call |
| NFR-06 | Core unit tests for BFS, resolver, bundle, sort order, token refresh, accounts, war watcher, structure scout, structure alerts, static roster, notifications, Thera / Turnur routes. | after 0.8.0: 287 unit, 29 integration (10 process + 19 browser), 1 footprint; scenario matrix in 03-Testing |

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
| AC-15 | A citadel dies in a feed killmail → Alerts page shows it with core drop and the closest character → Set destination reaches ESI for an online character, offline gives a clear error | test_alerts_e2e::test_feed_alerts_closest_characters_and_set_destination |
| AC-16 | A 700-entry file imports and is ranked against Jita | test_roster.py (import + rank), NFR-09 measurement |
| AC-17 | Re-uploading a file where a character moved updates that entry and keeps its tags and Ranked setting | test_import_merges_and_keeps_local_settings |
| AC-18 | An unknown system is stored unresolved with suggestions and is fixed by clicking one | test_unknown_system_is_stored_unresolved_with_suggestions, test_roster_page_fixes_an_unresolved_entry |
| AC-19 | A file that is not JSON or not the schema is rejected whole; a large file (5000 entries, over 500 KB) is accepted | test_file_level_rejections, test_there_is_no_limit_on_file_size_or_entries |
| AC-20 | One malformed entry in an otherwise good file is rejected alone | test_bad_entries_are_rejected_one_by_one |
| AC-21 | Ranking a roster makes no ESI request and runs no extra BFS | test_ranking_order_route_and_no_esi |
| AC-22 | Another main sees an empty roster; deleting the account clears the pool | test_the_pool_is_per_main_and_goes_with_the_account |
| AC-23 | An alert card shows the live characters with Set destination and the roster characters without it | test_closest_static_characters_for_an_alert, alerts.js |
| AC-24 | A roster name equal to a linked character's name is badged and absent from the alert card | test_closest_static_characters_for_an_alert |
| AC-25 | Export → clear → import round trip restores the pool | test_export_import_round_trip |
| AC-26 | An asset-safety notification appears on the Notifications page with its system, region and the closest characters | test_the_api_lists_notifications_with_region_and_closest_characters, test_notifications_page_shows_region_and_closest_characters |
| AC-27 | A main of another alliance sees neither the Notifications nor the Structures link and gets 404 from both pages and both APIs | test_notifications_and_structures_are_hidden_until_the_alliance_matches, test_the_page_and_api_are_hidden_from_other_alliances |
| AC-28 | A type outside the default groups is hidden until it is ticked, and the choice survives a reload | test_notification_filters_open_other_types_and_survive_a_reload |
| AC-29 | A character whose token is refused is reported and the other characters are still read | test_a_broken_token_is_recorded_and_does_not_stop_the_round |
| AC-30 | A second poll inside the ESI cache window reads nothing | test_poll_stores_new_notifications_once |
| AC-31 | A member sees the notifications of characters linked by other mains, with the main named | test_the_api_lists_the_notifications_of_every_registered_character |
| AC-32 | A character with a revoked token is not listed and not polled; one switched off in the ranking still is | test_every_registered_character_with_a_valid_token_is_polled, test_the_api_lists_the_notifications_of_every_registered_character |
| AC-33 | A structure appears on the Structures page as soon as any registered character has an asset there | test_assets_reveal_structures_for_everyone |
| AC-34 | A main who leaves the alliance loses both tabs once its alliance is read again | test_only_a_main_of_the_configured_alliance_passes |
| AC-35 | With Thera or Turnur selected, a character whose route through a Thera / Turnur connection is shorter shows the hub route (gate, wormhole, gate) and the "via" badge; with Gates only, or when the hub route is not shorter, the gate route stays | test_thera_route_replaces_the_gate_route_only_when_shorter, test_thera_filter_draws_a_two_leg_route_and_is_remembered |
| AC-36 | The listed system closest to the character and the one closest to the target are used, higher security first on a tie | test_closest_listed_system_and_security_tie_break_follow_the_list, test_equal_distance_prefers_higher_security |
| AC-37 | When EVE-Scout is down the last list is still used and the error is shown | test_list_is_kept_when_eve_scout_goes_down_and_off_switch_hides_the_filter |
