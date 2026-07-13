#!/usr/bin/env python3
"""
🏁 Market Resolution Checker

Polls the Gamma API for markets we've recorded trades in but haven't seen
resolve yet, and once one closes, settles every trade recorded against it as
WIN or LOSS (see db.settle_market_trades).

A market is considered resolved when Gamma reports `closed: true` and one of
its outcome prices has settled to (near) 1.0 — the winning outcome is
whichever one that is.

Usage:
    python resolve_markets.py             # one pass over unresolved markets
    python resolve_markets.py --loop 900  # run forever, one pass every 15 min
    python resolve_markets.py --limit 50  # cap markets checked per pass
"""

import argparse
import json
import logging
import time
from datetime import datetime, timezone

import requests
from colorama import init, Fore, Style

import db

init(autoreset=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CHUNK_SIZE = 20
RESOLVED_PRICE_THRESHOLD = 0.99


def _get_json(url: str, params: dict, timeout: int = 20, retries: int = 2):
    """GET with a couple of retries — one flaky response shouldn't stall a
    whole pass over the queue."""
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


def _parse_json_field(value):
    """Gamma returns outcomes/outcomePrices as JSON-encoded strings
    (e.g. '["Yes", "No"]') — sometimes already-parsed lists. Normalize."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _parse_end_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def check_resolution(market: dict) -> str:
    """Return the winning outcome name if this Gamma market object is
    resolved, else ''."""
    if not market.get("closed"):
        return ""
    outcomes = _parse_json_field(market.get("outcomes"))
    prices = _parse_json_field(market.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return ""
    try:
        prices_f = [float(p) for p in prices]
    except (TypeError, ValueError):
        return ""
    best_idx = max(range(len(prices_f)), key=lambda i: prices_f[i])
    if prices_f[best_idx] < RESOLVED_PRICE_THRESHOLD:
        return ""
    return str(outcomes[best_idx])


def run_once(limit: int, pause: float) -> dict:
    """One pass: check every unresolved market we know about, settle the
    ones that just resolved. Returns a summary dict for logging."""
    Session = db.init_db()
    if not Session:
        logger.error("No database available — nothing to check.")
        return {"checked": 0, "resolved": 0}

    with Session() as session:
        markets = db.unresolved_markets(session, limit=limit)
        condition_ids = [m.condition_id for m in markets]

    if not condition_ids:
        logger.info("No unresolved markets in the queue.")
        return {"checked": 0, "resolved": 0}

    logger.info(f"Checking {len(condition_ids)} unresolved market(s)...")

    # Gamma's /markets implicitly filters to closed=false unless told
    # otherwise, so a plain condition_ids query silently misses anything that
    # just resolved. Pass 1 catches still-open markets (and refreshes their
    # metadata); pass 2 explicitly asks closed=true for whatever pass 1 didn't
    # return, to see if those have resolved.
    fetched: dict = {}
    for i in range(0, len(condition_ids), CHUNK_SIZE):
        chunk = condition_ids[i:i + CHUNK_SIZE]
        results = _get_json(f"{GAMMA_API}/markets", {"condition_ids": chunk, "limit": len(chunk)})
        if isinstance(results, list):
            for m in results:
                cid = m.get("conditionId")
                if cid:
                    fetched[cid] = m
        time.sleep(pause)

    missing = [cid for cid in condition_ids if cid not in fetched]
    for i in range(0, len(missing), CHUNK_SIZE):
        chunk = missing[i:i + CHUNK_SIZE]
        results = _get_json(f"{GAMMA_API}/markets",
                            {"condition_ids": chunk, "closed": "true", "limit": len(chunk)})
        if isinstance(results, list):
            for m in results:
                cid = m.get("conditionId")
                if cid:
                    fetched[cid] = m
        time.sleep(pause)

    resolved_count = 0
    now = datetime.now(timezone.utc)
    with Session() as session:
        for cid in condition_ids:
            gm = fetched.get(cid)
            market = session.get(db.Market, cid)
            if market is None:
                continue
            market.last_checked = now

            if gm is None:
                # Gamma didn't return it this round — don't block the queue.
                session.commit()
                continue

            if not market.title:
                market.title = gm.get("question") or gm.get("title") or ""
            if not market.slug:
                market.slug = gm.get("slug") or ""
            if not market.category:
                market.category = gm.get("category") or ""
            if not market.end_date:
                market.end_date = _parse_end_date(gm.get("endDate"))
            session.commit()

            winning_outcome = check_resolution(gm)
            if winning_outcome:
                db.mark_market_resolved(session, cid, winning_outcome)
                counts = db.settle_market_trades(session, cid, winning_outcome)
                resolved_count += 1
                print(f"{Fore.GREEN}🏁 RESOLVED{Style.RESET_ALL} {market.title or cid} "
                      f"→ {Fore.YELLOW}{winning_outcome}{Style.RESET_ALL} "
                      f"— settled {counts['whale_trades']} whale trade(s), "
                      f"{counts['watched_trades']} watched trade(s)")

    logger.info(f"Pass complete — {resolved_count} market(s) resolved this round.")
    return {"checked": len(condition_ids), "resolved": resolved_count}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check unresolved markets for resolution and settle trades")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max unresolved markets to check per pass (default: 100)")
    parser.add_argument("--pause", type=float, default=0.2,
                        help="Seconds to sleep between Gamma API batches (default: 0.2)")
    parser.add_argument("--loop", type=int, default=0,
                        help="Run forever, sleeping this many seconds between passes (0 = run once)")
    args = parser.parse_args()

    print(f"\n{Fore.CYAN}{'═' * 50}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}🏁  Market Resolution Checker{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * 50}{Style.RESET_ALL}\n")

    try:
        if args.loop:
            while True:
                run_once(args.limit, args.pause)
                time.sleep(args.loop)
        else:
            run_once(args.limit, args.pause)
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Interrupted.{Style.RESET_ALL}")
