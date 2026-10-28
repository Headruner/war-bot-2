# Architecture (C4) — EVE War Bot v2

## C1 — System Context
- **Users:** Discord community members, Alliance officers, Corporation leads.
- **External Systems:** 
    - **CCP ESI API:** Data source for wars, killmails, and entities.
    - **Discord Webhook:** The primary notification and interaction layer.
    - **Adam4EVE:** The source for structure ownership data.
    - **SQLite:** Local persistent storage.

## C2 — Containers
- **EVE War Bot (App Container):** 
    - **Language:** Python 3.11+
    - **Framework:** Asyncio / HTTPX
    - **Storage:** SQLite (aiosql._db)
- **External APIs:** ESI, Discord, Adam4EVE.

## C3 — Components
- **WarWatcher:** Monitors `/wars/` and manages the "watermark" to detect new events.
- **EsiClient:** A wrapper providing async requests with automatic retries and a TTL cache for entity data.
- **StructureAnalyzer:** Logic to fetch and summarize structures from Adam4EVE, handling the logic for both Corp and Alliance wars.
- **ReportGenerator:** Orchestrates the assembly of the Discord embed (Scorecards, Timeline, Allies, Killmails, Structures).
- **DiscordNotifier:** Handles high-performance webhook delivery, image uploads, and rate-limit management.
- **StoreManager:** Handles asynchronous database transactions.

## C4 — Code Structure
- `app/main.py`: Application lifecycle, CLI, and main loop.
- `app/esi.py`: The core network layer with caching and retry logic.
- `app/source.py`: Logic for war ingestion and party decomposition.
- `app/report.py`: Formatting logic for scores and Discord embeds.
- `app/store.py`: Database models and persistence.
- `app/structures.py`: Specific logic for the Adam4EVE integration.
- `app/models.py`: Unified data classes for all internal state.
