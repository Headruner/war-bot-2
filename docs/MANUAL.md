# 👶 Manual for Dummies: Running EVE War Bot v2

Welcome! This guide will take you from "I have nothing" to "The bot is live on your server" in just a few minutes.

---

## 📋 Prerequisites
1. **Linux VM:** A basic DigitalOcean droplet (1GB RAM, 2 CPUs) is perfect.
2. **Docker & Docker Compose:** These must be installed on your VM.

---

## 🛠 Step 1: Get the Bot Code
Log into your VM via SSH and run these commands to download the bot:
```bash
git clone https://github.com/Headruner/eve-war-bot-2.git
cd eve-war-bot-2
```

## 🔑 Step 2: Configure Your Secrets
The bot needs to know where to send messages.
1. Create your configuration file:
   ```bash
   nano config/credentials.json
   ```
2. Paste your **Discord Webhook URL** inside the `webhook_url` field.
   *(Hint: To find this, go to your Discord Server -> Channel Settings -> Integrations -> Webhooks -> New Webhook)*.
3. Save and exit (Ctrl+O, Enter, Ctrl+X).

## 🐳 Step 3: Launch the Bot
Run the magic command to start everything:
```bash
docker compose up -d --build
```

### How do I know it's working?
Run this command to see the bot's "thoughts":
```bash
docker compose logs -f
```
You should see a message saying: `eve_war_bot started`.

---

## 📊 How to use the bot
- **Automatic Alerts:** The bot will automatically post a message in your Discord channel whenever a new war is declared.
- **Live Updates:** The bot will automatically update those messages with the current score (ships lost, ISK destroyed) and the most recent kills.
- **Structure Summaries:** Each message will show a summary of the structures owned by both sides.
- **Monthly Stats:** If you want to see a summary of wars from the last 12 months, run:
  ```bash

  docker compose run --rm eve-war-bot stats --months 12
  ```

## 🆘 Troubleshooting
- **"Docker command not found":** Make sure you installed Docker in Step 1.
- **"WebhookInvalid":** Your Webhook URL in `credentials.json` is wrong.
- **"Memory limit":** The bot is optimized for 1GB RAM, but ensure no other heavy apps are running on the VM.
- **Logs:** If anything breaks, the logs will tell you exactly what happened. Just run `docker compose logs -f`.
---
*This bot is powered by the v2.0 Async Engine.*
