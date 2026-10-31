# EVE Distance Meter · Wiki

EVE Distance Meter ranks your EVE Online characters by **stargate jumps** to a target system, shows what each one is
flying, and draws the route of the selected character on a **2D star map**. You read a notification (for example in the
Discord channel of your existing bot), type or paste the system, and decide which character to log in. Since 0.7.0 the
Alerts page also watches zKillboard's live feed for structures destroyed (quantum core drops), combat next to known
structures and structures firing, lists your closest characters and can set an online character's destination.

| Page | What it covers |
| --- | --- |
| [01 · Requirements](01-Requirements.md) | SRS v0.3 + additions to 0.7.0: decisions, use cases, EARS requirements with status and test traceability |
| [02 · Architecture (C4)](02-Architecture-C4.md) | C1 context, C2 containers, C3 components, C4 code, deployment, sequences, data model |
| [03 · Testing](03-Testing.md) | Test strategy, suites, fake EVE server, how to run, where logs go |
| [04 · Deployment](04-Deployment.md) | Docker on Linux and Windows, mounted database, EVE application setup, phone access |
| [05 · Performance](05-Performance.md) | 2 vCPU / 1 GB budget, measurements, tuning log |
| [06 · Operations](06-Operations.md) | Logs, launch records, backups, universe updates, troubleshooting |
| [07 · Run on Windows](07-Run-on-Windows.md) | Step-by-step: Docker Desktop, image load, .env, start, checks, troubleshooting, accounts, Wars, Structures, Alerts |
| [Architecture guidelines](../guidelines/architecture.md) · [Git guidelines](../guidelines/git.md) | Rules the project follows and how each is applied and verified |

![Ranking view](../screenshots/ranking-desktop.png)
