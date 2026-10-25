# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## State of the repo

Pre-implementation: the only content is `Amazon List Watch – spec.md` (written in Swedish; the source of truth). There is no code, build, lint or test tooling yet, and no commits. This directory is a standalone project inside a larger workspace; the parent `/mnt/c/temp/github/claude/CLAUDE.md` describes Home Assistant tooling that does **not** apply here (the spec explicitly excludes any Home Assistant integration). Stage files explicitly; don't `git add` from the parent directory.

## What it is

Amazon List Watch ("listwatch") scrapes shared/public wishlists on amazon.se, stores per-product price history, and sends Apprise notifications when a price drops below a configured level. It is self-hosted in a Debian 13 LXC (Proxmox), single user, no login (LAN/Tailscale only), with its own web UI.

## Planned architecture (from the spec)

- **One Python 3.13 process** runs scheduler, collector, alert engine and web UI over one SQLite DB (WAL mode). Don't split into services.
- Stack: FastAPI + Jinja2 + HTMX (no frontend build), Chart.js, httpx + selectolax, APScheduler (interval with jitter), Apprise as a library (no Apprise server).
- **Collector sits behind a common source interface** so other sources (e.g. Hagglezon) can be added later without touching alerts or the GUI. The **parser is an isolated module** whose tests use saved real Amazon HTML pages as fixtures.
- Collection strategy: read prices from wishlist pages (paginated); fall back to the product page `amazon.se/dp/<ASIN>` only when price or Prime status is missing (capped per round). Local ASIN watches always use the product page.
- Politeness is a requirement: one request per 5–15 s (random), realistic User-Agent, `Accept-Language: sv-SE`; on CAPTCHA/HTTP 503 back off exponentially; after 3 blocked rounds in a row send an ops notification.
- Parse Swedish price format ("1 299,00 kr") to **integer öre**; never store floats.

### Data model
Tables: `wishlist`, `product` (unique ASIN), `wishlist_product`, `price_point`, `alert_rule`, plus `notification_log` and `settings` (Apprise URLs, intervals live in DB, not env). A product in several lists is stored and measured once. `price_point.pris_öre = NULL` means out of stock (still recorded; graphs show a gap). Removed list items are marked inactive, history is kept.

### Alert rules (easy to get subtly wrong — read the spec's "Larmlogik")
- Reference price = **median of valid Prime measurements over the last 90 days** (use less history if that's all there is; no percent alert before 7 days of data).
- Triggers: fixed price (`price ≤ target`) and/or percent (`price ≤ ref × (1 − pct/100)`); any active condition fires. Per-product rule overrides list default; if inheriting, use the **lowest** percent among the lists containing the product.
- Alert fires on downward crossing, then is suppressed until price drops a further ≥5 % below `senaste_larm_pris` or goes back above the level (which resets it).
- Only **Prime** offers alert. Prices <10 % of the median are "suspicious" and don't alert until confirmed by the next measurement.
- Optional "all-time low" alert, only after ≥30 days of history.

## Required repo layout (per the spec's install section)

`install.sh` at repo root; a locked dependency file; a Python package with CLI subcommands `listwatch serve | migrate | probe <url> | backup`; a `/health` endpoint; `deploy/` with `listwatch.service` and `listwatch-backup` timer/service files. Paths on the target: code `/opt/listwatch`, data `/var/lib/listwatch`, config `/etc/listwatch/listwatch.env` (created only if missing, never overwritten), user `listwatch`.

`install.sh` must be idempotent (re-run = update: `git fetch` + `reset --hard`, `migrate` with DB backup first), use `set -euo pipefail` with an error trap, and log to `/var/log/listwatch-install.log`.

## Build order

**Step 0 is `listwatch probe <url>`**, built before the rest: fetches a list with the service's real HTTP settings and reports HTTP status, CAPTCHA detection, page/product counts, and share of products with readable price / Prime status; saves raw HTML under `/var/lib/listwatch/probe/<date>/` (these become parser fixtures). Exit codes: 0 all readable, 1 partial, 2 blocked/error. Output includes a terminal report (counts of price/Prime read, out of stock, missing price, and "product pages needed per round"), saved `page-N.html` files and a `report.json` (same numbers plus one row per product: ASIN, title, price, Prime). Whether the list HTML exposes Prime status decides how many product-page fetches each round needs.

## Out of scope for v1
Other Amazon stores, private lists/login, multiple users, purchasing, Home Assistant. Phase 2 ideas: CSV export, weekly Apprise summary, more sources.

Repo is public: `github.com/emilgil/list-watch` (default `--repo`), so `install.sh` runs via `curl … | bash` and `--deploy-key` is only for a possible later switch to private. The spec also requires the same code to be runnable as a Docker container.
