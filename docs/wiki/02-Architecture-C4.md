# 02 · Architecture (C4 model)

Diagrams are written in PlantUML ([`docs/c4`](../c4)) and rendered to SVG/PNG in [`docs/diagrams`](../diagrams) with
`scripts/render-diagrams.sh` (set `PLANTUML_JAR` or install `plantuml`).

## Use cases

![Use cases](../diagrams/use-cases.svg)

## C1 · System context
Actors are the user, EVE SSO, ESI and CCP static data. The existing bot's Discord channel is read by the user only.

![C1](../diagrams/c1-system-context.svg)

## C2 · Containers
One Python process serves the browser UI and the JSON API; a one-file SQLite database (WAL) and the logs live on the `/app/data` volume; the universe file is
bundled. The desktop app (PySide6) is planned and will reuse the same core package.

![C2](../diagrams/c2-containers.svg)

## C3 · Components of the web app

![C3](../diagrams/c3-components.svg)

Key mechanisms:

- **Single-flight refresh + stale-while-revalidate** (`RankingService`): concurrent rankings share one ESI round; answers come
  from snapshots younger than 30 s while a background round runs every 5 s at most. The Refresh button waits for fresh data.
- **ESI client**: Expires/ETag cache, identical in-flight requests coalesced, 20 parallel calls, 429 Retry-After, 420 stop.
- **GateGraph**: CSR adjacency arrays; one BFS per target (cached) gives every character's route through parent pointers.
- **Tokens**: Fernet-encrypted in SQLite; refreshed when less than 60 s remain; `invalid_grant` marks the character REAUTH.

## C4 · Code

![C4](../diagrams/c4-code.svg)

## Deployment

![Deployment](../diagrams/deployment.svg)

## Sequences

![SSO](../diagrams/seq-sso-add-character.svg)

![Import](../diagrams/seq-import-bundle.svg)

![Rank and map](../diagrams/seq-rank-and-map.svg)

## Data model

![Data model](../diagrams/data-model.svg)

## CI pipeline

![CI](../diagrams/ci-pipeline.svg)
