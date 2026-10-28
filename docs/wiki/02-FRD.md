# FRD — EVE War Bot v2 (Enterprise Edition)

## 1. Project Overview
The **EVE War Bot** is a production-grade monitoring service that tracks war declarations in EVE Online. It provides real-time updates to Discord channels, featuring rich media, live scorecards, and structure ownership analysis.

## 2. Functional Requirements

### 2.1 Data Ingestion
- **ESI Integration:** Connects to CCP's ESI API to monitor the `/wars/` endpoint.
- **War Detection:** Identifies new wars using a persistent "watermark" to prevent duplicate notifications.
- **Structure Analysis:** 
    - Fetches structure data from `adam4eve.eu`.
    - For **Corporation wars**, lists all known structures for the corporation.
    - For **Alliance wars**, lists structures for every corporation belonging to the alliance.
- **Live Scorecard:** Calculates and displays:
    - Ships killed and ISK destroyed.
    - Efficiency percentages.
    - Hot systems (Top 3 by kill density).
    - Recent killmail links.

### 2.2 Notification Engine
- **Discord Webhooks:** Delivers notifications with custom branding.
- **Dynamic Banners:** Generates PNG images showing "Aggressor vs Defender" logos.
- **Live Updates:** Edits existing Discord messages as the battle progresses.
- **Rate Limiting:** Implements `Retry-After` handling and token bucket management to respect Discord's API limits.

### 2.3 Persistence & Statistics
- **SQLite Storage:** Uses `aiosqlite` for non-blocking storage of war states and historical data.
- **Monthly Reporting:** Generates summaries of wars declared on the 1st of each month.

## 3. Non-Functional Requirements

### 3.1 Technical Architecture
- **Asynchronous Core:** Built on `httpx` and `asyncio` to handle high-concurrency network requests.
- **Entity Caching:** Implements a TTL-based in-memory cache for tickers and names to minimize redundant API calls.
- **Batching:** Uses batching for database writes to maintain high performance under load.
- **Memory Efficiency:** Uses `ijson` to stream large JSON datasets (like structures) without consuming excessive RAM.

### 3.2 Deployment & Security
- **Containerization:** Deployed via Docker with resource limits (1GB RAM, 2 CPUs).
- **Persistence:** Uses Docker volumes to ensure database records survive container restarts.
- **Security:** Secrets are stored in `config/credentials.json`, which is excluded from version control.

## 4. User Experience
- **Dummy Manual:** Provides a "one-click" setup guide for non-technical users.
- **Observability:** Logs all events, errors, and rate-limit hits for easy debugging.
