#!/usr/bin/env python3
"""
🏆 Polymarket Leaderboard Scout

Pulls the top traders off Polymarket's real leaderboard
(data-api.polymarket.com/v1/leaderboard — the same data that backs
polymarket.com/leaderboard), then vets each one against their actual trade
history before promoting them onto the watchlist that main.py / dashboard.py
already track.

The leaderboard itself only gives rank, volume, and PnL — no win rate or
trade count — so "vetting" means, per candidate:
  1. cheap filter: skip anyone under --min-capital (leaderboard `vol`), no
     extra API calls spent on them.
  2. pull /trades for total trade count and every market they've touched.
  3. pull /activity?type=REDEEM — redeeming winning shares is the only
     on-chain signal of "this position won", so distinct redeemed
     conditionIds = wins.
  4. batch those conditionIds against Gamma's /markets?closed=true to see
     which are actually resolved yet (decided markets = wins + losses,
     where losses = decided markets never redeemed).
  5. win_rate = wins / decided, only trusted once --min-decided markets
     have actually settled.

Usage:
    python scout_leaderboard.py                  # scan, report, apply
    python scout_leaderboard.py --dry-run         # scan + report only
    python scout_leaderboard.py --order-by VOL --top 100 --keep 30
"""

import argparse
import logging
import time

import requests
from colorama import init, Fore, Style

import db

init(autoreset=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

TRADE_PAGE_SIZE = 500
TRADE_MAX_PAGES = 6  # caps trade-history fetch at 3000 trades/wallet


def _get_json(url: str, params: dict, timeout: int = 20, retries: int = 2):
    """GET with a couple of retries — leaderboard scouting makes hundreds of
    calls in one run, so a single flaky response shouldn't kill the scan."""
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


def fetch_leaderboard(top_n: int, order_by: str, time_period: str, category: str) -> list:
    """Page /v1/leaderboard (max 50/page) up to top_n entries."""
    entries, offset = [], 0
    while len(entries) < top_n:
        page_limit = min(50, top_n - len(entries))
        batch = _get_json(f"{DATA_API}/v1/leaderboard", {
            "category": category, "timePeriod": time_period,
            "orderBy": order_by, "limit": page_limit, "offset": offset,
        })
        if not batch:
            break
        entries.extend(batch)
        offset += page_limit
    return entries[:top_n]


def _fetch_all(url: str, params: dict, page_size: int = 500, max_pages: int = 6) -> list:
    """Page a data-api list endpoint (limit/offset) until exhausted or capped."""
    items, offset = [], 0
    for _ in range(max_pages):
        batch = _get_json(url, dict(params, limit=page_size, offset=offset))
        if not batch:
            break
        items.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return items


def evaluate_wallet(address: str, pause: float) -> dict:
    """Real trade history for one wallet: trade count + an approximate win rate."""
    trades = _fetch_all(f"{DATA_API}/trades", {"user": address},
                        page_size=TRADE_PAGE_SIZE, max_pages=TRADE_MAX_PAGES)
    trades_capped = len(trades) >= TRADE_PAGE_SIZE * TRADE_MAX_PAGES
    time.sleep(pause)
    redeems = _fetch_all(f"{DATA_API}/activity", {"user": address, "type": "REDEEM"})
    time.sleep(pause)

    traded_markets = {t["conditionId"] for t in trades if t.get("conditionId")}
    won_markets = {r["conditionId"] for r in redeems if r.get("conditionId")}

    decided = set()
    ids = list(traded_markets)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        markets = _get_json(f"{GAMMA_API}/markets",
                            {"condition_ids": chunk, "closed": "true", "limit": len(chunk)})
        decided.update(m["conditionId"] for m in markets if m.get("conditionId"))
        time.sleep(pause)

    wins = won_markets & decided
    losses = decided - won_markets
    decided_n = len(wins) + len(losses)

    return {
        "trade_count": len(trades),
        "trades_capped": trades_capped,
        "markets_traded": len(traded_markets),
        "decided_markets": decided_n,
        "wins": len(wins),
        "win_rate": (len(wins) / decided_n) if decided_n else None,
    }


def scout(args) -> list:
    print(f"\n{Fore.CYAN}{'═' * 60}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}🏆  Polymarket Leaderboard Scout{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * 60}{Style.RESET_ALL}")
    print(f"  Ranking        : {Fore.YELLOW}{args.order_by} · {args.time_period} · {args.category}{Style.RESET_ALL}")
    print(f"  Candidates     : {Fore.YELLOW}top {args.top}{Style.RESET_ALL}")
    print(f"  Keep           : {Fore.YELLOW}top {args.keep} that qualify{Style.RESET_ALL}")
    print(f"  Min capital    : {Fore.YELLOW}${args.min_capital:,.0f} lifetime volume{Style.RESET_ALL}")
    print(f"  Min trades     : {Fore.YELLOW}>{args.min_trades}{Style.RESET_ALL}")
    print(f"  Min win rate   : {Fore.YELLOW}{args.min_win_rate:.0%} (over ≥{args.min_decided} decided markets){Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * 60}{Style.RESET_ALL}\n")

    board = fetch_leaderboard(args.top, args.order_by, args.time_period, args.category)
    print(f"Fetched {len(board)} traders from Polymarket's leaderboard.\n")

    Session = db.init_db()
    already_watched = set()
    if Session:
        with Session() as session:
            already_watched = {a.address for a in db.get_watched_addresses(session)}

    qualified = []
    for i, entry in enumerate(board, 1):
        address = entry.get("proxyWallet", "")
        name = entry.get("userName") or (address[:10] + "…" if address else "?")
        vol = float(entry.get("vol") or 0)
        pnl = float(entry.get("pnl") or 0)

        if not address:
            continue
        if vol < args.min_capital:
            print(f"  [{i:>3}] {name:<20} skip — capital ${vol:,.0f} < ${args.min_capital:,.0f}")
            continue
        if pnl <= 0:
            print(f"  [{i:>3}] {name:<20} skip — lifetime PnL ${pnl:,.0f} <= $0")
            continue

        if address.lower() in already_watched:
            # Already watched — resolve_markets.py has been continuously
            # settling this wallet's trades, so db.wallet_record() gives a
            # live, exact win/loss + realized PnL. Skip the expensive
            # REDEEM-based vetting and just refresh its label from that.
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

        stats = evaluate_wallet(address, args.pause)
        trade_count = stats["trade_count"]
        decided = stats["decided_markets"]
        win_rate = stats["win_rate"]

        reasons = []
        if trade_count <= args.min_trades:
            reasons.append(f"only {trade_count} trades")
        if decided < args.min_decided:
            reasons.append(f"only {decided} decided markets — win rate unreliable")
        elif win_rate < args.min_win_rate:
            reasons.append(f"win rate {win_rate:.0%} < {args.min_win_rate:.0%}")

        if reasons:
            print(f"  [{i:>3}] {name:<20} skip — {', '.join(reasons)}")
            continue

        wr_txt = f"{win_rate:.0%}" if win_rate is not None else "n/a"
        trades_txt = f"{trade_count}+" if stats["trades_capped"] else str(trade_count)
        print(f"  [{i:>3}] {Fore.GREEN}{name:<20} QUALIFIES{Style.RESET_ALL} — "
              f"vol ${vol:,.0f}, pnl ${pnl:,.0f}, {trades_txt} trades, "
              f"{wr_txt} win rate ({decided} decided)")
        qualified.append({
            "rank": entry.get("rank"), "address": address, "userName": name,
            "vol": vol, "pnl": pnl, "already_watched": False, **stats,
        })

    qualified.sort(key=lambda w: w["pnl"], reverse=True)
    kept = qualified[:args.keep]

    print(f"\n{Fore.CYAN}{'─' * 60}{Style.RESET_ALL}")
    print(f"{len(qualified)} of {len(board)} qualified — keeping top {len(kept)} by PnL.\n")
    for rank, w in enumerate(kept, 1):
        wr_txt = f"{w['win_rate']:.0%}" if w["win_rate"] is not None else "n/a"
        if w["trade_count"] is None:
            trades_txt = f"{w['decided_markets']} settled (live)"
        else:
            trades_txt = f"{w['trade_count']}+ trades" if w["trades_capped"] else f"{w['trade_count']} trades"
        print(f"  {rank:>2}. {w['userName']:<20} {w['address']}  "
              f"${w['pnl']:>12,.0f} pnl  {wr_txt:>5} WR  {trades_txt}")

    if args.dry_run:
        print(f"\n{Fore.YELLOW}Dry run — nothing written to the watchlist.{Style.RESET_ALL}")
        return kept

    if not Session:
        print(f"\n{Fore.RED}No database available — can't write to the watchlist.{Style.RESET_ALL}")
        return kept

    with Session() as session:
        existing = {a.address for a in db.get_watched_addresses(session)}
        added = 0
        skipped_cap = 0
        for w in kept:
            # Cap only blocks *new* additions — a wallet already on the
            # watchlist still gets its label refreshed with the latest rank/WR.
            if w["address"] not in existing and len(existing) >= args.max_keep_total:
                skipped_cap += 1
                continue
            wr_txt = f"{w['win_rate']:.0%}" if w["win_rate"] is not None else "n/a"
            if w["already_watched"]:
                label = f"🏆 LB#{w['rank']} {w['userName']} · {wr_txt} WR (live) · {w['decided_markets']} settled"
            else:
                label = f"🏆 LB#{w['rank']} {w['userName']} · {wr_txt} WR · {w['trade_count']} trades"
            db.add_watched_address(session, w["address"], label[:128])
            existing.add(w["address"])
            added += 1

    print(f"\n{Fore.GREEN}Added/updated {added} wallet(s) on the watchlist.{Style.RESET_ALL}")
    if skipped_cap:
        print(f"{Fore.YELLOW}Skipped {skipped_cap} qualifying wallet(s) — watchlist already at "
              f"the --max-keep-total cap ({args.max_keep_total}).{Style.RESET_ALL}")
    return kept


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scout Polymarket's leaderboard for wallets worth watching")
    parser.add_argument("--top", type=int, default=300, help="How many leaderboard entries to pull (default: 300)")
    parser.add_argument("--keep", type=int, default=300,
                        help="Per-run cap on qualifiers to keep (default: 300, i.e. keep every "
                             "qualifier). --max-keep-total, not --keep, is the standing cap on "
                             "watchlist size across runs.")
    parser.add_argument("--order-by", choices=["PNL", "VOL"], default="PNL",
                        help="Leaderboard ranking metric (default: PNL)")
    parser.add_argument("--time-period", choices=["DAY", "WEEK", "MONTH", "ALL"], default="ALL",
                        help="Leaderboard window (default: ALL)")
    parser.add_argument("--category", default="OVERALL",
                        choices=["OVERALL", "POLITICS", "SPORTS", "ESPORTS", "CRYPTO", "CULTURE",
                                 "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE"],
                        help="Leaderboard category (default: OVERALL)")
    parser.add_argument("--min-capital", type=float, default=50_000,
                        help="Minimum lifetime volume in USD (default: 50000)")
    parser.add_argument("--min-trades", type=int, default=10,
                        help="Minimum trade count, exclusive (default: 10 — i.e. more than 10)")
    parser.add_argument("--min-win-rate", type=float, default=0.5,
                        help="Minimum win rate on decided markets, 0-1 (default: 0.5)")
    parser.add_argument("--min-decided", type=int, default=5,
                        help="Minimum resolved markets before trusting win rate (default: 5)")
    parser.add_argument("--pause", type=float, default=0.15,
                        help="Seconds to sleep between API calls (default: 0.15)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report only — don't write to the watchlist")
    parser.add_argument("--max-keep-total", type=int, default=100,
                        help="Stop adding new wallets once the watchlist reaches this size "
                             "(default: 100) — prevents unbounded growth on repeated runs. "
                             "Wallets already on the watchlist still get their label refreshed.")
    args = parser.parse_args()

    try:
        scout(args)
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Interrupted.{Style.RESET_ALL}")
