#!/usr/bin/env python3
"""
🔄 Wallet Position Sync

Pulls each watched wallet's open positions from the Polymarket data-api and
writes them into the wallet_positions table, so the dashboard's /watchlist
page can read live-looking P&L straight from the DB instead of making one
HTTP call per watched wallet on every page load (that's what made the page
lag as the watchlist grew).

Also drops a PnL snapshot (wallet_pnl_snapshots) per wallet per pass, gated
to at most one every 6 hours, so /wallet/<address> can eventually chart
whether a whale's edge is trending up or down.

Usage:
    python sync_positions.py             # one pass: sync all watched addresses
    python sync_positions.py --loop 300  # run forever, one pass every 5 min
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


def fetch_positions(address: str, limit: int = 100, retries: int = 2) -> list:
    """GET a wallet's open positions, with a couple of retries."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(f"{DATA_API}/positions",
                                params={"user": address, "limit": limit}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            if attempt == retries:
                logger.debug(f"positions fetch failed for {address}: {e}")
                return None  # None = fetch failed, distinct from "no positions"
            time.sleep(0.5 * (attempt + 1))


def run_once(pause: float) -> dict:
    """One pass over every watched address. Returns a summary dict."""
    Session = db.init_db()
    if not Session:
        logger.error("No database available — nothing to sync.")
        return {"synced": 0, "failed": 0}

    with Session() as session:
        addresses = [a.address for a in db.get_watched_addresses(session)]

    if not addresses:
        logger.info("No watched addresses to sync.")
        return {"synced": 0, "failed": 0}

    logger.info(f"Syncing positions for {len(addresses)} watched wallet(s)...")

    synced = failed = 0
    for address in addresses:
        positions = fetch_positions(address)
        if positions is None:
            failed += 1
            time.sleep(pause)
            continue

        open_value = sum(p.get("currentValue") or 0 for p in positions)
        unrealized = sum(p.get("cashPnl") or 0 for p in positions)

        with Session() as session:
            db.replace_wallet_positions(session, address, positions)
            record = db.wallet_record(session, address)
            db.record_pnl_snapshot(
                session, address,
                open_value=open_value, unrealized=unrealized,
                realized=record["realized_pnl"],
                wins=record["wins"], losses=record["losses"],
            )
        synced += 1
        print(f"  {Fore.GREEN}✓{Style.RESET_ALL} {address}  "
              f"{len(positions)} position(s), ${open_value:,.0f} open value")
        time.sleep(pause)

    logger.info(f"Pass complete — {synced} synced, {failed} failed.")
    return {"synced": synced, "failed": failed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync watched wallets' open positions into the database")
    parser.add_argument("--pause", type=float, default=0.3,
                        help="Seconds to sleep between wallets (default: 0.3)")
    parser.add_argument("--loop", type=int, default=0,
                        help="Run forever, sleeping this many seconds between passes (0 = run once)")
    args = parser.parse_args()

    print(f"\n{Fore.CYAN}{'═' * 50}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}🔄  Wallet Position Sync{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * 50}{Style.RESET_ALL}\n")

    try:
        if args.loop:
            while True:
                run_once(args.pause)
                time.sleep(args.loop)
        else:
            run_once(args.pause)
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Interrupted.{Style.RESET_ALL}")
