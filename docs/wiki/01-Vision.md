# Vision — EVE War Bot v2

## Problem Statement
EVE Online is a massive sandbox game where "wars" (large-scale fleet battles) are common. Currently, players rely on manual observation or third-party websites to see who has declared war on whom. These sources can be slow to update, lack rich context (like logos or specific kill data), or require manual refreshing.

## Vision Statement
To provide a real-time, automated, and visually rich notification system for EVE Online war declarations, ensuring that community members and corporations are immediately informed of new conflicts with high-fidelity data (scorecards, logos, and structure information) delivered directly to their primary communication hub (Discord).

## Goals (KPIs)
- **G-01: Latency.** Notifications should reach Discord within 120 seconds of an ESI war declaration.
- **G-02: Fidelity.** Every notification must include logos for both sides and a "War Report" with at least 5 recent kills.
- **G-03: Concurrency.** The system must handle "War Rushes" (20+ declarations in < 1 minute) without missing events or crashing.
- **G-04: Reliability.** The bot should maintain 99.9% uptime via automated health checks and Docker restart policies.

## Stakeholders
- **Corporations/Alliances:** Need to know who is attacking them to coordinate defenses.
- **Community Members:** Want to follow the "meta" of current wars in real-time.
- **Developers:** Need a reliable, low-maintenance tool to alert their communities.
