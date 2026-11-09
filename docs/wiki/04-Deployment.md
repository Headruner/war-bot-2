# 04 · Deployment

## 1. Register your EVE application

1. Open <https://developers.eveonline.com/applications> and create an application.
2. Callback URL: the address you open the app at, plus `/sso/callback`, e.g. `http://localhost:8080/sso/callback` or
   `https://evedm.<tailnet>.ts.net/sso/callback`.
3. Scopes: `esi-location.read_location.v1`, `esi-location.read_ship_type.v1`, `esi-location.read_online.v1`,
   for the Notifications page `esi-characters.read_notifications.v1`, for the Structures page `esi-universe.read_structures.v1`, `esi-assets.read_assets.v1`, and for the **Set
   destination** buttons `esi-ui.write_waypoint.v1`. EVE SSO refuses the sign-in (`invalid_scope`) while one of them
   is missing on the application; `EVE_OPTIONAL_SCOPES=none` leaves the last three out. Characters signed in before
   a scope was added grant it with **Add character** (sign in with the main first).
4. Put the client ID and secret into `.env` (copy `.env.example`), together with a long random `EVEDM_SECRET_KEY` and your
   character ID in `EVEDM_ALLOWED_LOGIN_CHARACTERS`.

## 2. Run with Docker

The image is a Linux image; it runs on a Linux VM and on Windows/macOS through Docker Desktop.

| Way | Command |
| --- | --- |
| Published image (default) | `EVEDM_IMAGE=ghcr.io/headruner/war-bot-2:latest docker compose pull && docker compose up -d --no-build` (Linux `scripts/update.sh`, Windows `scripts\update.ps1`) |
| Build from source | `docker compose up -d --build` (`scripts/update.sh --build`, `scripts\update.ps1 -Build`) |
| Load the shipped image (0.3.0: no accounts, no Wars page) | `docker load -i evedm-image-0.3.0-amd64.tar.gz` (Windows: `scripts\load-image.ps1`) |
| GHCR with the servers' compose file | `EVEDM_IMAGE=ghcr.io/headruner/war-bot-2:latest docker compose -f deploy/docker-compose.yml up -d` |
| Try without an EVE app | `docker run --rm -p 8080:8080 -e EVEDM_DEMO=1 -e EVEDM_SECRET_KEY=x ghcr.io/headruner/war-bot-2:latest` |

Resource limits are part of the compose files: `cpus: 2`, `memory: 1g` (`EVEDM_CPUS`, `EVEDM_MEMORY` in `.env`).
The host port is `EVEDM_PUBLISH` in `.env` (default `8080`; `deploy/docker-compose.yml` defaults to `127.0.0.1:8080`).

### Linux VM, published image

Complete runbook for an Ubuntu/Debian VM with 1 or more vCPU and 1 GB RAM. Run the commands in order. Replace
`<user>` and `<vm-address>` with your VM login and IP, and `<port>` with `8080` unless it is taken. The app is one
container (`evedm`); everything persistent lives in `./data` on the VM (one-file SQLite database + logs).

#### 1. Connect to the VM

From PowerShell (Windows) or a terminal:

```bash
ssh <user>@<vm-address>
```

#### 2. Install the basic tools

```bash
sudo apt update
sudo apt install -y git curl openssl
```

#### 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
docker version
docker compose version
```

Both version commands must print without errors (Compose v2).

#### 4. Get the code

Way A, from GitHub (a private repository: sign in first):

```bash
sudo apt install -y gh
gh auth login
git clone -b main https://github.com/Headruner/war-bot-2.git evedm
cd evedm
```

Way B, copy the folder from a Windows PC. The first command runs in PowerShell on the PC, the rest on the VM:

```bash
scp -r C:\Users\andre\source\repos\evedm <user>@<vm-address>:~/
```

```bash
cd ~/evedm
rm -rf data logs .perf-data .ruff_cache
sed -i 's/\r$//' .env .env.example scripts/*.sh
chmod +x scripts/*.sh
```

The `sed` removes Windows line endings and the `chmod` restores the execute bit that the copy loses. This way also
copies the PC's `.env`; step 6 edits it.

#### 5. Register the EVE application

At <https://developers.eveonline.com/applications> create an application with

- callback `http://<vm-address>:<port>/sso/callback`, identical to `EVE_CALLBACK_URL` in step 6 (scheme, host, port);
- the scopes `esi-location.read_location.v1`, `esi-location.read_ship_type.v1`, `esi-location.read_online.v1`,
  `esi-characters.read_notifications.v1` (Notifications page), `esi-universe.read_structures.v1`,
  `esi-assets.read_assets.v1` (Structures page) and `esi-ui.write_waypoint.v1` (Set destination). A scope missing on
  the application makes EVE SSO answer `invalid_scope`.

Note the Client ID and the Secret.

#### 6. Create and fill `.env`

Way A starts from the template; with Way B an `.env` already exists, so skip the first command.

```bash
cp .env.example .env
sed -i "s|^EVEDM_SECRET_KEY=.*|EVEDM_SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n')|" .env
ss -ltn | grep ':8080 '
nproc
nano .env
```

`ss` printing a line means port 8080 is taken: use `8082` as `<port>` here and in step 5. `nproc` is the number of
CPUs of the VM. In `nano` check these lines (save: Ctrl+O, Enter, Ctrl+X):

```
EVEDM_SECRET_KEY=<filled by the sed command, keep it>
EVE_CLIENT_ID=<from step 5>
EVE_CLIENT_SECRET=<from step 5>
EVE_CALLBACK_URL=http://<vm-address>:<port>/sso/callback
EVEDM_PUBLISH=<port>
EVEDM_CPUS=1
EVEDM_ALLOWED_LOGIN_CHARACTERS=<your character ID>
```

- `EVEDM_CPUS=1` on a 1-vCPU VM, otherwise `2` (Docker refuses a limit above the host's CPU count).
- Your character ID is the number in `zkillboard.com/character/<id>/`; empty means anyone may sign in.
- `EVEDM_PUBLISH=127.0.0.1:<port>` makes the app reachable from the VM only (reverse proxy, Tailscale).
- Keep a copy of `EVEDM_SECRET_KEY`: a new key makes the stored tokens unreadable.

#### 7. Build and start

```bash
scripts/update.sh --no-git
```

The script gives `data/` to uid 10001 (the container user), lowers `EVEDM_CPUS` to the VM's CPU count, runs
`docker compose pull` and `docker compose up -d --no-build`, waits until the container is healthy and prints the address and the image revision. Nothing is built on the VM: CI publishes the image (Python 3.13) to GHCR after the tests pass. A private package needs `docker login ghcr.io` once, or make it public. To build from the checked-out source use `--build`. By hand, the same is:

```bash
mkdir -p data
sudo chown -R 10001:10001 data
EVEDM_IMAGE=ghcr.io/headruner/war-bot-2:latest docker compose pull evedm
EVEDM_IMAGE=ghcr.io/headruner/war-bot-2:latest docker compose up -d --no-build
```

#### 8. Verify

```bash
docker compose ps
curl -s http://127.0.0.1:<port>/api/health
docker compose logs --tail 50 evedm
```

`docker compose ps` must show `healthy`.

#### 9. Open the firewall (private network only)

```bash
sudo ufw status
sudo ufw allow <port>/tcp
```

Skip this when `ufw` is inactive. For access from outside use Tailscale and set `EVEDM_COOKIE_SECURE=1`
(section 4, phone access).

#### 10. Sign in

Open `http://<vm-address>:<port>/`, sign in with EVE, then **Characters > Add character**.

#### Apply a change

| What you changed | Command |
| --- | --- |
| `.env` | `docker compose up -d` (recreates the container; **`restart` does not re-read `.env`**) |
| `config/wars.yaml` (mounted read-only) | `docker compose restart evedm` |
| `config/evedm.example.yaml` (copied into the image as `/app/config/evedm.yaml`) | a new image: `scripts/update.sh` (or `--build`) |
| new code | `scripts/update.sh` (pulls the published image) |

Environment variables from `.env` always override the YAML files, so a value set in both places is taken from `.env`.

To edit the main YAML without rebuilding, mount a copy of it with a `docker-compose.override.yml` next to
`docker-compose.yml` (Compose merges it automatically), then run `docker compose up -d`:

```yaml
services:
  evedm:
    volumes:
      - ./config/evedm.yaml:/app/config/evedm.yaml:ro   # cp config/evedm.example.yaml config/evedm.yaml
```

#### Daily operation

```bash
cd ~/evedm
docker compose logs -f evedm                          # follow the log
docker compose stop evedm                             # stop (data stays)
docker compose start evedm                            # start again
docker compose down                                   # remove the container (data stays)
docker compose stop evedm && cp -a data data-backup-$(date +%F) && docker compose start evedm   # backup
```

#### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `evedm check` stops the start with a permission message | `sudo chown -R 10001:10001 data` |
| `Range of CPUs is from 0.01 to 1.00` | one vCPU: `EVEDM_CPUS=1` in `.env` (`scripts/update.sh` does it for you) |
| `port is already allocated` | free port in `EVEDM_PUBLISH`, same port in `EVE_CALLBACK_URL` and on the EVE application |
| SSO error about redirect / callback | callback in `.env` and on the EVE application differ (scheme, host, port) |
| `invalid_scope` | a scope of step 5 is not ticked on the EVE application, or set `EVE_OPTIONAL_SCOPES=none` |
| `bad interpreter: /bin/bash^M` | Windows line endings: `sed -i 's/\r$//' scripts/*.sh` |
| `Permission denied` on `scripts/update.sh` | `chmod +x scripts/*.sh`, or run `bash scripts/update.sh --no-pull` |
| container `unhealthy` or restarting | `docker compose logs --tail 50 evedm`; most often `EVEDM_SECRET_KEY` is empty |
| page not reachable from another PC | firewall (step 9), or `EVEDM_PUBLISH` is bound to `127.0.0.1` |
| `pip` fails on TLS during the build | corporate proxy: section 5 |

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
