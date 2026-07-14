# TODO — Roadmap to make polymarket-whales feature-complete

**Status: Phases 1-6 implemented.** Phase 1 (markets + resolution + settlement),
Phase 2 (realized PnL + win/loss records), Phase 3 (position sync — fixes the
watchlist lag), and Phase 4 (market participant pages) are done — see
`resolve_markets.py`, `sync_positions.py`, `migrate_002_trade_results.py`, and the
`Market`/`WalletPnlSnapshot`/`WalletPosition` tables + helpers in `db.py`. One
deviation from the plan worth knowing: Gamma's `/markets` endpoint implicitly
filters to `closed=false` unless `closed=true` is passed explicitly, so
`resolve_markets.py` does a two-phase fetch (plain query, then a `closed=true`
retry for anything the first pass didn't return) — see the comment in
`run_once()`.

Phase 5 (tracked/unknown wallet promotion flow) and Phase 6 (housekeeping) are
also done: watched-wallet 👀 markers on the feed, an "Unknown wallets worth a
look" card on `/watchlist`, win/loss Record + Realized P&L columns on
`/leaderboard` and the top-wallets card, a `--max-keep-total` cap on
`scout_leaderboard.py`; JSON API endpoints (`/api/stats`, `/api/trades`,
`/api/watchlist`, `/api/market/<condition_id>`); pagination on the live feed
(`?page=N`); composite DB indexes (`migrate_003_indexes.py`); a `retention_days`
config knob (`prune_old_trades`, wired into `main.py`'s loop); Telegram/Discord
alerts on market resolution via a shared `notify.py`; and a pytest suite under
`tests/` (settlement, wallet_record, resolution parsing, main.py pure functions —
run with `pytest tests/`, needs `requirements-dev.txt`).

Two Phase 6 items were deliberately **not** implemented — both were flagged
low-priority/deferred in the plan itself, not accidentally missed:
- **Alert dedupe across restarts** (backfilling `SeenTrades` from the DB on
  startup) — marked "low priority" in the original plan.
- **WebSocket feed** instead of polling — marked "big item, keep last"; the
  polling loop still works fine, this would be a larger architectural change.

This is an implementation plan, ordered by priority. Each item says **what to build,
where, the schema/function signatures to use, and how to verify it**. Follow the
existing code style (SQLAlchemy models in `db.py`, plain functions, emoji log lines).

Context on the current architecture (do not change it):

- **Two processes**: `main.py` (polling worker, writes trades) and `dashboard.py`
  (Flask, reads DB + makes some live API calls). They share one DB (SQLite locally,
  Postgres on Railway via `DATABASE_URL`).
- Tables today: `whale_trades` (feed trades ≥ threshold), `wallets` (auto-tracked
  stats per wallet seen in the feed), `watched_addresses` (user watchlist),
  `watched_trades` (all trades by watched wallets), `tracker_status` (heartbeat).
- APIs: `data-api.polymarket.com` (trades, positions, activity, leaderboard — public),
  `gamma-api.polymarket.com` (market metadata; use `condition_ids` param). The CLOB
  API needs auth — don't use it.
- `Base.metadata.create_all()` creates new tables automatically, but does **not**
  add columns to existing tables. Any new column on an existing table needs a
  migration script like `migrate_001_widen_trade_id.py` (copy that pattern:
  idempotent, works on both SQLite and Postgres).

---

## Phase 1 — Markets as first-class entities + resolution tracking

**Why:** We currently store only a market title string on each trade. We never learn
whether a market resolved or who won, so we can't compute realized win/loss for any
wallet. Everything in Phases 2–3 depends on this.

### 1.1 New `markets` table (`db.py`)

```python
class Market(Base):
    __tablename__ = "markets"

    condition_id = Column(String(128), primary_key=True)
    title        = Column(String(512), default="")
    slug         = Column(String(256), default="")
    event_slug   = Column(String(256), default="")
    category     = Column(String(128), default="", index=True)
    end_date     = Column(DateTime, nullable=True)        # from Gamma endDate
    # Resolution state
    resolved     = Column(Integer, default=0, index=True) # 0 = open, 1 = resolved
    winning_outcome = Column(String(128), default="")     # "Yes" / "No" / "France" / ...
    resolved_at  = Column(DateTime, nullable=True)        # when WE detected it
    # Bookkeeping
    first_seen   = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_checked = Column(DateTime, nullable=True)        # last resolution poll
```

Helper functions to add in `db.py`:

- `upsert_market(session, *, condition_id, title="", slug="", event_slug="", category="", end_date=None)`
  — insert if missing, fill in blanks if known fields are empty. Never overwrite a
  non-empty title with an empty one. Call `session.commit()`.
- `unresolved_markets(session, limit=100)` — markets with `resolved == 0`, ordered by
  `last_checked` ascending nulls-first (check the never-checked ones first). Note:
  SQLite and Postgres order NULLs differently — use
  `order_by(Market.last_checked.is_(None).desc(), Market.last_checked.asc())` which
  works on both.
- `mark_market_resolved(session, condition_id, winning_outcome)` — set
  `resolved=1`, `winning_outcome`, `resolved_at=now`.

### 1.2 Populate `markets` from the trade pipeline (`main.py`)

In `run()`, at the point where a whale trade or watched trade is persisted (both
branches), also call `db.upsert_market(...)` in the same `with Session()` block,
using data already in hand: `condition_id`, `base_title`, `trade.get("slug", "")`,
`trade.get("eventSlug", "")`, `category`, and `end_date` if the trade payload has
`endDate` (it usually doesn't — leave None, the resolution checker fills it in).
Skip when `condition_id` is empty.

### 1.3 Resolution checker (new file: `resolve_markets.py`)

A standalone script, same shape as `scout_leaderboard.py` (argparse, `db.init_db()`,
colorama output), that can be run manually, by cron, or in a loop:

```
python resolve_markets.py            # one pass over unresolved markets
python resolve_markets.py --loop 900 # run forever, one pass every 15 min
python resolve_markets.py --limit 50 # cap markets checked per pass
```

Per pass:

1. `unresolved_markets(session, limit=args.limit)` → list of condition_ids.
2. Batch them 20 at a time against Gamma:
   `GET https://gamma-api.polymarket.com/markets?condition_ids=<id1>&condition_ids=<id2>&...`
   (pass a list to `requests` params: `{"condition_ids": chunk}`). Sleep
   `--pause` (default 0.2s) between chunks — copy `_get_json` retry helper from
   `scout_leaderboard.py`.
3. For each returned market object:
   - Update blanks on our row (title, slug, category, `endDate` → `end_date`,
     parse with `datetime.fromisoformat(s.replace("Z", "+00:00"))`).
   - Detect resolution: Gamma marks finished markets with `closed: true` and
     exposes `outcomePrices` (JSON string like `'["1", "0"]'`) plus `outcomes`
     (JSON string like `'["Yes", "No"]'`). A market is **resolved** when
     `closed` is true AND some outcome price is ≥ 0.99. The winning outcome is
     `outcomes[i]` where `outcomePrices[i]` is the max. Both fields arrive as
     JSON-encoded strings — `json.loads` them, guard with try/except.
   - If resolved → `mark_market_resolved(...)`, log a line, and run the
     settlement step (1.4) for that market.
   - Whether or not resolved, set `last_checked = now` and commit.
4. Markets Gamma didn't return at all: still bump `last_checked` so they don't
   block the queue forever.

Also handle **stale markets**: if a market's `end_date` is more than 30 days past
and Gamma still doesn't report it closed, log it but keep checking (some markets
resolve very late) — do NOT auto-resolve on end_date alone.

**Deploy:** add to `Procfile` docs/README — on Railway this is a third service with
start command `python resolve_markets.py --loop 900`, or use Railway's cron feature
with `python resolve_markets.py`. Locally: `--loop` mode.

### 1.4 Settlement — mark trades won/lost when a market resolves

Add columns to `whale_trades` and `watched_trades` (**requires a migration script**,
`migrate_002_trade_results.py`, modeled on `migrate_001`):

```python
# on both WhaleTrade and WatchedTrade:
result = Column(String(8), default="", index=True)  # "" = open, "WIN", "LOSS"
```

Add in `db.py`:

```python
def settle_market_trades(session, condition_id: str, winning_outcome: str) -> dict:
    """Mark every trade in a resolved market WIN or LOSS.
    Returns {"whale_trades": n, "watched_trades": n} counts updated."""
```

Rules (keep them simple and explicit — a trade is a bet on an outcome):

- `watched_trades` rows have `outcome` (what they bet on) and `side` (BUY/SELL).
  - BUY of the winning outcome → WIN; BUY of a losing outcome → LOSS.
  - SELL of the winning outcome → LOSS; SELL of a losing outcome → WIN.
  - Compare case-insensitively, strip whitespace.
- `whale_trades` rows only have `side` (already normalized YES/NO for binary
  markets, or the raw outcome name for multi-outcome). Same comparison:
  `side == winning_outcome` (case-insensitive) → WIN, else LOSS. For YES/NO
  markets Gamma's outcomes are "Yes"/"No", so uppercase both sides before comparing.
- Only touch rows where `result == ""` (idempotent — safe to re-run).

Call `settle_market_trades` from `resolve_markets.py` right after
`mark_market_resolved`.

### 1.5 Verification for Phase 1

- Run the tracker a few minutes so `markets` fills up, then `python
  resolve_markets.py --limit 10` and confirm rows get `last_checked` set.
- Manually insert a market row with a condition_id of an already-resolved market
  (grab one from Gamma with `closed=true`), insert a fake watched_trade on it,
  run the checker, confirm `result` flips to WIN/LOSS correctly for both a BUY
  of the winner and a BUY of the loser.

---

## Phase 2 — Realized P&L + win/loss records per wallet

**Why:** the user wants PnL and win/lose per wallet, computed from OUR recorded
history (works for both watched wallets and random feed whales) — not just the
live unrealized number from the positions API.

### 2.1 Per-wallet record aggregation (`db.py`)

```python
def wallet_record(session, address: str) -> dict:
    """Win/loss record from settled trades (both tables, deduped by trade_id).
    Returns {"wins": int, "losses": int, "open": int, "win_rate": float|None,
             "realized_pnl": float}"""
```

- Wins/losses: count settled rows per table for this wallet
  (`WhaleTrade.wallet == address` / `WatchedTrade.address == address`). A trade
  present in both tables (watched wallet whose trade was also whale-sized) must
  count once — collect `trade_id` sets and union them.
- **Realized PnL per settled BUY trade** (approximation from our data —
  document this in the docstring):
  - WIN: shares won pay out $1 each. Shares ≈ `amount_usd / price` (guard
    price ≤ 0). PnL = `shares * 1.0 - amount_usd`.
  - LOSS: PnL = `-amount_usd`.
  - SELL trades: skip in v1 (we don't know their cost basis). Count them in
    wins/losses but exclude from realized_pnl, and note it in the docstring.
- `win_rate = wins / (wins + losses)` or None when nothing settled.

### 2.2 PnL history snapshots (new table)

The README already lists "Wallet PnL tracking over time" as a wanted feature.

```python
class WalletPnlSnapshot(Base):
    __tablename__ = "wallet_pnl_snapshots"

    id          = Column(Integer, primary_key=True)
    address     = Column(String(64), index=True)
    taken_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    open_value  = Column(Float, default=0.0)   # from positions API
    unrealized  = Column(Float, default=0.0)   # from positions API (cashPnl sum)
    realized    = Column(Float, default=0.0)   # from wallet_record()
    wins        = Column(Integer, default=0)
    losses      = Column(Integer, default=0)
```

Snapshots are written by the position-sync job (Phase 3.2) once per sync cycle,
but at most one snapshot per wallet per 6 hours (check the latest `taken_at`
before inserting).

### 2.3 Surface it in the UI (`dashboard.py`)

- `/wallet/<address>`: add tiles "Record" (`12W–5L (71%)`) and "Realized P&L"
  from `wallet_record()`. Add a small sparkline-style section for snapshot
  history later (optional — plain table of snapshots is fine for v1).
- `/watchlist`: add "Record" and "Realized P&L" columns next to the existing
  unrealized P&L column.
- Recent-trades tables (`/`, `/watchlist`, `/wallet/...`): show a ✅/❌ marker
  on rows where `result` is WIN/LOSS (empty string → no marker).

---

## Phase 3 — Stop ad-hoc API calls from blocking page loads

**Why (user's complaint):** `/watchlist` calls `fetch_positions()` synchronously
for EVERY watched address on every page load (dashboard.py, the
`pnl = {a.address: positions_summary(fetch_positions(a.address)) ...}` line).
With 30 watched wallets and a cold cache that's 30 sequential HTTP calls × up to
8s timeout each — that's the lag. Fix: the worker syncs positions into the DB in
the background; the dashboard only reads the DB.

### 3.1 New `wallet_positions` table (`db.py`)

```python
class WalletPosition(Base):
    __tablename__ = "wallet_positions"

    id           = Column(Integer, primary_key=True)
    address      = Column(String(64), index=True)
    condition_id = Column(String(128), index=True)
    title        = Column(String(512), default="")
    outcome      = Column(String(128), default="")
    size         = Column(Float, default=0.0)
    avg_price    = Column(Float, default=0.0)
    cur_price    = Column(Float, default=0.0)
    current_value= Column(Float, default=0.0)
    cash_pnl     = Column(Float, default=0.0)
    percent_pnl  = Column(Float, default=0.0)
    synced_at    = Column(DateTime, index=True)
```

Helpers:

- `replace_wallet_positions(session, address, positions: list)` — delete this
  address's rows, insert the new list, one commit. (Field mapping from the
  data-api positions payload: `title`, `outcome`, `size`, `avgPrice`,
  `curPrice`, `currentValue`, `cashPnl`, `percentPnl`, `conditionId`.)
- `get_wallet_positions(session, address) -> list[WalletPosition]`
- `positions_summary_db(session, addresses: list) -> dict` — one grouped query:
  `{address: {"value": sum(current_value), "pnl": sum(cash_pnl), "count": n, "synced_at": max}}`.

### 3.2 Position-sync job (new file: `sync_positions.py`)

Same standalone-script shape as `resolve_markets.py`:

```
python sync_positions.py             # one pass: sync all watched addresses
python sync_positions.py --loop 300  # every 5 min
```

Per pass: for each `watched_address`, GET `{DATA_API}/positions?user=<addr>&limit=100`
(reuse the retry helper), `replace_wallet_positions(...)`, sleep `--pause`
(default 0.3s) between wallets. After syncing a wallet, write a
`WalletPnlSnapshot` if the 6-hour gate allows (Phase 2.2).

**Alternative accepted:** instead of a third/fourth process, fold both the
resolution check and the position sync into `main.py`'s loop — run them every
N cycles (e.g. resolution every 20 cycles ≈ 10 min, positions every 10 cycles).
Simpler to deploy (no new Railway service). Implement as functions imported from
`resolve_markets.py` / `sync_positions.py` so both modes work; add config keys:

```yaml
jobs:
  resolve_every_cycles: 20    # 0 = disabled in worker (run standalone instead)
  positions_every_cycles: 10
```

Prefer the in-worker mode as the default — it keeps the Railway topology at two
services.

### 3.3 Dashboard reads DB instead of the API (`dashboard.py`)

- `/watchlist`: replace the `fetch_positions` loop with one
  `positions_summary_db(session, [a.address for a in addresses])` call inside
  the existing session block. Show a muted "synced Xm ago" note (from max
  `synced_at`) under the table header. If a wallet has never been synced, show
  "—" rather than calling the API inline.
- `/wallet/<address>`: read `get_wallet_positions()` from the DB. Keep
  `fetch_positions()` as a fallback ONLY when the DB has zero rows for the
  address AND the address is not watched (random feed wallet the sync job never
  covers) — that keeps per-wallet pages useful for unknown whales without
  slowing the watchlist page.
- Delete the `_positions_cache` TTL machinery once the above works (or keep it
  only for the unknown-wallet fallback path).

---

## Phase 4 — Market pages: who's in a contract (tracked vs unknown)

**Why (user's ask):** "track contracts so that we can see who is in it, like
known/unknown users versus tracked users."

### 4.1 `db.py`

```python
def market_participants(session, condition_id: str) -> dict:
    """Everyone we've seen trade this market.
    Returns {"watched": [...], "unknown": [...]} where each entry is
    {address, label_or_tag, trades, volume, outcomes: {outcome_side: usd}, last_traded}.
    'watched' = address in watched_addresses; 'unknown' = everyone else."""
```

Pull from BOTH `whale_trades` (filter `condition_id`) and `watched_trades`,
merge per address (dedupe on trade_id like 2.1), then split by membership in
`watched_addresses`.

### 4.2 New route `/market/<condition_id>` (`dashboard.py`)

- Header: market title, category, resolved badge (`✅ Resolved: <outcome>` or
  `🟢 Open`), end date — from the `markets` table.
- Two tables: "Watched wallets in this market" and "Other wallets", both from
  `market_participants()`, columns: wallet (link to `/wallet/`), label/tag,
  position(s) (outcome+side with $ each), total $, last trade, and result
  (WIN/LOSS once resolved).
- Link market titles to this page everywhere a market is shown (`/`,
  `/watchlist` convergence table, `/wallet/...` top-markets table). The trades
  already carry `condition_id`; where a table only has the title (e.g.
  `wallet_market_breakdown`), add `condition_id` to the query's group-by/output.
- Add a "Markets" index page `/markets` listing markets from the `markets`
  table: title, category, open/resolved, watched-wallet count, total tracked $,
  sortable open-first. Add it to the nav.

---

## Phase 5 — Formalize tracked vs unknown wallets

**Why (user's ask):** "track users we want to track as well as unknown users who
pop up in the feed." Most of this exists (`wallets` = anyone seen in the feed,
`watched_addresses` = explicit follows) — what's missing is promotion flow and
visibility.

- [ ] Add `is_watched` awareness to the main feed page: in the recent-trades
  table on `/`, show a 👀 marker next to wallets that are on the watchlist
  (one query for the watched set, check membership in the template).
- [ ] "Unknown wallets worth a look" card on `/watchlist`: top 10 wallets from
  `wallets` NOT in the watchlist, ranked by `total_usd`, each with a one-click
  "👀 Watch" button (the `/watchlist/add` form already exists on
  `/leaderboard` — reuse that pattern).
- [ ] Wallet record columns (from Phase 2) on both the `/leaderboard` and the
  top-wallets card so "unknown" wallets show their track record before you
  decide to watch them.
- [ ] Scheduled scout: document (README + `railway.json` note) running
  `python scout_leaderboard.py` on Railway cron weekly. Add `--max-keep-total`
  guard: skip adding if the watchlist already has ≥ N entries (avoid unbounded
  growth). Default 100.

---

## Phase 6 — Robustness / housekeeping (smaller, independent items)

- [ ] **JSON API endpoints** so the UI (or anything else) can poll without a
  full page render: `/api/stats`, `/api/trades?limit=&q=&category=&wallet=`,
  `/api/watchlist`, `/api/market/<condition_id>`. Return the same dicts the
  templates use; use Flask `jsonify`. Keep the HTML pages server-rendered.
- [ ] **Pagination** on `/` trades table (`?page=N`, 100/page, prev/next links)
  — right now it silently truncates at 100.
- [ ] **DB indexes**: composite index on `whale_trades (condition_id, side, traded_at)`
  (used by the consensus query every whale trade) and `whale_trades (wallet, traded_at)`.
  New-table indexes are covered above. Needs migration for existing deployments
  (`migrate_003_indexes.py`) — `CREATE INDEX IF NOT EXISTS` works on both engines.
- [ ] **Retention**: optional `retention_days` config (default 0 = keep forever);
  a `prune_old_trades(session, days)` helper called once per worker start and
  daily thereafter. Never prune `watched_trades` (that's the user's own record) —
  only `whale_trades` older than the cutoff **whose market is resolved**.
- [ ] **Tests** (README already wants them): `pytest`, in-memory SQLite
  (`db.init_db("sqlite:///:memory:")`). Priority order:
  1. `settle_market_trades` — all 4 side/outcome combinations + idempotency.
  2. `wallet_record` — dedupe across the two trade tables, PnL math.
  3. Resolution parsing — feed the checker canned Gamma payloads
     (`closed`, `outcomePrices` as JSON strings, malformed JSON).
  4. `matches_filters`, `trade_unique_id`, `parse_trade_usd_size` (pure functions
     in `main.py`).
  Add `pytest` to a new `requirements-dev.txt`.
- [ ] **Alert dedupe across restarts**: `SeenTrades` starts empty on every worker
  restart and the first fetch is alert-suppressed (`first_run`), which is fine —
  but trades that occur *while the worker is down* are never recorded. Optional:
  on startup, backfill `seen` from the last 1000 `trade_id`s in `whale_trades` +
  `watched_trades`, then process (not suppress) the first fetch. Low priority.
- [ ] **Telegram/Discord alert for resolutions of watched positions**: when
  `settle_market_trades` settles any `watched_trades` rows, send one summary
  alert per market: "🏁 Market resolved: <title> → <outcome>. Sharp Sam: WIN
  +$412, 0x12ab…: LOSS −$1,200." Reuse the send helpers from `main.py` — move
  `send_telegram_alert` / `send_discord_alert` into a new `notify.py` shared
  module so `resolve_markets.py` doesn't import all of `main.py`.
- [ ] **WebSocket feed** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`)
  instead of polling — big item, keep last; the polling loop works.

---

## Suggested implementation order

1. Phase 1 (markets + resolution + settlement) — everything else builds on it.
2. Phase 3 (position sync + fast watchlist page) — fixes the user-visible lag.
3. Phase 2 (records + realized PnL) — needs Phase 1's `result` columns.
4. Phase 4 (market pages), then Phase 5, then Phase 6 items in any order.

Each phase should land as its own commit(s) with the migration script (if any)
committed alongside the model change, and a README feature-list update.
