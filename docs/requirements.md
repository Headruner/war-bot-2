# Project Requirements Document (PRD) — EVE War Bot v2 (Enterprise Edition)

## 1. Project Overview
The **EVE War Bot** is an industrial-grade monitoring service for EVE Online war declarations. It bridges the gap between raw ESI data and community-facing Discord notifications, providing rich, live-updating intelligence on active conflicts.

## 2. Functional Requirements

### 2.1 War Detection & Data Ingestion
- **Source:** Real-time monitoring of CCP's official ESI API (`/wars/`).
- **Core Data:** Capture timestamps, status, participants, and scoring.
- **Deduplication:** The bot must maintain a persistent record of processed wars in a SQLite database to prevent duplicate posts upon restart.

### 2.2 Structure Analysis (New)
- **Source:** Data must be fetched from `adam4eve.eu`.
- **Corp Wars:** The bot must fetch and summarize all publicly known structures for the participating corporation.
- **Alliance Wars:** The bot must fetch and summarize structures for **every** member corporation in the alliance.
- **Summary Logic:** Reports must show a summary of structure counts (e.g., "Corporation X: 12 structures") and provide direct links to the Adam4EVE search results.

### 2.3 Notification & Presentation
- **Discord Integration:** All notifications must be delivered via Discord Webhooks.
- **Rich Media:** Dynamic banner generation for every war.
- **Live Updates:** Partial edits of Discord messages as the war progresses, without creating "spam" messages.
- **Scorecard:** Real-time calculation of ships lost, ISK destroyed, and efficiency.
- **Recent Activity:** Inclusion of the last 25 killmails with direct links to zKillboard.

### 2.4 Statistics & Logging
- **Monthly Stats:** The bot must generate a summary of wars declared on the 1st of each month for historical tracking.
- **Logging:** Detailed structured logging for errors, rate limits, and success events.

## 3. Non-Functional Requirements (Technical Specs)
- **Concurrency:** The bot must use an asynchronous framework (e.g., `httpx` + `asyncio`) to handle multiple concurrent network requests.
- **Memory Management:** Use `ijson` for streaming large datasets to stay within a **1GB RAM** limit.
- **Resiliency:** Implement exponential back-off retries for long-running or flaky API responses.
- **Environment:** Optimized for Docker deployment on a Linux VM with 2 CPU cores.

## 4. User Experience
- **Manual for Dummies:** Clear, step-by-step instructions for non-technical users.
- **Discord Integration:** Intuitive UX for users with automatic role mentions.
