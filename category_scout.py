#!/usr/bin/env python3
"""
🌦️  Polymarket Category Scout

The leaderboard scout (scout_leaderboard.py) ranks wallets globally. This
answers a different question: *within one contract category (weather,
politics, sports, ...), who has the best real track record over a recent
window?* That surfaces specialists the global leaderboard would never
highlight — a wallet that's mediocre overall but crushes weather markets
specifically.

Unlike scout_leaderboard.py, this reads live from the API only. It doesn't
touch whale_trades/watched_trades — the whole point is finding wallets we
don't already track — and it doesn't dedupe against what we've already
recorded. The only optional DB write is to watched_addresses via --watch-top.

Usage:
    python category_scout.py --category weather
    python category_scout.py --category weather --days 14
    python category_scout.py --category weather --top 200
    python category_scout.py --category weather --export weather.csv
    python category_scout.py --category weather --watch-top 20
"""

import argparse
import csv
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import requests
from colorama import init, Fore, Style

import db
from resolve_markets import check_resolution

init(autoreset=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

EVENT_PAGE_SIZE = 100
TRADE_PAGE_SIZE = 500
TRADE_MAX_PAGES = 6  # caps trade fetch at 3000 trades/market, same convention as scout_leaderboard.py


def _get_json(url: str, params: dict, timeout: int = 20, retries: int = 2):
    """GET with a couple of retries — a category scan makes many calls in one run."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt == retries:
                logger.debug(f"GET {url} failed after {retries + 1} tries: {e}")
                return []
            time.sleep(0.5 * (attempt + 1))


def fetch_resolved_markets(category: str, days: int, max_markets: int, pause: float) -> tuple:
    """Page closed events for a tag_slug, newest first, stopping once the
    window is exhausted. Flattens each event's nested markets. Returns
    (markets, hit_cap) where each market dict carries conditionId/outcomes/
    outcomePrices/endDate.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    markets, offset, hit_cap = [], 0, False

    while True:
        batch = _get_json(f"{GAMMA_API}/events", {
            "tag_slug": category, "closed": "true",
            "order": "endDate", "ascending": "false",
            "limit": EVENT_PAGE_SIZE, "offset": offset,
        })
        if not batch:
            break

        page_oldest = None
        for event in batch:
            for market in event.get("markets", []):
                end_date = market.get("endDate")
                if not end_date:
                    continue
                try:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if page_oldest is None or end_dt < page_oldest:
                    page_oldest = end_dt
                if end_dt < cutoff:
                    continue
                if len(markets) >= max_markets:
                    hit_cap = True
                    continue
                markets.append(market)

        if hit_cap:
            break
        if page_oldest is not None and page_oldest < cutoff:
            break  # results are newest-first — the window is exhausted
        if len(batch) < EVENT_PAGE_SIZE:
            break
        offset += EVENT_PAGE_SIZE
        time.sleep(pause)

    return markets, hit_cap


def _fetch_all_trades(condition_id: str, pause: float) -> list:
    """Page every trade in one market (not filtered by wallet)."""
    items, offset = [], 0
    for _ in range(TRADE_MAX_PAGES):
        batch = _get_json(f"{DATA_API}/trades",
                          {"market": condition_id, "limit": TRADE_PAGE_SIZE, "offset": offset})
        if not batch:
            break
        items.extend(batch)
        if len(batch) < TRADE_PAGE_SIZE:
            break
        offset += TRADE_PAGE_SIZE
        time.sleep(pause)
    return items


def aggregate_wallets(markets: list, pause: float) -> dict:
    """Per-wallet trades/volume/wins/losses/realized_pnl across every
    resolved market, using the same WIN/LOSS/PnL rules as
    db.settle_market_trades / db.wallet_record."""
    wallets: dict = {}

    for market in markets:
        condition_id = market.get("conditionId")
        if not condition_id:
            continue
        winning_outcome = check_resolution(market)
        if not winning_outcome:
            continue
        winner = winning_outcome.strip().upper()

        trades = _fetch_all_trades(condition_id, pause)
        for t in trades:
            wallet = (t.get("proxyWallet") or "").lower()
            if not wallet:
                continue
            try:
                size = float(t.get("size", 0))
                price = float(t.get("price", 0))
            except (TypeError, ValueError):
                continue
            amount_usd = size * price
            is_sell = (t.get("side") or "").strip().upper() == "SELL"
            is_winner = (t.get("outcome") or "").strip().upper() == winner

            row = wallets.setdefault(wallet, {"trades": 0, "volume": 0.0, "wins": 0,
                                              "losses": 0, "realized_pnl": 0.0})
            row["trades"] += 1
            row["volume"] += amount_usd
            won = is_winner if not is_sell else not is_winner
            if won:
                row["wins"] += 1
                if not is_sell:
                    shares = (amount_usd / price) if price else 0.0
                    row["realized_pnl"] += shares - amount_usd
            else:
                row["losses"] += 1
                if not is_sell:
                    row["realized_pnl"] -= amount_usd

    return wallets


def rank_wallets(wallets: dict, top_n: int) -> list:
    ranked = [
        {
            "address": address, "trades": row["trades"], "volume": row["volume"],
            "wins": row["wins"], "losses": row["losses"],
            "win_rate": (row["wins"] / (row["wins"] + row["losses"])) if (row["wins"] + row["losses"]) else None,
            "realized_pnl": row["realized_pnl"],
        }
        for address, row in wallets.items()
    ]
    ranked.sort(key=lambda w: w["realized_pnl"], reverse=True)
    return ranked[:top_n]


def export_ranked(ranked: list, path: str) -> None:
    if path.endswith(".json"):
        with open(path, "w") as f:
            json.dump(ranked, f, indent=2)
    else:
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["address", "trades", "volume", "wins",
                                                    "losses", "win_rate", "realized_pnl"])
            writer.writeheader()
            writer.writerows(ranked)


def scout(args) -> list:
    print(f"\n{Fore.CYAN}{'═' * 60}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}🌦️  Polymarket Category Scout{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * 60}{Style.RESET_ALL}")
    print(f"  Category       : {Fore.YELLOW}{args.category}{Style.RESET_ALL}")
    print(f"  Window         : {Fore.YELLOW}trailing {args.days} day(s){Style.RESET_ALL}")
    print(f"  Max markets    : {Fore.YELLOW}{args.max_markets}{Style.RESET_ALL}")
    print(f"  Top            : {Fore.YELLOW}{args.top}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * 60}{Style.RESET_ALL}\n")

    markets, hit_cap = fetch_resolved_markets(args.category, args.days, args.max_markets, args.pause)
    print(f"Found {len(markets)} resolved market(s) for tag_slug={args.category!r} "
          f"in the last {args.days} day(s).")
    if hit_cap:
        print(f"{Fore.YELLOW}Hit --max-markets cap ({args.max_markets}) — ranking is partial; "
              f"raise --max-markets for a fuller picture.{Style.RESET_ALL}")
    if not markets:
        print(f"{Fore.YELLOW}Nothing to rank.{Style.RESET_ALL}")
        return []

    wallets = aggregate_wallets(markets, args.pause)
    print(f"Aggregated {len(wallets)} distinct wallet(s) across {len(markets)} market(s).\n")

    ranked = rank_wallets(wallets, args.top)

    print(f"{Fore.CYAN}{'─' * 60}{Style.RESET_ALL}")
    print(f"Top {len(ranked)} by realized PnL:\n")
    for rank, w in enumerate(ranked, 1):
        wr_txt = f"{w['win_rate']:.0%}" if w["win_rate"] is not None else "n/a"
        print(f"  {rank:>3}. {w['address']}  ${w['realized_pnl']:>12,.0f} pnl  "
              f"{wr_txt:>5} WR  {w['wins']:>3}W-{w['losses']:<3}L  "
              f"{w['trades']:>4} trades  ${w['volume']:>12,.0f} vol")

    if args.export:
        export_ranked(ranked, args.export)
        print(f"\n{Fore.GREEN}Exported {len(ranked)} row(s) to {args.export}{Style.RESET_ALL}")

    if args.watch_top > 0:
        watch_candidates = ranked[:args.watch_top]
        if args.dry_run:
            print(f"\n{Fore.YELLOW}Dry run — would add/update {len(watch_candidates)} "
                  f"wallet(s) on the watchlist:{Style.RESET_ALL}")
            for rank, w in enumerate(watch_candidates, 1):
                wr_txt = f"{w['win_rate']:.0%}" if w["win_rate"] is not None else "n/a"
                label = f"🌦️ {args.category.title()} #{rank} · {wr_txt} WR · ${w['realized_pnl']:,.0f} ({args.days}d)"
                print(f"  {rank:>2}. {w['address']}  {label}")
        else:
            Session = db.init_db()
            if not Session:
                print(f"\n{Fore.RED}No database available — can't write to the watchlist.{Style.RESET_ALL}")
            else:
                with Session() as session:
                    existing = {a.address for a in db.get_watched_addresses(session)}
                    added = 0
                    skipped_cap = 0
                    for rank, w in enumerate(watch_candidates, 1):
                        if w["address"] not in existing and len(existing) >= args.max_keep_total:
                            skipped_cap += 1
                            continue
                        wr_txt = f"{w['win_rate']:.0%}" if w["win_rate"] is not None else "n/a"
                        label = f"🌦️ {args.category.title()} #{rank} · {wr_txt} WR · ${w['realized_pnl']:,.0f} ({args.days}d)"
                        db.add_watched_address(session, w["address"], label[:128])
                        existing.add(w["address"])
                        added += 1
                print(f"\n{Fore.GREEN}Added/updated {added} wallet(s) on the watchlist.{Style.RESET_ALL}")
                if skipped_cap:
                    print(f"{Fore.YELLOW}Skipped {skipped_cap} wallet(s) — watchlist already at "
                          f"the --max-keep-total cap ({args.max_keep_total}).{Style.RESET_ALL}")

    return ranked


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scout a Polymarket category for the best real track records")
    parser.add_argument("--category", required=True,
                        help="Gamma tag_slug, e.g. weather, politics, sports, crypto, elections")
    parser.add_argument("--days", type=int, default=7,
                        help="Only include markets whose endDate falls in the trailing N days (default: 7)")
    parser.add_argument("--top", type=int, default=100, help="How many ranked wallets to print/export (default: 100)")
    parser.add_argument("--max-markets", type=int, default=300,
                        help="Safety cap on resolved markets pulled per run (default: 300)")
    parser.add_argument("--pause", type=float, default=0.15,
                        help="Seconds to sleep between API calls (default: 0.15)")
    parser.add_argument("--export", default="", help="Export the full ranked list to a .csv or .json path")
    parser.add_argument("--watch-top", type=int, default=0,
                        help="Add the top N ranked wallets to the watchlist (default: 0 = off)")
    parser.add_argument("--max-keep-total", type=int, default=100,
                        help="Stop adding new wallets once the watchlist reaches this size (default: 100)")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --watch-top, report what would be added without writing to the watchlist")
    args = parser.parse_args()

    try:
        scout(args)
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Interrupted.{Style.RESET_ALL}")
