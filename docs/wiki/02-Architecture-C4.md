# 02 · Architecture (C4 model)

Diagrams are written in PlantUML ([`docs/c4`](../c4)) and rendered to SVG/PNG in [`docs/diagrams`](../diagrams) with
`scripts/render-diagrams.sh` (set `PLANTUML_JAR` or install `plantuml`).

## Use cases

![Use cases](../diagrams/use-cases.svg)

## C1 · System context
Actors are the user, EVE SSO, ESI (which also forwards an autopilot destination to the game client on **Set
destination**), zKillboard's live killmail feed R2Z2 (structure alerts), EVE Ref's structure dataset, Discord webhooks
(optional posts) and CCP static data.

![C1](../diagrams/c1-system-context.svg)

## C2 · Containers
One Python process serves the browser UI and the JSON API and runs four background tasks in its event loop: the
war watcher, the structure scout, the notification poller and the alert service. A one-file SQLite database (WAL) and the logs live on the
`/app/data` volume; the universe file is bundled. The desktop app (PySide6) is planned and will reuse the same core package.

![C2](../diagrams/c2-containers.svg)

## C3 · Components of the web app

![C3](../diagrams/c3-components.svg)

Key mechanisms:

- **Single-flight refresh + stale-while-revalidate** (`RankingService`): concurrent rankings share one ESI round; answers come
  from snapshots younger than 30 s while a background round runs every 5 s at most. The Refresh button waits for fresh data.
- **ESI client**: Expires/ETag cache, identical in-flight requests coalesced, 20 parallel calls, 429 Retry-After, 420 stop.
- **GateGraph**: CSR adjacency arrays; one BFS per target (cached) gives every character's route through parent pointers.
- **Tokens**: Fernet-encrypted in SQLite; refreshed when less than 60 s remain; `invalid_grant` marks the character REAUTH.
- **Structure alerts** (`AlertService`, 0.7.0): `R2Z2Feed` follows zKillboard's sequence (position kept in the `meta`
  table, 404 = caught up, 403/429 = 10 min pause). Each killmail is classified against the known structures of its
  system (scout finds + EVE Ref, positions in meters): structure victim → destroyed (core drop for citadels,
  engineering complexes, refineries), kill within `grid_km` → combat, structure attacker → firing. Alerts are stored
  once (unique index) and posted to Discord with a per-structure cooldown. The Alerts API groups them by structure
  and adds the main's three closest characters from the same cached per-target BFS the ranking uses.

- **Alliance gate** (`AllianceGate`, `access.py`): the Notifications and Structures tabs are members-only. The
  alliance of the main that signed in is read from ESI (public `/characters/{id}`) at sign-in, stored on the
  account (`users.alliance_id`) and read again every 6 hours; navigation, pages and APIs all ask the same gate and
  answer 404 for anyone else, so there is no link to find and no endpoint to probe.
- **Notifications for the whole site** (`NotificationService`): every registered character with the notifications
  scope and a valid token is polled with its own token once per ESI cache window (600 s), whoever linked it; the
  page lists them all and adds the viewer's own three closest characters to each card.
- **Structures from assets** (`StructureScout`): the assets of every registered character (ESI) reveal the structure
  IDs where something is stored; each ID is then read from `/universe/structures/{id}` with the token of a
  character that has an asset there (next character on 403). The pooled result feeds the Structures page.

## C4 · Code

![C4](../diagrams/c4-code.svg)

## Deployment

![Deployment](../diagrams/deployment.svg)

## Sequences

![SSO](../diagrams/seq-sso-add-character.svg)

![Import](../diagrams/seq-import-bundle.svg)

![Rank and map](../diagrams/seq-rank-and-map.svg)

![Structure alert and Set destination](../diagrams/seq-structure-alert.svg)

![Members-only tabs: Notifications and Structures](../diagrams/seq-members-tabs.svg)

![Ranking through Thera / Turnur](../diagrams/seq-thera-route.svg)

## Data model

![Data model](../diagrams/data-model.svg)

## CI pipeline

![CI](../diagrams/ci-pipeline.svg)
