# 04 · Deployment

## 1. Register your EVE application

1. Open <https://developers.eveonline.com/applications> and create an application.
2. Callback URL: the address you open the app at, plus `/sso/callback`, e.g. `http://localhost:8080/sso/callback` or
   `https://evedm.<tailnet>.ts.net/sso/callback`.
3. Scopes: `esi-location.read_location.v1`, `esi-location.read_ship_type.v1`, `esi-location.read_online.v1`,
   for the Structures page `esi-universe.read_structures.v1`, `esi-assets.read_assets.v1`, and for the **Set
   destination** buttons `esi-ui.write_waypoint.v1`. EVE SSO refuses the sign-in (`invalid_scope`) while one of them
   is missing on the application; `EVE_OPTIONAL_SCOPES=none` leaves the last three out. Characters signed in before
   a scope was added grant it with **Add character** (sign in with the main first).
4. Put the client ID and secret into `.env` (copy `.env.example`), together with a long random `EVEDM_SECRET_KEY` and your
   character ID in `EVEDM_ALLOWED_LOGIN_CHARACTERS`.

## 2. Run with Docker

The image is a Linux image; it runs on a Linux VM and on Windows/macOS through Docker Desktop.

| Way | Command |
| --- | --- |
| Build from source | `docker compose up -d --build` |
| Load the shipped image (0.3.0: no accounts, no Wars page) | `docker load -i evedm-image-0.3.0-amd64.tar.gz` (Windows: `scripts\load-image.ps1`) |
| Prebuilt from GHCR (after CI) | `EVEDM_IMAGE=ghcr.io/<owner>/<repo>:latest docker compose -f deploy/docker-compose.yml up -d` |
| Try without an EVE app | `docker run --rm -p 8080:8080 -e EVEDM_DEMO=1 -e EVEDM_SECRET_KEY=x evedm:0.7.0` |

Resource limits are part of the compose files: `cpus: 2`, `memory: 1g`. The host port is `EVEDM_PUBLISH` in `.env`
(default `8080`; `deploy/docker-compose.yml` defaults to `127.0.0.1:8080`).

### Linux VM, build from source

```bash
git clone -b main https://github.com/Headruner/war-bot-2.git evedm && cd evedm   # private repo: gh auth login first
cp .env.example .env && openssl rand -base64 48     # paste the output as EVEDM_SECRET_KEY
nano .env           # EVE_CLIENT_ID, EVE_CLIENT_SECRET, EVEDM_PUBLISH, EVE_CALLBACK_URL (see below)
ss -ltn | grep -E ':(8080|8082) '                   # pick a free host port
scripts/update.sh --no-pull                         # chown data/ to uid 10001, build, start, wait until healthy
```

- Port 8080 taken: `EVEDM_PUBLISH=8082` and `EVE_CALLBACK_URL=http://<vm address>:8082/sso/callback`, the address
  you type in the browser. Add exactly that callback URL to your EVE application.
- Firewall: `sudo ufw allow 8082/tcp` (only on a private network; for remote access use Tailscale, section 4).
- Later updates: `scripts/update.sh` (git pull, rebuild, restart, health check).
- A second instance has its own database: sign in and add the characters there, or move a whole database
  (stop both, copy `data/evedm.sqlite3`, keep the same `EVEDM_SECRET_KEY`).

## 3. Mounted database

Everything persistent lives in `/app/data` inside the container (`data/` on the host). The database is one SQLite file in WAL
mode; on shutdown the WAL is checkpointed back into it.

| Path | Content |
| --- | --- |
| `/app/data/evedm.sqlite3` | characters, encrypted tokens, snapshots, launches |
| `/app/data/logs/evedm.log` | application log, rotated at 1 MB × 5, secrets redacted |
| `/app/data/logs/launches.jsonl` | one line per start and stop |

- **Linux**: a named volume (`deploy/docker-compose.yml`, owned by uid 10001 automatically) or a host folder
  (`./data:/app/data`). The container runs as the non-root user 10001, so a host folder needs
  `sudo chown -R 10001:10001 data` once; otherwise `evedm check` stops the start with a clear message.
- **Windows (Docker Desktop)**: `-v C:\evedm\data:/app/data` works without permission changes. SQLite runs in WAL mode;
  if a bind-mounted folder cannot share WAL memory, the app logs a warning and falls back to the rollback journal by itself.
  A named volume always gets WAL.
- The database file is portable between Linux and Windows hosts. Keep the same `EVEDM_SECRET_KEY`, or tokens cannot be
  decrypted (export a bundle before changing it).

## 4. Phone access

Expose the app privately, for example with Tailscale on the VM (`tailscale serve --bg 8080`, or your `EVEDM_PUBLISH` port, gives
`https://<vm>.<tailnet>.ts.net`), then set `EVE_CALLBACK_URL` and `EVEDM_COOKIE_SECURE=1`. Do not publish port 8080 to the
internet; the compose file for servers binds to 127.0.0.1 by default.

## 5. Behind a TLS-inspecting proxy (build only)

`docker build --secret id=pipca,src=/path/ca.crt .` lets pip trust a corporate CA during the build. Any Debian/Ubuntu base
with python3 + venv works: `--build-arg BASE_IMAGE=<image>`.

## 6. Outbound connections

| Host | Why | Switch off |
| --- | --- | --- |
| `login.eveonline.com`, `esi.evetech.net` | sign-in, character locations, structures, wars, killmails, Set destination | — |
| `data.everef.net` | public structure dataset (Wars page) | `EVEDM_WARS_ENABLED=0` |
| `r2z2.zkillboard.com` | live killmail feed for structure alerts (one file per kill, ≥ 6 s pause when caught up) | `EVEDM_ALERTS_ENABLED=0` |
| `discord.com` | optional war and alert posts (webhooks) | leave the `*_DISCORD_ENABLED` settings at 0 |
