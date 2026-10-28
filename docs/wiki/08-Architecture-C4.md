# Architecture (C4) — EVE War Bot v2

## C1 — System Context
The **EVE War Bot** is a notification service for the EVE Online community.
- **Users:** Discord users, Alliance officers, and Corporation leaders.
- **External Systems:**
    - **ESI API:** Source of truth for war data and entity information.
    - **Discord:** Delivery platform for rich notifications.
    - **Adam4EVE:** Source of truth for structure data.
    - **SQLite:** Persistence layer for deduplication and historical stats.

## C2 — Containers
The system is deployed as a single Docker container:
- **Application Container:** Runs the Python 3.11 runtime.
- **Database Container (Embedded):** SQLite handles local persistence.
- **External APIs:** RESTful communication with ESI, Discord, and Adam4EVE.

## C3 — Components
The application is divided into the following functional modules:
- **WarWatcher:** Periodically polls ESI for new war declarations.
- **EntityResolver:** Handles name resolution and ticker fetching with a caching layer.
- **StructureAnalyzer:** Queries Adam4EVE for structure ownership data (Corp/Alliance).
- **ReportGenerator:** Orchestrates the construction of the Discord embed (Timeline, Score, Allies, Kills, Structures).
- **Notifier:** Manages asynchronous Discord webhook interaction, including rate-limiting and image uploads.
- **StatsManager:** Handles monthly war summary calculations.

## C4 — Code Structure
- `app/main.py`: Orchestration, CLI, and main loop.
- `app/esi.py`: Async client with retries and caching.
- `app/source.py`: Data ingestion logic.
- `app/report.py`: Embed construction and score calculation.
- `app/store.py`: Async SQLite database interaction.
- `app/structures.py`: Structure fetching and summary logic.
- `app/models.py`: Data classes and type definitions.
