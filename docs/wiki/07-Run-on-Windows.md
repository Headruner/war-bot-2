# 07 · Run on Windows (Docker Desktop)

Step-by-step guide for running EVE Distance Meter on a Windows PC with Docker Desktop.
All commands are for **PowerShell**, run from the repository folder `C:\Users\<you>\source\repos\evedm`.

## 0. Prerequisites

1. Docker Desktop running in **Linux containers** mode (default; tray icon > "Switch to Windows containers…" must *not* be active).
2. Check it:
   ```powershell
   docker version          # Server: OS/Arch must be linux/amd64
   docker compose version
   ```
3. Port 8080 free: `netstat -ano | findstr :8080` (no output = free).

## 1. Get the image (pick A or B)

**A. Build from source (0.6.0: accounts, Wars page with statistics, Structures page)**
```powershell
docker build -t evedm:0.6.0 .
```
(Behind a TLS-inspecting corporate proxy: `docker build --secret id=pipca,src=C:\path\ca.crt -t evedm:0.6.0 .`)

**B. Load the shipped image (older 0.3.0, offline, no accounts or Wars page)** — joins the 3 parts in `image\`,
verifies SHA-256, runs `docker load`:
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\load-image.ps1
docker image ls evedm        # expect evedm  0.3.0
```
Use `evedm:0.3.0` instead of `evedm:0.6.0` in the commands below if you take this way.

## 2. Smoke test in demo mode (no EVE application needed)

```powershell
docker run --rm -p 8080:8080 -e EVEDM_DEMO=1 -e EVEDM_SECRET_KEY=demo evedm:0.6.0
```
Open <http://localhost:8080/?target=Tama>. Stop with `Ctrl+C`.

Demo with the prepopulated database (10 fictional characters):
```powershell
docker run --rm -p 8080:8080 -v "${PWD}\data\demo:/app/data" `
  -e EVEDM_DEMO=1 -e EVEDM_SECRET_KEY=demo-secret-key-change-me evedm:0.6.0
```

## 3. Register your EVE application (once)

1. <https://developers.eveonline.com/applications> > Create application (Authentication & API Access).
2. Callback URL: `http://localhost:8080/sso/callback` (must match `EVE_CALLBACK_URL` exactly).
3. Scopes: `esi-location.read_location.v1`, `esi-location.read_ship_type.v1`, `esi-location.read_online.v1`,
   and for the Structures page `esi-universe.read_structures.v1`, `esi-assets.read_assets.v1`. EVE SSO refuses the
   sign-in (`invalid_scope`) while one of them is missing on the application; `EVE_OPTIONAL_SCOPES=none` leaves the
   last two out.
4. Copy the **Client ID** and **Secret Key**.

## 4. Create `.env`

```powershell
Copy-Item .env.example .env
# generate a strong secret key and print it
$b = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b)
notepad .env
```
Fill in:
```
EVEDM_SECRET_KEY=<the generated value - keep it; tokens cannot be decrypted without it>
EVE_CLIENT_ID=<client id>
EVE_CLIENT_SECRET=<secret key>
EVE_CALLBACK_URL=http://localhost:8080/sso/callback
EVEDM_ALLOWED_LOGIN_CHARACTERS=<your main character ID, comma-separated for several>
EVEDM_DEMO=0
EVEDM_SQLITE_JOURNAL=wal
EVEDM_WARS_ENABLED=1
EVEDM_WARS_CONTACT=<your e-mail or character name, sent to ESI>
# optional: also post new wars to Discord
EVEDM_WARS_DISCORD_ENABLED=0
EVEDM_WARS_DISCORD_WEBHOOK=
```
Character ID: the number in your character's URL on zkillboard.com or evewho.com.
Empty `EVEDM_ALLOWED_LOGIN_CHARACTERS` = any EVE character can sign in and get its own account (see section 9).
Then bind the port to this PC only: `"127.0.0.1:8081:8080"` in `docker-compose.yml`.

## 5. Start

Data folder on the host (`.\data` → `/app/data`), as in `docker-compose.yml`:
```powershell
docker compose up -d --build   # builds evedm:0.6.0 from source (or uses it if step 1A already built it)
docker compose ps              # STATUS should become "healthy" within ~30 s
docker compose logs -f evedm   # Ctrl+C to stop following
```
Open <http://localhost:8080>, sign in with EVE SSO, then **Characters > Add character** or **Import JSON**.
Rank: type or paste a system name (or a Dotlan / zKillboard / in-game link) on the main page.

Alternative with a named Docker volume (WAL always available, port bound to 127.0.0.1 only):
```powershell
docker compose -f deploy/docker-compose.yml up -d
```

## 6. Check it works

```powershell
curl.exe http://localhost:8080/api/health
docker exec evedm evedm check
docker exec evedm evedm rank Jita
```
Files on the host: `data\evedm.sqlite3`, `data\logs\evedm.log`, `data\logs\launches.jsonl`.

## 7. Daily operations

| Task | Command |
| --- | --- |
| Stop / start | `docker compose stop` / `docker compose start` |
| Remove container (data stays in `.\data`) | `docker compose down` |
| Backup tokens | Characters > Export JSON, or `docker exec evedm evedm export --out /app/data/backup.json` |
| Backup DB | `docker compose stop`, copy `data\evedm.sqlite3`, `docker compose start` |
| Update after code change | `git pull` then `docker compose up -d --build` |
| War watcher settings | edit `config\wars.yaml`, then `docker compose restart` (mounted read-only, no rebuild) |
| Wars from the console | `docker exec evedm evedm wars list`, `docker exec evedm evedm wars stats --months 12` |

## 8. Troubleshooting (Windows specifics)

| Symptom | Fix |
| --- | --- |
| `load-image.ps1 cannot be loaded … execution policy` | run it via `powershell -ExecutionPolicy Bypass -File …` (step 1A) |
| `checksum mismatch` | a part file is incomplete: delete `image\evedm-image-0.3.0-amd64.tar.gz`, restore the parts with `git checkout -- image`, retry |
| `pull access denied for evedm, repository does not exist` | the image is not in Docker yet, so Docker tries Docker Hub. Do step 1 first (`docker build -t evedm:0.6.0 .`), check with `docker image ls evedm` |
| `no matching manifest for windows/amd64` | Docker Desktop is in Windows-containers mode; switch to Linux containers |
| `port is already allocated` | change `"8080:8080"` to `"8081:8080"` in `docker-compose.yml` and update `EVE_CALLBACK_URL` and the callback in your EVE application to port 8081 |
| `missing required settings` | `.env` lacks `EVEDM_SECRET_KEY`, `EVE_CLIENT_ID` or `EVE_CLIENT_SECRET` |
| Sign-in refused (`not_allowed`) | add your character ID to `EVEDM_ALLOWED_LOGIN_CHARACTERS`, then `docker compose up -d` |
| SSO error about redirect / callback | callback URL in the EVE application and `EVE_CALLBACK_URL` differ (scheme, host, port) |
| Log: `journal_mode=WAL unavailable` | normal on some Windows bind mounts; app fell back to rollback journal. Use the named-volume variant to keep WAL |
| `cannot decrypt token` | `EVEDM_SECRET_KEY` changed; restore the old one or re-add characters |
| Shell scripts fail with `\r` errors (only if you run `scripts/*.sh` in WSL/Git Bash) | repo enforces LF via `.gitattributes`; run `git add --renormalize .` or re-clone |

## 9. Accounts: one site, several players

- Each player signs in with their **main** character. That creates their account and links the main to it.
- **Characters > Add character** (or Import JSON) links more characters to the signed-in main. Each player sees and
  ranks only their own characters. The same character can be linked by two players; each link has its own token.
- The sign-in lasts 90 days after the last visit on that browser (`web.session_days` in `config/evedm.example.yaml`).
  The cookie only identifies the main; tokens are kept encrypted in `data\evedm.sqlite3`.
- **Characters > Delete account** removes the main and all its links and revokes their tokens.
- Upgrading from 0.3.0: characters added before accounts existed are taken over automatically by the first main
  that signs in and is one of them. If your main was not among them, move them once:
  `docker exec evedm evedm users adopt --to <your main character ID>` (`evedm users list` shows the mains).

## 10. Wars page (merged eve-war-bot-2)

- **New wars**: the watcher checks ESI for new war declarations every 15 minutes, newest first. Each war lists the
  systems of both sides' public structures and the systems with recent kills. Click one to rank your characters.
- **Statistics**: every active war in New Eden (fighting, starting within 24 h, ending): counts, most active
  aggressors, most war-decced corporations, most destructive wars, high-sec structures at war by type and region.
  The first scan walks back through the newest 20,000 war IDs in the background (about 20 cycles of 15 minutes);
  the coverage line under the tables shows its progress.
- **High-sec structures**: public high-sec structures of both sides of every active war, nearest to your own
  characters first, with the war state (fighting, starts in …, ends in …). Filter by type, region, side, war state
  and security; click the system to rank your characters.
- The chosen tab, the filters, the map options (names, other characters, stargate links) and the ranking's
  auto-refresh interval are remembered in a cookie of your browser (`evedm_prefs`, one year); nothing is stored on
  the server, so another browser starts with the defaults.
- Settings: `config\wars.yaml` (its own file). Switch it off with `EVEDM_WARS_ENABLED=0` in `.env`.
- Discord posting (what the old bot did): set `EVEDM_WARS_DISCORD_ENABLED=1` and `EVEDM_WARS_DISCORD_WEBHOOK=<url>`
  in `.env`, then `docker compose up -d --force-recreate`. Messages link back to the Wars page; set
  `EVEDM_PUBLIC_URL` (for example your Tailscale address) so the link works for others.
- Stop the old eve-war-bot-2 container if both would post to the same channel.

## 11. Structures page (structure scout)

- Lists the player-owned structures (Upwell: citadels, engineering complexes, refineries, Keepstars …) in every kind
  of space where **any** character registered on the site has assets. Everyone signed in sees the whole list,
  sorted by jumps from their own nearest character; NPC stations never appear.
- It needs two extra scopes on your EVE application: tick `esi-universe.read_structures.v1` and
  `esi-assets.read_assets.v1` on developers.eveonline.com **before** upgrading, otherwise sign-in fails with
  `invalid_scope` (or set `EVE_OPTIONAL_SCOPES=none` in `.env` until you have).
- Characters added before need to grant them once: sign in again with the main and use **Add character** for each
  alt. The Characters page says "structures not shared" for characters that have not.
- The scout runs every 60 minutes, reads each character's assets once a day (locations only are kept) and each
  structure's name, owner and system every 3 days. A structure a character may no longer dock at keeps its last
  known data.
- Structures of corporations at war (from the Wars census) are marked **at war**, and the high-sec ones also appear
  in the Wars page's High-sec structures tab.
- The search box and filters are remembered in your browser like the other settings.

