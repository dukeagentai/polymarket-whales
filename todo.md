# TODO — Upcoming features

**Status:** the previous roadmap (markets/resolution tracking, realized PnL,
position sync, market pages, tracked/unknown wallet flow, JSON API, pagination,
retention, tests) is fully implemented — see git history and the README's
Features list. This file now tracks what's next. Each item below is detailed
enough for a lower model to implement without re-deriving the design.

Follow the existing code style: standalone scripts shaped like
`scout_leaderboard.py` / `resolve_markets.py` (argparse, `colorama` output,
`db.init_db()`), SQLAlchemy models/helpers in `db.py`, no new abstractions
beyond what's asked.

---

## 1. Expand `scout_leaderboard.py` to the top 300 (prod usage)

**Why:** wider candidate pool = better odds of finding genuinely sharp
traders instead of just the highest-volume ones. Running this weekly against
300 candidates means most of the pool will already be on the watchlist after
the first run or two — re-vetting all of them every time is wasted API calls
we don't need, because we now have better data for anyone already watched
(see below).

### 1.1 Bump the default pool size

In `scout_leaderboard.py`'s argparse section:
- `--top` default: `100` → `300`.
- `--keep` default: `30` → `300` (i.e. keep every qualifier by default —
  `--max-keep-total`, already implemented, is the real ceiling on watchlist
  growth, not `--keep`). Update the help text on both to explain the
  relationship: "keep is a per-run cap on qualifiers; max-keep-total is the
  standing cap on watchlist size across runs."

### 1.2 Skip re-vetting wallets already on the watchlist

Right now `scout()` calls `evaluate_wallet()` (paginated `/trades` +
`/activity?type=REDEEM` + a `/markets?closed=true` batch call) for **every**
candidate that passes the cheap capital/PnL filter — including wallets we
already watch and already have better data for, since
`resolve_markets.py` has been continuously settling their trades and
`db.wallet_record()` gives a live, exact win/loss + realized PnL (not the
REDEEM-based approximation `evaluate_wallet()` uses). Re-vetting them is
pure waste once the watchlist is populated.

Change in `scout()` (`scout_leaderboard.py`):

1. At the top of `scout()`, before the loop, open a DB session and fetch
   `existing = {a.address for a in db.get_watched_addresses(session)}` —
   guard with `Session = db.init_db()`; if unavailable, fall back to the
   current behavior (vet everyone, since there's nothing to compare against).
2. Inside the per-candidate loop, right after the capital/PnL cheap filter
   and before calling `evaluate_wallet()`:
   ```python
   if address in existing:
       # Already watched — we have live, continuously-updated stats for
       # this wallet from resolve_markets.py; skip the expensive REDEEM-based
       # vetting and just refresh its label from the leaderboard's cheap
       # fields plus our own db.wallet_record() (real win rate, not approximated).
       with Session() as session:
           record = db.wallet_record(session, address)
       wr_txt = f"{record['win_rate']:.0%}" if record["win_rate"] is not None else "n/a"
       print(f"  [{i:>3}] {name:<20} {Fore.CYAN}ALREADY WATCHED{Style.RESET_ALL} — "
             f"skipping re-vet (live record: {record['wins']}W-{record['losses']}L, {wr_txt} WR)")
       qualified.append({
           "rank": entry.get("rank"), "address": address, "userName": name,
           "vol": vol, "pnl": pnl, "trade_count": None, "trades_capped": False,
           "decided_markets": record["wins"] + record["losses"],
           "wins": record["wins"], "win_rate": record["win_rate"],
           "already_watched": True,
       })
       continue
   ```
   Then the existing `evaluate_wallet()` path runs unchanged for everyone
   else, tagging its results with `"already_watched": False`.
3. Sorting/keeping logic (`qualified.sort(...)`, `kept = qualified[:args.keep]`)
   stays the same — already-watched wallets just skip straight to
   "qualified" without the vetting cost, and their PnL for sorting purposes
   is the leaderboard's `pnl` field (same as everyone else uses for sorting
   today — no change needed there).
4. In the final DB-write loop, the label format needs a branch: for
   `already_watched` entries, build the label from `db.wallet_record()`
   data instead of `evaluate_wallet()`'s `win_rate`/`trade_count` (which are
   `None` for these). Something like:
   `f"🏆 LB#{rank} {name} · {live_win_rate} WR (live) · {wins+losses} settled"`.

### 1.3 Verification
- Run `python scout_leaderboard.py --dry-run --top 300` against prod (or a
  copy of the DB) and confirm: (a) it completes noticeably faster on a
  second run than the first once most candidates are watched, (b) the
  "ALREADY WATCHED" skip path prints for wallets known to be on the
  watchlist, (c) newly-seen candidates still go through full vetting.
- Confirm `--max-keep-total` (existing, default 100) still caps how many
  brand-new wallets get added even with `--top 300 --keep 300`.

---

## 2. `category_scout.py` — per-category "who's actually good at this" analysis

**Why:** the existing leaderboard scout ranks wallets globally. This answers
a different question: *within one contract category (weather, politics,
sports, ...), who has the best real track record over a recent window?*
That surfaces specialists the global leaderboard would never highlight (a
wallet that's mediocre overall but crushes weather markets specifically).

**Confirmed via live API testing this session** (do this yourself again if
building later — Polymarket's data shapes drift):
- `GET gamma-api.polymarket.com/events?tag_slug=<category>&closed=true&order=endDate&ascending=false&limit=&offset=`
  returns closed events for a category, each with a nested `markets: [...]`
  list containing `conditionId`, `outcomes`, `outcomePrices`, `endDate`,
  `closed`. Tested with `tag_slug=weather` — returned genuine weather
  events (e.g. "Highest temperature in Paris on July 13?").
  **`tag_slug` is the correct param — `tag` (no `_slug`) silently ignores
  the filter and returns unrelated events.** This mirrors the
  `resolve_markets.py` `closed=true` quirk already documented in git
  history — verify empirically, don't assume.
- `GET data-api.polymarket.com/trades?market=<condition_id>&limit=&offset=`
  returns every trade in that specific market (not filtered by wallet) —
  confirmed fields: `proxyWallet`, `side` (BUY/SELL), `outcome`, `size`,
  `price`, `timestamp`. This is the key building block: iterate markets, not
  wallets, to discover *every* participant in a category, not just wallets
  already in our DB.
- Gamma's tag vocabulary (`tag_slug`) is a **different vocabulary** from
  `scout_leaderboard.py`'s `--category` choices (which are the
  `/v1/leaderboard` API's own category enum: `OVERALL`, `POLITICS`, `SPORTS`,
  `WEATHER`, etc., uppercase). Don't assume they're interchangeable — verify
  the `tag_slug` for whatever category the user wants via
  `gamma-api.polymarket.com/tags` (paginated) or empirically, same as
  `weather` was confirmed here.

### 2.1 CLI shape

```bash
python category_scout.py --category weather                    # trailing 7 days, print top 100
python category_scout.py --category weather --days 14           # wider window
python category_scout.py --category weather --top 200           # rank more than 100
python category_scout.py --category weather --export weather.csv
python category_scout.py --category weather --watch-top 20      # also add top 20 to the watchlist
```

- `--category` (required): a Gamma `tag_slug` string, e.g. `weather`,
  `politics`, `sports`, `crypto`, `elections` — passed straight through as
  the `tag_slug` param, no translation table needed (keep it simple; if a
  slug doesn't exist Gamma just returns zero events, which the script should
  report plainly rather than erroring).
- `--days` (default 7): only include markets whose `endDate` falls within
  the trailing N days (i.e. "last week's contracts").
- `--top` (default 100): how many ranked wallets to print/export.
- `--max-markets` (default 300): safety cap on how many resolved markets to
  pull per run — some categories (Sports) could have thousands of
  micro-markets in a week; cap it and log a warning if the cap is hit.
- `--pause` (default 0.15s): sleep between paginated API calls, same
  convention as `scout_leaderboard.py`.
- `--export PATH.csv|.json`: dump the full ranked list (reuse `main.py`'s
  `export_trade`-style CSV/JSON append pattern, or write a small dedicated
  writer — this is a one-shot full dump, not an append-per-trade log, so a
  simple `csv.DictWriter` / `json.dump` over the whole ranked list is
  simpler than reusing `export_trade` as-is).
- `--watch-top N` (optional, default 0 = off): add the top N ranked wallets
  to the watchlist via `db.add_watched_address`, labeled with the category
  and rank, e.g. `"🌦️ Weather #3 · 82% WR · +$12,400 (7d)"`. Respect the
  same `--max-keep-total`-style cap as `scout_leaderboard.py` (reuse the
  identical guard: skip *new* additions once the watchlist is at the cap,
  still refresh labels for wallets already watched).

### 2.2 Algorithm

1. **Fetch resolved events in the category over the window.**
   Page `events?tag_slug=<category>&closed=true&order=endDate&ascending=false`
   (limit/offset), stop paging once a page's oldest `endDate` is older than
   `now - days` (results are ordered newest-first, so this is a clean early
   exit — don't keep paging past the window).
2. **Flatten to markets.** Each event can have multiple nested `markets`
   (e.g. a multi-outcome election) — collect every market's `conditionId`,
   `outcomes`, `outcomePrices`, capped at `--max-markets` total.
3. **Determine the winning outcome per market.** Reuse
   `resolve_markets.check_resolution(market_dict)` — `import` it directly
   from `resolve_markets.py` rather than re-implementing the
   JSON-string-parsing + threshold logic. Skip any market where this
   returns `""` (shouldn't happen since we filtered `closed=true`, but a
   market can be `closed` without fully-settled `outcomePrices` in edge
   cases — skip rather than crash).
4. **Pull every trade in each market.** For each market's `conditionId`,
   page `data-api.polymarket.com/trades?market=<conditionId>&limit=500&offset=`
   (same `TRADE_PAGE_SIZE`/`TRADE_MAX_PAGES` capping convention as
   `scout_leaderboard.py`'s `evaluate_wallet()` — reuse those constants or
   define local equivalents) until exhausted or capped.
5. **Aggregate per wallet across every market in the category-week.** For
   each trade: `wallet = trade["proxyWallet"].lower()`,
   `amount_usd = float(trade["size"]) * float(trade["price"])` (same
   formula as `main.py`'s `parse_trade_usd_size` — reuse it or duplicate the
   one-liner), `is_sell = trade["side"].upper() == "SELL"`,
   `is_winner = trade["outcome"].strip().upper() == winning_outcome.strip().upper()`.
   Apply the **exact same WIN/LOSS/PnL rules as
   `db.settle_market_trades`/`db.wallet_record`**: BUY+winner=WIN,
   BUY+loser=LOSS, SELL+winner=LOSS, SELL+loser=WIN; realized PnL only
   accrues on BUY trades (`shares = amount_usd/price; pnl = shares -
   amount_usd` for WIN, `pnl = -amount_usd` for LOSS); SELL trades count
   toward win/loss but not PnL (no cost basis, identical caveat to
   `wallet_record`'s docstring). Accumulate per wallet: `trades`, `volume`,
   `wins`, `losses`, `realized_pnl`.
6. **Rank and output.** Sort by `realized_pnl` descending, take `--top`.
   Print a colorama table (rank, address, trades, volume, W-L, win rate,
   realized PnL) matching `scout_leaderboard.py`'s print style. If
   `--export` is set, dump the full ranked list. If `--watch-top N` is set,
   add the top N to the watchlist (per 2.1).

### 2.3 Edge cases / notes for the implementer
- A wallet trading in 50 different weather markets in the window should
  show one aggregated row, not 50 — aggregate by `proxyWallet`, not by
  trade.
- Don't dedupe trades against our own `whale_trades`/`watched_trades`
  tables — this script is deliberately independent of what we've already
  recorded, since the whole point is finding wallets we *don't* already
  track. It reads live from the API only; it doesn't touch
  `whale_trades`/`watched_trades` at all (only optionally writes to
  `watched_addresses` via `--watch-top`).
- Rate limits: a category with thousands of resolved markets in a week
  (Sports is the obvious risk) could mean thousands of paginated `/trades`
  calls. `--max-markets` is the safety valve — log clearly how many markets
  were skipped due to the cap so the user knows the ranking is partial.
- No new DB tables needed. This script is stateless analysis + an optional
  watchlist write; it doesn't need its own persistence.

### 2.4 Verification
- Run `python category_scout.py --category weather --days 7` against the
  live API (no `--watch-top`, no `--export`) and manually spot-check: does
  the #1 ranked wallet's PnL look plausible against a couple of its trades
  pulled directly from `data-api.polymarket.com/trades?user=<address>`?
- Run with `--max-markets 5` on a busier category (e.g. `sports`) to confirm
  the cap and warning message work without hammering the API during
  development.
- Run with `--watch-top 3 --dry-run`-equivalent (add a `--dry-run` flag
  mirroring `scout_leaderboard.py`'s, so this can be tested without
  mutating the real watchlist) and confirm the labels look right before
  wiring up the real `add_watched_address` calls.

---

## 3. Other ideas worth considering (lighter detail — flesh out if picked up)

Ordered roughly by how much they leverage what's already built vs. how much
new surface area they'd add.

- **Win-rate-weighted consensus alerts.** The existing smart-money
  consensus alert (`db.market_side_whales` in `main.py`) counts *distinct
  wallets* on one side of a market — it doesn't know if those wallets are
  any good. Now that `db.wallet_record()` gives a real win rate per wallet,
  the consensus check could weight by (or require a minimum) average win
  rate among the converging wallets, so "3 random whales agree" and "3
  wallets with 70%+ win rates agree" aren't treated the same. Lowest-effort,
  highest-leverage item on this list — it's a filter change to an existing
  alert, not a new subsystem.
- **Category leaderboard page in the dashboard.** Once `category_scout.py`
  exists, its output is only as useful as the last time someone ran it.
  Store results in a new small table (e.g. `category_leaders(category,
  address, rank, wins, losses, realized_pnl, computed_at)`) written each
  time the script runs, and add a `/leaderboard?category=weather`-style view
  reading from it — turns a CLI report into a living dashboard page.
- **Wallet PnL trend chart.** `wallet_pnl_snapshots` (built in the previous
  round, written by `sync_positions.py`) is already collecting the data —
  nothing on `/wallet/<address>` renders it yet. A simple table or sparkline
  of `db.pnl_snapshot_history()` closes this loop with no new backend work.
- **Slippage-aware alerting.** Surface whether a whale's trade meaningfully
  moved the price (compare pre-trade vs. post-trade price on the same
  market) rather than alerting on dollar size alone — catches large trades
  against deep books that don't actually move anything, and small trades
  against thin books that move a lot.
- **Multi-wallet correlation across time.** Detect clusters of wallets that
  repeatedly land on the same side of the same markets across *many*
  markets, not just one (the existing `market_convergence` is single-market
  only) — could indicate coordinated wallets or one person operating
  several addresses.
- **Rate-limit/backoff tuning for the scout scripts.** Both
  `scout_leaderboard.py` and (once built) `category_scout.py` use a flat
  `--pause` between calls. A real backoff (exponential on 429/5xx, shorter
  pause on success) would let `--top 300` runs go faster without risking a
  ban.

Not carried forward from the old list (still valid ideas, just no new
information to add beyond what was already written): Streamlit analytics
mode, WebSocket feed instead of polling, backtest mode. Revisit those if
they become priorities — the earlier reasoning for deferring them
(polling works fine, WebSocket is a bigger architectural change) still
holds.
