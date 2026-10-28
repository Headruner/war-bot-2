# Architecture Overview - EVE War Bot v2

## High-Level Design
The EVE War Bot v2 is built as an asynchronous, event-driven service. The primary goal of this version is to decouple **Data Ingestion** from **Data Presentation** through a high-performance middleware layer.

## Key Components

### 1. Asynchronous Request Engine
- **Library:** `httpx`
- **Pattern:** Request Worker Pool
- **Description:** Instead of a linear sequence of calls, the bot maintains a pool of concurrent workers. When a batch of new wars is identified, the workers fetch the required details (Names, Corps, Allies) in parallel.

### 2. Entity Cache Layer
- **Implementation:** In-memory LRU (Least Recently Used) cache with TTL.
- **Scope:** 
    - `Corporation` (Ticker, Member Count, Name)
    - `Alliance` (Ticker, Name, Member Count)
    - `Universe Names` (System, Ship, Character)
- **Benefit:** Reduces the number of calls to `POST /universe/names/` and `GET /corporations/{id}/` by over 70% for active war zones.

### 3. Batching & Storage
- **Database:** SQLite (via `aiosqlite` for non-blocking operations).
- **Optimization:** The bot groups "pending" wars into batches. A single database transaction is used to mark a batch as "sent," which significantly reduces disk I/O overhead during high-activity periods.

### 4. Reporting Engine
- **Logic:** Computes scores, identifies "Hot Systems" using a weighted counter, and builds the Discord Embed.
- **Live Updates:** Uses a `PATCH` request strategy to update existing messages. It tracks "change fingerprints" to avoid unnecessary Discord API calls if the data hasn't changed.

## Data Flow
1. **Poll:** `EsiWarSource` fetches newest war IDs (Async).
2. **Ingest:** Parallel workers fetch details for new IDs; Cache is populated/queried.
3. **Process:** `ReportEngine` builds the JSON payload.
4. **Notify:** `DiscordNotifier` sends/edits the message with batching logic.
5. **Update:** Periodically, the `LiveUpdate` task refreshes scores and systems for active wars.
