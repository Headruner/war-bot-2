# FRD — EVE War Bot v2

## 1. Functional Requirements

### 1.1 War Detection
- **FR-01:** The bot shall poll the ESI `/wars/` endpoint at a configurable frequency.
- **FR-02:** The bot shall identify new wars by comparing the highest seen ID (watermark) against the current ESI list.
- **FR-03:** The bot shall skip "skipped" wars (IDs that appear in ESI but return a 404 on details) to avoid retry loops.

### 1.2 Data Ingestion
- **FR-04:** For every new war, the bot shall fetch:
    - Aggressor/Defender IDs and types (Corp/Alliance).
    - Tickers and Names.
    - Declarations, Start times, and End times.
    - Mutual and Open For Allies flags.
- **FR-05:** The bot shall fetch the last 25 killmails (or max page) for each war to generate a "Recent Kills" feed.
- **FR-06:** The bot shall fetch the top 3 "Hot Systems" based on kill density from the latest killmails.
- **FR-07:** The bot shall fetch publicly known structures for both sides (if the alliance/corp has ≤ 20 corporations).

### 1.3 Discord Notifications
- **FR-08:** The bot shall send a rich embed containing logos, a banner, and the full scorecard.
- **FR-09:** The bot shall include a zKillboard link for the war and individual kills.
- **FR-10:** The bot shall perform "Live Updates" by editing the Discord message whenever the scorecard or timeline changes significantly.
- **FR-11:** The bot shall support "Batching" of notifications (e.g., grouping 5 wars into one message) to prevent Discord rate-limit triggers.

### 1.4 Storage & Reliability
- **FR-12:** The bot shall persist the "watermark" and "sent" statuses in a SQLite database.
- **FR-13:** The bot shall provide a healthcheck endpoint for Docker monitoring.
- **FR-14:** The bot shall provide a `stats` command to view monthly war summaries.

## 2. Non-Functional Requirements
- **NFR-01: Performance.** The bot must use Asynchronous I/O (`httpx`) to ensure that network latency does not block the main execution loop.
- **NFR-02: Efficiency.** The bot must cache static entity data (Names/Tickers) to minimize API calls.
- **NFR-03: Scalability.** The bot must be able to handle "War Rushes" of 50+ declarations without exceeding 10% CPU usage.
- **NFR-04: Observability.** The bot must log all errors, rate-limit events, and successes to `stdout`.
