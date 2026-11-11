# 03 · Testing

| Suite | Path | What runs | Count |
| --- | --- | --- | --- |
| Unit | `tests/unit` | In-process: universe/BFS, resolver, config, crypto, DB, SSO + ESI against the fake (ASGI transport), bundle, ranking, accounts (signed session cookies), war watcher, war census and high-sec structures, structure scout, structure alerts (R2Z2 feed, classification, Discord, Alerts API), Set destination, web API (TestClient), CLI, static roster (import, merge, resolution, ranking without ESI, export), Thera / Turnur routes (EVE-Scout list, closest listed systems, tie-break, two-leg plan, TTL, ranking API), notifications (polling with the ESI cache for every valid character, parsing, filters), the alliance gate for the Notifications and Structures tabs (cache, legacy settings, hidden pages and APIs), dependency validation | 413 |
| Integration | `tests/integration/test_e2e.py`, `test_multi_user.py`, `test_alerts_e2e.py`, `test_evescout_e2e.py` | Real `evedm serve` process + real fake EVE SSO/ESI process: sign-in, add characters, import, rank, ESI caching, export, revocation, second instance, restart, secrets-not-in-logs; several mains on one site; upgrade of a 0.3.0 database; structures shared through the scout; killmail feed → alerts → closest character → Set destination; EVE-Scout list → Thera / Turnur routes | 10 |
| Browser | `tests/integration/test_ui.py`, `test_ui_accounts.py` | Chromium (Playwright): list + map + route bar, autocomplete, paste, characters page, phone layout, Wars page (filter, rank a structure system, phone, statistics and structures tabs), browser-cookie settings, Structures page, Alerts page (filters, deep link, offline characters), Set destination in the route bar, account UI (main badge, remove, delete account), Roster tab and Roster page, Notifications page (cards, filters, phone), Thera / Turnur filter | 22 |
| Footprint | `tests/perf` | 50 characters, 600 requests at concurrency 20, 40 ms ESI latency, server pinned to 2 CPUs; budget peak RSS ≤ 160 MB, p95 ≤ 1 s | 1 |
| Dependencies | `tests/unit/test_dependencies.py` | every import pinned in `requirements.txt`, exact stable pins, installed = pinned, absolute verified paths, WAL + checkpoint | 8 |
| Container | `scripts/docker-smoke.sh` | Image with `--cpus 2 --memory 1g`, host folder mounted at `/app/data`, health, ranking, wars, structures, alerts, WAL, `evedm check`, uid 10001, restart keeps data | script |

## Scenario matrix (accounts, war watcher, structures, alerts)

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
| Roster | upload 50-700 name + system pairs | merged into the main's pool, report per entry | test_roster_api_import_rank_and_manage |
| Roster | upload again after characters moved | systems updated, tags and Ranked setting kept | test_import_merges_and_keeps_local_settings |
| Roster | bad name, duplicate name in one file | those entries rejected, the rest imported | test_bad_entries_are_rejected_one_by_one |
| Roster | not JSON, unknown property; 5000 entries over 500 KB | whole file rejected with JSON paths / large file accepted | test_file_level_rejections, test_there_is_no_limit_on_file_size_or_entries |
| Roster | unknown or ambiguous system | stored unresolved with up to three suggestions, ranked last | test_unknown_system_is_stored_unresolved_with_suggestions, test_several_systems_in_one_field_are_ambiguous |
| Roster | station name as the system | resolved to the system it names | test_a_station_name_resolves_to_its_system |
| Roster | rank against a target | reachable first by jumps, no ESI call, no extra BFS | test_ranking_order_route_and_no_esi |
| Roster | pool per main, account deleted | invisible to other mains, deleted with the account | test_the_pool_is_per_main_and_goes_with_the_account |
| Roster | name equal to a linked character | badged "also linked", left out of the alert card | test_closest_static_characters_for_an_alert |
| Roster | export, clear, import | pool restored unchanged | test_export_import_round_trip |
| Roster | Roster tab in the browser | ranked list, Static badge, route on the map, no Set destination | test_roster_tab_ranks_static_characters_and_draws_a_route |
| Roster | Roster page in the browser | filter survives a reload, a suggestion fixes an unresolved entry | test_roster_page_fixes_an_unresolved_entry |
| Notifications | a character's ESI notifications | stored once, parsed for system and structure | test_poll_stores_new_notifications_once |
| Notifications | second poll inside the 600 s cache window | nothing read, nothing stored | test_poll_stores_new_notifications_once |
| Notifications | character without the notification scope | never polled | test_only_characters_that_granted_the_scope_are_polled |
| Notifications | revoked token on one character | reason recorded, the others still read | test_a_broken_token_is_recorded_and_does_not_stop_the_round |
| Notifications | unreadable YAML, no system in the text | stored anyway, shown without distances | test_body_is_parsed_and_a_broken_one_is_empty, test_a_notification_without_a_system_is_still_stored |
| Members-only tabs | main of another alliance, or alliance not known yet | no Notifications or Structures link, 404 from both pages and both APIs | test_notifications_and_structures_are_hidden_until_the_alliance_matches, test_sign_in_reads_the_alliance_so_both_members_tabs_show |
| Members-only tabs | alliance read at sign-in, cached for affiliation_hours, read again by force | one ESI call, then cache, then a new answer | test_the_alliance_is_read_again_only_after_affiliation_hours |
| Members-only tabs | the main leaves the alliance / no alliance configured | tabs disappear / open to everyone | test_only_a_main_of_the_configured_alliance_passes |
| Members-only tabs | 0.8.0 setting names | still read as `access.*`; the new names win | test_the_alliance_gate_lives_under_access_and_the_old_names_still_work |
| Notifications | characters of several mains, one switched off in the ranking, one with a revoked token | all read except the revoked one | test_every_registered_character_with_a_valid_token_is_polled |
| Notifications | page API for a member | notifications of other mains' characters listed with the main named, revoked character left out | test_the_api_lists_the_notifications_of_every_registered_character |
| Notifications | page in the browser | title, system, region, EVE time, closest characters and roster line | test_notifications_page_shows_region_and_closest_characters |
| Notifications | filters | non-default type hidden until ticked, choice kept in the cookie | test_notification_filters_open_other_types_and_survive_a_reload |
| Notifications | hub label and route type | closer of Thera / Turnur per notification, route type per closest character | test_notifications (hub label and route type) |
| Notifications | character with no structure with assets (`min_structures` 1; default was 10), or the tick off | not polled, counted as excluded by the gate, one INFO line when the count changes | test_characters_with_few_structures_are_not_read, test_the_default_reads_every_character_with_a_structure, test_the_gate_count_and_the_log_line_follow_the_eligible_set, test_the_notifications_tick_decides_whether_a_character_is_read |
| Notifications | next check and errors | ESI Expires + 0.25 s, a short lifetime followed, 30 s only without a usable Expires; transient error after 60 s, 420 when the block ends; loop sleeps exactly until due; saved schedule kept after a restart | test_the_next_read_follows_the_expiry_of_esis_cache, test_a_short_remaining_lifetime_is_followed_but_not_forever, test_the_loop_wakes_when_the_next_character_is_due_not_a_second_later, test_the_error_limit_is_retried_when_it_ends, test_a_restart_keeps_the_saved_schedule, test_a_passing_esi_error_is_retried_soon_but_a_refusal_waits |
| Notifications | what ESI says about freshness: 200 and 304 metadata, no Expires, Expires in the past, Age, a server clock minutes off | `Date` / `Last-Modified` / `Expires` read for both; a prompt re-read (30 s), never a window; lifetime measured on ESI's clock | test_the_answer_metadata_is_read_for_200_and_304, test_a_missing_or_past_expires_means_a_prompt_reread, test_an_unusable_expires_is_retried_in_min_interval_not_a_window, test_a_server_clock_off_by_minutes_still_reads_at_esis_expiry, test_the_cache_lifetime_is_measured_on_esis_clock |
| Notifications | read log: 200, 304, failed read (redacted), pruning, v17 database | one `notification_reads` row per read, kept 7 days, table added in place | test_every_read_is_logged_with_what_esi_said, test_the_error_of_a_failed_read_is_redacted, test_old_read_rows_are_pruned_with_the_hourly_purge, test_the_purge_in_the_poll_loop_prunes_the_reads, test_an_old_database_gets_the_read_log_in_place |
| Notifications | `evedm notifications lag`: percentiles, histogram, on time / late split, gate counts, `reading`, no table yet | JSON from the database alone, nothing migrated or written | test_the_lag_report_has_percentiles_a_histogram_and_the_split, test_the_lag_report_names_the_biggest_component, test_the_lag_report_lists_the_characters_not_read_in_the_last_hour, test_the_lag_command_prints_json_and_leaves_the_database_alone |
| Notifications | stagger: never more than `stagger_s` past the expiry, settles; short re-arm | reads a window apart again, no drift; a short re-arm exact | test_the_stagger_never_pushes_a_read_past_stagger_s_and_settles, test_a_short_rearm_is_exact_and_not_staggered |
| Notifications on Discord | one slow character in the round; Discord down while several characters are read | the others are posted at once; one attempt, retried after `retry_s`, nothing lost | test_discord_gets_a_character_as_soon_as_it_is_read_not_after_the_round, test_a_failed_post_is_not_hammered_by_every_read_and_is_retried_when_due |
| Event loop | war census step at production size (20 000 wars, 10 000 active, 19 000 structures); cached `corps_at_war`; activity names in the warning | largest overshoot of the loop under 1 s (measured 0.15-0.2 s; 5.9 s with the per-ID `_known()` call); cache until the census changes or an active war ends; "during wars.cycle" | test_a_census_step_at_production_size_leaves_the_event_loop_free, test_the_census_caches_are_warm_after_a_step, test_corps_at_war_is_cached_until_the_census_changes_or_a_war_ends, test_activities_nest_and_always_close, test_the_warning_names_the_activity_it_happened_in, test_every_service_marks_its_work |
| Notifications | lag headline: backlog, unmatched, history; fewer than 20 samples; reads with data age 300 s or more | only matched notifications within 1200 s in the percentiles; "too few samples: N"; share and pattern named | test_the_headline_counts_only_matched_notifications_within_two_windows, test_a_conclusion_from_reads_still_carries_the_sample_warning, test_reads_with_old_data_are_counted_and_a_recurring_pattern_is_named |
| Event loop | monitor with injected clock; a real blocked loop; health JSON; dataset parsing | max / p99 over 10 minutes, one WARNING per minute above 2 s; the parse runs in a worker thread | test_the_loop_monitor_keeps_max_and_p99_and_warns_once_a_minute, test_the_loop_monitor_forgets_what_is_older_than_ten_minutes, test_the_loop_monitor_measures_a_blocked_loop, test_health_shows_the_loop_lag_and_the_notification_lag, test_the_dataset_parsing_runs_off_the_event_loop |
| Alerts | kill without battleship/capital and under 500M ISK | no combat/firing alert; destroyed always kept; switches off the filter | test_alerts (big kills) |
| Alerts | capitals filter | only kills with a capital on both sides | test_alerts (capitals both sides) |
| Ansiblex | per-character gates, BFS with one-way links | route uses only that character's gates, 1 jump each | test_ansiblex (11 tests) |
| Notifications | ESI repeats the whole list; an unexpected failure for one character; host clock off against ESI | only new entries parsed and stored; that character backs off 60 s, the others are read; cache lifetime taken from ESI's Date | test_a_reread_parses_only_notifications_that_are_new, test_an_odd_answer_backs_off_instead_of_being_read_every_round, test_the_cache_lifetime_is_measured_on_esis_clock |
| Notifications | 30 cards of one system | distances worked out once | test_many_notifications_of_one_system_are_measured_once |
| Notifications | page waiting; reads clustered in ESI's window | woken at once by a new notification; reads drift evenly apart | test_a_waiting_page_is_woken_as_soon_as_something_new_is_stored, test_the_wait_endpoint_holds_until_the_timeout, test_reads_are_spread_over_esis_cache_window |
| Notifications on Discord | new event; second recipient; message deleted; history, other types; Discord down; role; no own webhook; database from before | posted once; message edited; posted anew; never posted; retried; pinged for attacks only; wars webhook used; old rows not posted | test_notifications.py (Discord tests) |
| Structures | Resolve unknown with the main's token; main without the scope or signed out | 403 kept, 404 gone, public list first; refused with a reason | test_main_resolves_unresolved_structures_one_by_one, test_resolver_needs_the_main_with_the_structure_scope, test_targets_go_public_first_then_never_asked_sighters_then_the_rest |
| Structures | sighter, alt of the main, other main, main last; a refused pair; nobody left to ask; a token ESI rejects | the sighter answers where the main would be refused; a refused pair is asked again only after `no_access_refresh_hours`; no request and status unchanged when nobody is left (counted skipped); the rejected token is skipped for the rest of the job | test_a_sighter_is_asked_before_the_main_and_the_main_comes_last, test_a_refused_pair_is_not_asked_again_until_the_window_is_over, test_a_structure_nobody_can_be_asked_about_is_not_sent_to_esi, test_a_token_that_is_not_accepted_is_skipped_for_the_rest_of_the_job |
| Structures | Resolve unknown as a job: ESI error window low, also between two characters of one structure; cancel; app shutdown; three ESI failures in a row; the hourly step meanwhile | waits and resumes without sending while low (time is injected); cancelled job sends nothing more; shutdown cancels and awaits it; nothing marked checked after failures; the hourly step leaves the reads to the job | test_the_job_waits_for_the_error_window_and_goes_on, test_a_window_that_closes_between_two_characters_of_one_structure, test_cancel_ends_the_job_and_nothing_more_is_sent, test_start_runs_in_the_background_refuses_a_second_job_and_can_be_stopped, test_three_failures_in_a_row_stop_the_job_and_mark_nothing, test_the_hourly_step_leaves_the_reads_to_a_running_job |
| Structures | refusal memory: hourly step, unlinked characters, old database; stats | refusals recorded and not repeated by the hourly step, pruned with unlinked characters (a main without assets keeps them); `structure_refusals` added to a v13 database in place; counts of `GET /api/structures/stats` and `evedm structures stats` (read-only, no migration) | test_the_hourly_step_records_refusals_and_does_not_repeat_them, test_refusals_of_unlinked_characters_are_pruned_but_a_main_without_assets_keeps_them, test_an_old_database_gets_the_refusal_table_in_place, test_stats_count_what_the_resolver_has_to_work_with, test_the_stats_command_reads_the_database_and_changes_nothing |
| Structures | the three job endpoints and stats | POST 202 / 409 busy, GET state, stop; 403 without the CSRF header, 404 outside the alliance, 401 signed out, 422 without a character | test_resolve_job_endpoints, test_resolve_endpoint_in_demo_is_refused |
| Structures | historical details: EVE Ref, local index, Adam4EVE; missing or broken index | ranked EVE Ref > local index > Adam4EVE; nothing changes without the file | test_resolver_also_matches_the_public_datasets, test_the_local_index_fills_unresolved_structures_and_ranks_between_the_datasets, test_a_missing_or_broken_local_index_changes_nothing |
| Structure index builder | timestamps, merge, folder listing, resumable and conditional downloads, empty build, reader, a dropped-in latest file | newest observation wins field by field; finished files never fetched again | test_structure_index.py (8 tests) |
| Notifications | Sound ticked; an item already seen or outside the filters | one chime for a new matching item only | test_notification_sound_only_chimes_for_new_matching_items |
| Phone layout | ranking on a phone; moving between pages | map first, controls under the gear, route bar above the list; page links keep their scroll position | test_phone_map_above_inline_settings_and_ranked_characters, test_navigation_position_across_pages |
| Notifications | retention | notifications older than retention_days are purged | test_old_notifications_are_purged |
| Thera / Turnur | listed systems closest to character and target | route gate, wormhole, gate; used only when strictly shorter; same entry and exit skipped | test_via_uses_closest_listed_system_each_side, test_same_entry_and_exit_is_skipped |
| Thera / Turnur | equal distance | higher security wins | test_equal_distance_prefers_higher_security |
| Thera / Turnur | list with J-space, unknown, End Of Life, duplicate rows | usable k-space systems kept, End Of Life kept, J-space and unknown dropped | test_load_filters_hubs_wspace_keeps_end_of_life |
| Thera / Turnur | EVE-Scout down | last list kept, error reported, no hammering | test_ensure_fresh_ttl_and_keep_last_on_error, test_list_is_kept_when_eve_scout_goes_down_and_off_switch_hides_the_filter |
| Thera / Turnur | one radio button per hub, `?hub=` | only that hub is the mid-system; unknown hub = 422 | test_evescout (hubs restriction, `?hub=`) |
| Thera / Turnur | default refresh, custom URL without a hub | 900 s cache; the missing hub is fetched by name | test_default_refresh_is_fifteen_minutes, test_missing_hub_is_fetched_by_name |
| ESI limits | HTTP 420, scout reserve | requests paused until the window resets; scout stops at 60 errors left | test_sso_esi, test_scout |
| Thera / Turnur | real process + fake EVE-Scout | thera=0 vs thera=1, legs, Perimeter to Rens through Turnur | test_thera_route_replaces_the_gate_route_only_when_shorter, test_closest_listed_system_and_security_tie_break_follow_the_list |
| Thera / Turnur | radio buttons in the browser | badge, route bar note, wormhole hop, legend, cookie | test_thera_filter_draws_a_two_leg_route_and_is_remembered |
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
| Names (0.6.0) | ESI down during the scan; first scan in progress; slow ESI on a page view | stand-in "Corporation 98000001", names page by page, page answers within 3 s then a minute's cooldown | test_names_are_resolved_on_demand…, test_names_appear_page_by_page…, test_on_demand_names_give_up_fast… |
| zKillboard links | wars, corporations, alliances | links present | test_statistics, test_stats_api_resolves_missing_names_first |
| Structure scout (0.6.0) | assets in structures, items in own containers, NPC stations, unknown IDs, structures without access | structures only; 404 = not a structure; no access keeps data | test_assets_reveal_structures_for_everyone |
| Structure scout (rework) | assets read page by page, known structures, bogus container IDs, truncated list, passing ESI errors | structures saved per page, known skipped and kept, only type `other`, truncation not an error, no `checked_at` on passing errors | test_scout |
| Structure scout | one character may not read a structure another may | read with the other token | test_unreadable_by_one_character_is_read_with_another |
| Structure scout | refresh intervals, character unlinked, re-auth or demo characters, truncated assets, failed read | a truncated or failed read saves nothing and keeps the old sightings | test_refresh_intervals…, test_reauth_and_demo…, test_paged_assets_and_truncation, test_a_failed_read_saves_nothing_and_keeps_the_old_sightings |
| Structure scout | ingestion rule: flags Hangar, Deliveries, AssetSafety typed "item" or "other"; fittings and cargo; a Hangar location that is an own ship or container | only the first group is stored; the Characters count is the real number | test_only_hangar_deliveries_and_asset_safety_of_not_own_locations_are_structures, test_item_typed_locations_are_never_structures, test_the_scan_keeps_ids_not_rows |
| Structure scout | purge: own items with sightings and refusals, an ok row that is an own item, orphans, a public history match, no contributor, a running resolve job; filter_v forces a re-read; v14 database | own items removed, ok kept with a warning, orphans removed only when every contributor is current and never ok, gone or history-matched rows, nothing deleted while the job holds the lock; the column is added in place | test_own_items_stored_as_structures_are_purged_with_their_sightings_and_refusals, test_a_character_read_with_an_older_filter_is_read_again_and_orphans_wait_for_everybody, test_a_structure_stays_in_the_file_until_it_is_gone, test_no_orphans_are_purged_without_a_contributor, test_the_purge_waits_while_a_resolve_job_holds_the_lock, test_an_old_database_gets_the_asset_filter_column_in_place |
| Dock search | prefixes of system names (case-insensitive, each system once, 3 or 4 characters), punctuated prefixes by full name, term order (home region, structure regions, k-space by region, J-space last) | one term per distinct prefix remembering its systems; every system covered once | test_prefixes_are_distinct_and_case_insensitive_and_remember_their_systems, test_the_search_terms_are_prefixes_or_full_names_for_punctuated_ones, test_terms_go_home_region_first_then_the_structure_regions_then_k_space_then_j_space, test_the_punctuated_setting_searches_the_full_system_name |
| Dock search | rows with their term and new known rows, IDs below 1e12, both scopes, switch off; first pass at every run, then the interval; cursor; pass counter | access rows only for characters with both scopes; the first pass is not held back by the interval | test_the_dock_search_stores_access_rows_with_their_term_and_creates_known_rows, test_the_dock_search_needs_both_scopes_and_can_be_switched_off, test_the_first_pass_goes_on_at_every_run_and_then_follows_the_interval |
| Dock search | a term at the cap split into full names (counted against the budget, resumes when it fits, one still capped is kept and counted); a term below the cap drops lost access; a capped term never drops | lost access found by prefix is removed, rows without a term only age out | test_a_term_at_the_cap_is_split_into_full_system_names, test_a_split_counts_against_the_budget_and_waits_for_a_run_it_fits_into, test_a_term_that_answers_below_the_cap_drops_what_it_no_longer_returns, test_a_capped_term_only_adds_and_keeps_what_it_did_not_return |
| Dock search | passing errors (420, 429, 5xx, network), 403, rejected token, error window low or closing mid-run | passing keeps the character due, 403 waits the whole interval, nothing sent while the window is low | test_a_passing_error_keeps_the_run_due_and_a_refusal_waits_the_whole_interval, test_the_dock_search_respects_the_error_window_and_pauses_in_the_middle |
| Dock search | candidate order, orphan purge, pruning (30 days or twice the pass), listing `source`, API parameter, counts only, stats, settings, removed `dock_search_term`, search without cache, v15 and v16 databases | dock characters first (no 403), access rows protect, default view unchanged, no character names, old config still loads | test_characters_that_may_dock_are_asked_first_and_resolve_without_a_403, test_the_orphan_purge_spares_structures_somebody_may_dock_at, test_dock_search_rows_of_unregistered_characters_and_old_rows_are_pruned, test_the_listing_source_filter_defaults_to_assets_and_shows_counts_only, test_the_structures_api_takes_the_source_filter, test_stats_have_a_dock_search_block, test_the_dock_search_settings_are_checked, test_an_old_config_with_the_removed_term_still_loads_and_says_so, test_search_structure_ids_is_the_one_request_and_can_skip_the_cache, test_an_old_database_gets_the_dock_search_tables_in_place, test_an_old_database_gets_the_search_term_and_pass_columns_in_place |
| Dock search | `search-probe` terms and `--self-test`: name shapes, spread of shapes, hits per term kind, recommendation, Ansiblex section, nothing stored | known structures found by their prefix; recommendation computed from evidence only (full name hits, prefix misses, not at the cap) | test_name_shapes_decide_how_a_prefix_is_tested, test_the_self_test_recommendation_is_computed_from_the_table, test_search_probe_self_test_finds_known_structures_by_system_name, test_search_probe_command_runs_the_terms_and_writes_nothing |
| Structures probe | candidates per bucket and by database status | the probe applies the scout's rule | test_probe_counts_the_candidates_the_scout_would_take |
| Structures page | every space, wormholes unreachable last, search, filters, sort, paging, at-war owners, shared between mains, disabled | as stated | test_structures_page_and_api_in_demo, test_structures_off, test_structures_found_through_assets_are_shared, browser test |
| Scopes | sign-in asks for the structure scopes; EVE SSO `invalid_scope`; `EVE_OPTIONAL_SCOPES=none`; bad values in the main config | hint shown; serve exits 2 with a message | test_sign_in_asks…, test_optional_scopes_setting, test_bad_values_in_the_main_config_are_reported |
| Browser settings | tab, filters, map options, auto-refresh survive a reload; other browser has defaults; broken cookie | remembered / defaults / defaults without errors | test_wars_tabs_and_filters_survive_a_reload, test_map_options_and_auto_refresh_are_remembered, test_broken_prefs_cookie… |
| Alert feed (0.7.0) | first start, resume, new process, trailing slash in the URL | starts at the pointer, continues from the stored position, User-Agent sent | test_feed_starts_at_the_pointer_resumes_and_sends_a_user_agent |
| Alert feed | missing file below the pointer; caught up; more files than `max_per_poll` | skipped / stops / rest next poll | test_feed_skips_gaps…, test_feed_limits_each_poll… |
| Alert feed | 429 or 403 on a file or on the pointer; 500; network down; position older than 20 h | 10 min pause without requests, file read afterwards / error counted / restart at the pointer | test_feed_limits_each_poll…, test_feed_restarts_at_the_pointer… |
| Classification | citadel, refinery, engineering complex, jump gate destroyed; two citadels of one owner in a system; unknown structure | core drop for the first three; nearest by position; "unknown Astrahus" | test_destroyed_citadel_is_matched_and_drops_a_core |
| Classification | kill at 50 km / 1500 km / without position / near a structure without position; smaller `grid_km` | combat / nothing / nothing / nothing / nothing | test_combat_on_grid_uses_positions |
| Classification | structures among attackers (known, unknown owner); structure kills structure | firing (matched / unknown); destroyed + firing | test_structures_on_the_attacker_list_are_firing |
| Classification | package without killmail body; unknown killmail; unknown system; empty package; same killmail twice | read from ESI by id/hash / ignored / ignored / ignored / one alert | test_killmail_body_is_fetched… |
| Classification | type lookups with ESI down | cached in `type_groups`; unknown types are not structures for now; combat still found | test_type_lookups_are_cached_in_the_database |
| Discord alerts | kinds filter, combat cooldown per structure, role ping on destroyed only, link to the Alerts page | posted / skipped (`posted=2`) / as stated | test_discord_posts_kinds_cooldown_and_mentions |
| Discord alerts | timeout, invalid webhook, Discord off | stays queued, error shown redacted, nothing sent | test_discord_failures_keep_alerts_queued |
| Discord alerts | combat title without a distance | no crash (*fix*: every title was formatted eagerly) | test_discord_posts_kinds_cooldown_and_mentions |
| Alerts page | groups, newest first, counts, core drop, closest characters sorted, kinds/core/space/period filters, health, disabled | as stated / 404 | test_alerts_page_and_api_in_demo, test_alerts_off |
| Alerts page | filters after a reload; no kind chosen; Discord link to an older alert of a structure; offline character | remembered / empty / that card highlighted / button disabled with reason | test_alerts_page_filters_deep_links_and_offline_characters |
| Set destination | demo; system or structure ID; bad ID, broken JSON, missing CSRF header; unknown or foreign character | simulated / accepted / 400 / 403 / 404 | test_set_destination_in_demo_and_bad_input, test_destination_requires_the_scope… |
| Set destination | character without `esi-ui.write_waypoint.v1`; ESI refuses (offline); token revoked | 400 "re-authorize" / 502 "is the character online?" / 409 | test_destination_requires_the_scope… |
| Set destination | real call against the fake ESI: online, offline, scope missing | 204 recorded / 404 / 403 | test_esi_post_auth_sets_the_waypoint_in_the_fake, test_alerts_e2e |
| Alert settings | poll under 6 s, zero grid, bad `max_per_poll`, unknown kind, Discord without webhook; YAML + env; contact in the User-Agent | refused at start-up / applied / sent | test_alert_settings_are_validated, test_alert_settings_from_yaml_and_environment, test_user_agent_names_the_contact |
| Database | `execute` count for UPDATE/DELETE after an INSERT on the same connection | rows changed (*fix*: returned the last insert's row ID, e.g. purge counts) | test_execute_returns_row_ids_for_inserts_and_counts_otherwise |

## Fake EVE server

`tests/fake_eve.py` implements the EVE SSO endpoints (authorize, token, revoke, JWKS, metadata) with **real RS256 JWTs**
and the ESI endpoints the app uses (location, ship, online, universe/names, assets, universe/structures, universe
types and groups, killmails, `POST /ui/autopilot/waypoint`) with `Expires`, `ETag`/304 and rate-limit headers, plus a
zKillboard R2Z2 feed under `/r2z2/`. Test-only `/_admin/*` endpoints mint tokens, revoke them, move characters (and put
them online), add structures and assets, publish killmail packages, add ESI latency and report calls and waypoints set.
Unit tests never reach the real zKillboard: `tests/conftest.py` keeps the alert loop off unless a test is marked
`alerts_loop`, and process tests point `EVEDM_ALERTS_FEED_URL` at the fake. It runs in-process
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


## Where the tests run

The full suite (lint, unit on Linux and Windows, integration + browser, footprint, image smoke test) runs in the GitHub `ci` workflow on **Python 3.13 only**. Locally use Python 3.13 (`pip install -r requirements-dev.txt && pip install -e . --no-deps`, `python -m pytest tests/unit`).
