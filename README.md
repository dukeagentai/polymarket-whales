<div align="center">

# 🐋 polymarket-whales

CLI whale tracker for Polymarket — terminal alerts when smart money moves.

[Quick Start](#-quick-start) · [Configuration](#️-configuration) · [Features](#-features) · [Contributing](#-contributing)

![demo](https://raw.githubusercontent.com/al1enjesus/polymarket-whales/main/assets/demo.gif)

</div>

---

## What is this?

`polymarket-whales` monitors the [Polymarket](https://polymarket.com) public Data API and fires an alert the moment a trade above your threshold hits the books — including the whale's wallet address. Prints to terminal with color-coded output. No sign-up, no API key, no infrastructure. Just Python.

**Don't want to self-host?** Subscribe to the live whale feed on Telegram: [@polymarketwhales_ai](https://t.me/polymarketwhales_ai)

---

## 📋 Example Output

```
══════════════════════════════════════════════════
🐋  polymarket-whales
══════════════════════════════════════════════════
  Min trade size : $500
  Check interval : 30s
══════════════════════════════════════════════════

🐋 WHALE ALERT  2026-03-20 14:23:01
───────────────────────────────────────────
Market : Will Trump tweet about crypto today?
Side   : YES
Amount : $2,847.00
Price  : 0.7300  (73% YES)
───────────────────────────────────────────

🐋 WHALE ALERT  2026-03-20 14:26:44
───────────────────────────────────────────
Market : Fed rate cut in March 2026?
Side   : NO
Amount : $12,500.00
Price  : 0.3100  (69% NO)
───────────────────────────────────────────
```

---

## ⚡ Quick Start

```bash
git clone https://github.com/al1enjesus/polymarket-whales
cd polymarket-whales
pip install -r requirements.txt
python main.py
```

That's it. Terminal alerts start immediately. No config needed to get started.

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and edit:

```env
MIN_TRADE_SIZE=500        # USD — only alert above this
CHECK_INTERVAL=30         # seconds between polls

# Optional — Telegram push alerts
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Optional — filters, cooldown, Postgres
MARKET_FILTER=Fed rate,Trump      # comma-separated title keywords or condition IDs
CATEGORY_FILTER=Politics,Crypto   # comma-separated categories
ALERT_COOLDOWN=300                # seconds between alerts per market (0 = off)
RETENTION_DAYS=0                  # prune resolved-market whale_trades older than this (0 = keep forever)
DATABASE_URL=postgresql://...     # defaults to local SQLite (whales.db)
```

Watchlist, consensus, and accumulation alerts live in `config.yaml` (see [Advanced](#️-advanced)) — the watchlist can also be managed live from the dashboard at `/watchlist`, no restart needed.

Or edit `config.yaml` directly. Environment variables take priority.

**Telegram setup (optional):**

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Message [@userinfobot](https://t.me/userinfobot) → copy your chat ID
3. Paste both into `.env`

> **Just want alerts without setup?** → [Join @polymarketwhales_ai](https://t.me/polymarketwhales_ai)

---

## ✨ Features

- ✅ Real-time polling of the Polymarket Data API (public, no auth needed)
- ✅ Configurable minimum trade size (USD)
- ✅ Colorized terminal output — YES in green, NO in red
- ✅ Optional Telegram push alerts to any chat or channel
- ✅ Optional Discord webhook alerts
- ✅ Export whale trades to CSV or JSON (`--export whales.csv`)
- ✅ Filter by market keyword, condition ID, or category (`--market`, `--category`)
- ✅ Tracks recurring whale wallets — auto-tags 🦈 Shark / 🐋 Whale / 🐳 Mega Whale by lifetime volume
- ✅ Per-market alert cooldown — hot markets can't spam you (muted trades still get recorded)
- ✅ Web dashboard (Flask) — stats, hourly volume chart, trades + top-wallet tables
- ✅ Persists to SQLite by default, Postgres via `DATABASE_URL` — one-click deploy on Railway
- ✅ Auto-resolves market names from condition IDs
- ✅ Trade deduplication — no double alerts
- ✅ Graceful handling of network errors and API timeouts
- ✅ Zero setup beyond `pip install` — SQLite works out of the box, no Docker needed
- ✅ Wallet watchlist — follow specific addresses, get alerted on every trade they make regardless of size; add/remove live from the dashboard, no restart
- ✅ Per-wallet pages — trade history, market breakdown, and live open positions / unrealized P&L (pulled from the data-api)
- ✅ 7d/30d wallet leaderboard, ranked by whale volume
- ✅ Smart-money consensus alerts — fires when N distinct whales hit the same side of the same market within a time window
- ✅ Accumulation alerts — catches wallets splitting one big order into many sub-threshold trades
- ✅ Tracker heartbeat + `/health` endpoint — monitor whether the polling worker is actually alive (uptime checks, Railway healthchecks)
- ✅ Leaderboard scout (`scout_leaderboard.py`) — pulls Polymarket's real top-traders leaderboard, vets each wallet's real win rate against resolved markets, and auto-populates the watchlist with the ones that qualify (capped watchlist size, safe to run on a schedule)
- ✅ Market resolution tracking (`resolve_markets.py`) — polls for markets that have resolved and settles every recorded trade WIN or LOSS
- ✅ Realized win/loss records + P&L — Record and Realized P&L columns on `/wallet/<address>`, `/watchlist`, and `/leaderboard`, computed from settled trades
- ✅ Position sync (`sync_positions.py`) — keeps watched-wallet P&L in the database instead of hitting the live API on every `/watchlist` page load
- ✅ Market pages (`/markets`, `/market/<condition_id>`) — see who's in a market, split into watched wallets vs. unknown ones, with resolution status
- ✅ "Unknown wallets worth a look" — top feed wallets not yet on the watchlist, one click to start watching
- ✅ Paginated trade history on the live feed (`?page=N`)
- ✅ Resolution alerts — Telegram/Discord message summarizing WIN/LOSS + P&L when a market with watched-wallet activity resolves
- ✅ JSON API (`/api/stats`, `/api/trades`, `/api/watchlist`, `/api/market/<condition_id>`) for programmatic access
- ✅ Configurable data retention (`RETENTION_DAYS`) — prune resolved-market trades past a cutoff to keep the database lean
- ✅ Automated pytest suite covering settlement, wallet PnL math, and resolution parsing
- ✅ Leaderboard scout skip-already-watched optimization — re-vetting is now skipped for wallets already on the watchlist, using their live win/loss record from `resolve_markets.py` instead of re-fetching REDEEM history (`--top 300`/`--keep 300` by default)
- ✅ Category scout (`category_scout.py`) — ranks wallets by real realized P&L within one Gamma category (weather, politics, sports, ...) over a trailing window, independent of the global leaderboard

---

## 🛠️ Advanced

**Run in background:**
```bash
nohup python main.py > whales.log 2>&1 &
```

**Custom config path:**
```bash
python main.py --config /path/to/config.yaml
```

**Export whale trades to CSV or JSON:**
```bash
python main.py --export whales.csv
python main.py --export whales.json
```

**Only watch specific markets or categories:**
```bash
python main.py --market "Fed rate" --market "Trump"   # title keyword or condition ID
python main.py --category Politics --category Crypto
```
Or set `filters:` in `config.yaml` / `MARKET_FILTER` + `CATEGORY_FILTER` in `.env`.

**Stop hot markets from spamming you:**
```yaml
# config.yaml — max one alert per market per 5 minutes
alert_cooldown: 300
```
Muted trades are still saved to the database, so the dashboard sees everything.

**Tag whale wallets:**
Wallets are tracked automatically and tagged by lifetime whale volume
(🦈 Shark ≥ $10k, 🐋 Whale ≥ $50k, 🐳 Mega Whale ≥ $250k, 🔁 Recurring after 3 trades).
Add your own tags in `config.yaml`:
```yaml
wallets:
  recurring_threshold: 3
  tags:
    "0xabc...": "Known insider"
```

**Web dashboard:**
```bash
python dashboard.py        # → http://localhost:8000
```
Live stats, hourly whale-volume chart, filterable trade history, and a top-wallets
leaderboard — reads from the same database the tracker writes to.

Pages:
- `/` — stats, hourly volume chart, trade history
- `/leaderboard?days=7|30` — wallets ranked by whale volume
- `/wallet/<address>` — trade history, market breakdown, live open positions & P&L for one wallet
- `/watchlist` — add/remove watched wallets, see their recent trades, live P&L, and market convergence (multiple watched wallets on the same side of the same market)
- `/health` — JSON status for uptime monitors: `{"ok": true, "tracker_alive": true, "tracker_last_poll": "..."}`

**Follow specific wallets (watchlist):**
Every trade a watched wallet makes is recorded and alerted regardless of `min_trade_size`. Manage from `/watchlist` in the dashboard (changes apply live, no restart) or in `config.yaml`:
```yaml
watchlist:
  min_trade_size: 0        # 0 = alert on any size for watched wallets
  addresses:
    "0xabc...": "Sharp Sam"
```

**Auto-populate the watchlist from Polymarket's leaderboard:**
```bash
python scout_leaderboard.py --dry-run                    # scan + report only
python scout_leaderboard.py                               # scan, vet, and add qualifiers to the watchlist
python scout_leaderboard.py --order-by VOL --top 100 --keep 30
python scout_leaderboard.py --max-keep-total 100          # cap on watchlist size (default) — new
                                                           # qualifiers stop being added once hit;
                                                           # already-watched wallets still get their
                                                           # label refreshed
```
Pulls the real `polymarket.com/leaderboard` top traders, then vets each one against
their actual trade history (trade count, and win rate computed from redeemed vs.
resolved markets) before adding them to the watchlist — so you're not just watching
whoever has the highest lifetime volume. Safe to run on a schedule (e.g. weekly via
Railway cron — see [Deploy to Railway](#-deploy-to-railway)) to keep the watchlist
fresh without growing it forever.

**Market resolution + settled win/loss records:**
```bash
python resolve_markets.py             # one pass: check unresolved markets, settle trades
python resolve_markets.py --loop 900  # run forever, one pass every 15 min
```
Polls the Gamma API for markets we've recorded trades in, and once one resolves,
marks every recorded trade in it WIN or LOSS. Powers the Record / Realized P&L
columns on `/wallet/<address>`, `/watchlist`, and `/leaderboard`, and the
resolution badge on `/market/<condition_id>`.

**Sync watched-wallet positions into the database:**
```bash
python sync_positions.py             # one pass: sync every watched wallet
python sync_positions.py --loop 300  # run forever, one pass every 5 min
```
Pulls each watched wallet's open positions from the data-api into the
`wallet_positions` table, so `/watchlist` reads one grouped DB query instead of
making a live API call per watched wallet on every page load.

**Smart-money consensus alerts:**
Fires when several distinct whale wallets hit the same side of the same market within
a time window — re-alerts only when the wallet count grows further:
```yaml
consensus:
  enabled: true
  min_wallets: 3
  window_minutes: 60
```

**Accumulation alerts:**
Catches wallets splitting a large order into many trades that individually stay under
`min_trade_size` — sums each wallet's sub-threshold trades per market over the window
and alerts once the total crosses the threshold:
```yaml
accumulation:
  enabled: true
  window_minutes: 60
  threshold: 0        # 0 = use min_trade_size
```

**Monitor the tracker itself:**
The worker writes a heartbeat to the database on every poll cycle. Check
`GET /health` on the dashboard service for uptime monitors, or the tracker
status banner shown on `/leaderboard` and `/watchlist`.

**24/7 on a VPS:** Any $5/month VPS works — the script uses <10MB RAM.

---

## 🚂 Deploy to Railway

The repo is Railway-ready (`Procfile` + `railway.json`). One project, three pieces:

1. **Create the project** — `railway init` (or "New Project → Deploy from GitHub repo" in the dashboard)
2. **Add Postgres** — "New → Database → PostgreSQL". Railway injects `DATABASE_URL` automatically.
3. **Web service (dashboard)** — deploys with the repo default: `gunicorn dashboard:app`. Add a domain under *Settings → Networking*.
4. **Worker service (tracker)** — add a second service from the same repo and set its start command to `python main.py`. Give it your `TELEGRAM_BOT_TOKEN` / `DISCORD_WEBHOOK_URL` variables, and reference the shared `DATABASE_URL`.

```bash
railway init && railway add --database postgres && railway up
```

Both services share the Postgres database: the worker writes whale trades, the dashboard reads them.

**Optional background jobs** — each is a small standalone script, so pick whichever
deployment fits: a long-running service (`--loop`), or a scheduled [Railway cron
job](https://docs.railway.com/reference/cron-jobs) that runs once and exits.

| Script | Purpose | Suggested cadence |
|---|---|---|
| `python resolve_markets.py` | Settle trades once their market resolves | every 15 min (`--loop 900`) or cron |
| `python sync_positions.py` | Keep `/watchlist` P&L reads off the live API | every 5 min (`--loop 300`) or cron |
| `python scout_leaderboard.py` | Refresh the watchlist from the real leaderboard | weekly cron |

All three read `DATABASE_URL` the same way the tracker and dashboard do — no extra
config beyond referencing the shared Postgres variable.

---

## 🤝 Contributing

Good first issues:

- [x] Discord / Slack webhook support _(merged — thanks [@Deepak8858](https://github.com/Deepak8858)!)_
- [x] Historical whale data export (CSV / JSON) _(merged — thanks [@Deepak8858](https://github.com/Deepak8858)!)_
- [x] Filter by specific market or category
- [x] Track and tag recurring whale wallets
- [x] Alert cooldown per market (avoid spam)
- [x] Web dashboard (simple Flask UI) + Postgres persistence + Railway deploy
- [x] Wallet watchlist with live P&L, per-wallet pages, and volume leaderboard
- [x] Smart-money consensus + accumulation alerts
- [x] Tracker heartbeat / `/health` endpoint
- [x] Leaderboard scout — auto-populate the watchlist from Polymarket's real top traders, vetted by win rate
- [x] Market resolution tracking + settled win/loss records + realized P&L (`resolve_markets.py`)
- [x] Position sync so `/watchlist` doesn't hit the live API on every page load (`sync_positions.py`)
- [x] Market pages — who's in a market, watched vs. unknown wallets (`/markets`, `/market/<condition_id>`)
- [x] Scheduled/automated leaderboard scout runs (cron-safe via `--max-keep-total`, documented under [Deploy to Railway](#-deploy-to-railway))
- [x] Test suite (`tests/` — settlement, wallet PnL/dedup math, resolution parsing, main.py pure functions)
- [x] Leaderboard scout expanded to top 300 with a skip-already-watched optimization (re-vetting wallets we already track wastes API calls once the watchlist is populated — see `scout_leaderboard.py`)
- [x] Per-category leaderboard scout (`category_scout.py`) — who's actually good at weather/politics/sports/etc., ranked by real realized P&L over a trailing window
- [ ] Wallet PnL trend chart — `wallet_pnl_snapshots` are already being collected by `sync_positions.py`; no chart on `/wallet/<address>` surfaces the trend yet
- [ ] Streamlit analytics mode (charts over historical whale data)
- [ ] WebSocket feed instead of polling
- [ ] Win-rate-weighted consensus alerts — weight the existing smart-money consensus check by (or require a minimum) average win rate among the converging wallets, using `db.wallet_record()`
- [ ] Category leaderboard page in the dashboard — persist `category_scout.py` output to a table and add a `/leaderboard?category=weather`-style view
- [ ] Slippage-aware alerting (surface whether a whale's trade meaningfully moved the price, not just its dollar size)
- [ ] Multi-wallet correlation across time (same cluster of wallets repeatedly moving together, beyond a single market)
- [ ] Backtest mode — replay historical trades through the alert rules to tune thresholds before going live
- [ ] Rate-limit / backoff tuning for the leaderboard/category scouts (currently a flat `--pause` between calls)

Open an issue or send a PR — both welcome.

---

## 📡 Community & Live Whale Feed

Join **[@polymarketwhales_ai](https://t.me/polymarketwhales_ai)** on Telegram:

- 🐋 Live feed of large trades — real-time, no setup required
- 💬 Community chat — discuss strategies, share setups, post your whale catches
- 🤖 AI bot connected — ask questions, get market context, analyze trades

Whether you're running the script or just lurking for signals — this is the place.

---

## 🌍 Blocked by geo-restrictions?

Polymarket is unavailable in the US and some other countries. If you can't access it, you have two options:

**Option A — Self-host with a VPN/proxy**
Point the script at a proxy by setting `HTTPS_PROXY` in `.env`:
```env
HTTPS_PROXY=http://your-proxy:port
```

**Option B — Use PolyClawster's relay (recommended)**

[PolyClawster](https://polyclawster.com) runs a transparent proxy to `clob.polymarket.com`, deployed in Tokyo (outside US geo-blocks). It routes your API calls on their behalf — your requests never touch Polymarket directly.

- 🚫 No VPN needed
- 🚫 No KYC
- ✅ Full Polymarket CLOB API access from any country
- ✅ One line of config

Set in `.env`:
```env
POLYMARKET_API_URL=https://polyclawster.com/api/clob-relay
```

Then in `main.py` the script will use this base URL for all CLOB requests instead of hitting Polymarket directly.

The relay is the same infrastructure used by [PolyClawster](https://polyclawster.com) AI agents to trade Polymarket 24/7 from any country.

---

## 🤖 Want trades executed automatically?

This tool watches. [PolyClawster](https://polyclawster.com) acts.  
AI agent that copies whale moves and trades Polymarket 24/7 — works from any country, no VPN, no KYC, start with $10.

[![PolyClawster](https://img.shields.io/badge/PolyClawster-Trade%20Automatically-8b5cf6?style=for-the-badge)](https://polyclawster.com)

---

MIT · Built by [Virixlabs](https://virixlabs.com)

