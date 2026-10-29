# EVE War Bot v2

EVE War Bot is a high-performance Python service designed to monitor and report **EVE Online war declarations** in real-time. It reads new war declarations directly from **CCP's official EVE Online API (ESI)** and posts them to a **Discord channel** as rich messages containing logos, banners, live-updating scorecards, and detailed reports.

## 🌟 Overview

Unlike standard trackers, EVE War Bot v2 is optimized for **high-concurrency** and **low-latency**. It handles "war rushes" efficiently using an asynchronous core, an entity caching system to minimize API load, and a batching database layer to ensure smooth updates even during intense combat periods.

### Key Features
- **Real-Time Notifications:** Instant Discord alerts with rich embeds.
- **Live Scorecards:** Automated updates to Discord messages as ships are killed and ISK is destroyed.
- **Visual Branding:** Dynamically generated banners and logos for both Aggressor and Defender sides.
- **Structure Visibility:** Displays publicly known structures for both parties in the conflict.
- **Hot System Tracking:** Identifies the top 3 systems with the most active combat.
- **Async Core:** Powered by `httpx` and `asyncio` for parallel data fetching.
- **Intelligent Caching:** Caches tickers, names, and member counts to stay within ESI rate limits while maintaining speed.

---

## 🚀 Quick Start Guide

Follow these steps to get the bot running on your server.

### Prerequisites
- **Docker** and **Docker Compose** installed.
- A **Discord Webhook URL** (Created in *Channel Settings -> Integrations -> Webhooks*).

### 1. Clone and Prepare
```bash
# Clone the repository
git clone https://github.com/Headruner/eve-war-bot-2.git
cd eve-war-bot-2

# Create the configuration files
cp config/credentials.example.json config/credentials.json
cp config/config.yaml.example config/config.yaml # If applicable
```

### 2. Configure Credentials
Edit `config/credentials.json` with your specific details:
```json
{
  "discord": {
    "webhook_url": "YOUR_DISCORD_WEBHOOK_URL",
    "mention_role_id": "OPTIONAL_ROLE_ID"
  },
  "esi": {
    "contact": "your-email@example.com"
  }
}
```

### 3. Launch the Bot
Start the service using Docker Compose:
```bash
docker compose up -d --build
```
View the logs to ensure the bot has connected successfully:
```bash
docker compose logs -f
```

---

## 🛠 Configuration

| Setting | Description | Default |
|---|---|---|
| `poll_interval_min` | How often to check ESI for new wars. | 15 |
| `batch_embeds` | Number of wars to group into a single Discord message. | 5 |
| `report_enabled` | Toggle the detailed war report in messages. | true |
| `live_updates` | Toggle automatic editing of Discord messages. | true |
| `dry_run` | If true, logs payloads instead of sending to Discord. | false |
| `statuses` | Only post these war types (`none`, `mutual`, `open_for_allies`). Unset posts all wars. | all |
| `bootstrap_hours` | On a clean database, also post wars declared in the last N hours (0 = only new ones). | 24 |
| `max_war_age_hours` | Do not post new war IDs declared longer ago than this (ESI re-issues old wars under new IDs). | 48 |

---

## 📊 Useful Commands

You can interact with the bot using the `stats` command:

- **View Monthly Stats:**
  ```bash
  docker compose run --rm eve-war-bot stats --months 12
  ```
- **Check Configuration:**
  ```bash
  docker compose run --rm eve-war-bot --check-config
  ```
- **Install Deployment Bundle:**
  ```bash
  docker compose run --rm eve-war-bot install --target /out
  ```

---

## 🏗 Architecture & Performance
EVE War Bot v2 is built for scale:
- **Concurrency:** Uses a worker pool to fetch multiple war components (Allies, Corps, Kills) in parallel.
- **Caching:** Implements an in-memory TTL cache for ESI entity data to reduce overhead.
- **Persistence:** Uses a SQLite backend with batching logic to ensure data integrity during high-volume events.

---
*Developed by Andrii Kunitsyn. Project documentation available in the `docs/` folder.*
