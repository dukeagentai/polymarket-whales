#!/usr/bin/env python3
"""
🐋 Polymarket Whale Tracker
Monitors Polymarket for large trades and sends Telegram alerts.
"""

import os
import sys
import time
import logging
import requests
import yaml
import csv
import json
import argparse
from collections import deque
from datetime import datetime, timezone
from dotenv import load_dotenv
from colorama import init, Fore, Style

import db

# Initialize colorama for cross-platform colored output
init(autoreset=True)

# Load environment variables from .env file if present
load_dotenv()

# ─────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────
def load_config(path: str = "config.yaml") -> dict:
    """Load configuration from YAML file, with env var overrides."""
    config = {
        "min_trade_size": 500,
        "check_interval": 30,
        "telegram": {
            "bot_token": "",
            "chat_id": "",
        },
        "discord": {
            "webhook_url": "",
        },
        "polymarket": {
            # Public Data API — no auth needed, includes wallet addresses.
            # The CLOB /trades endpoint requires L2 auth, so it's no longer the default.
            "api_url": "https://data-api.polymarket.com",
        },
        "filters": {
            "markets": [],      # keywords or condition IDs — empty = all markets
            "categories": [],   # e.g. Politics, Crypto, Sports — empty = all
        },
        "alert_cooldown": 0,    # seconds between alerts per market (0 = off)
        "wallets": {
            "recurring_threshold": 3,  # trades before a wallet is tagged recurring
            "tags": {},                # address: custom tag overrides
        },
        "watchlist": {
            "min_trade_size": 0,  # alert on any size for watched wallets
            "addresses": {},      # address: label — also manageable in the dashboard
        },
        "consensus": {
            # Alert when several distinct whales hit the same side of a market
            "enabled": True,
            "min_wallets": 3,
            "window_minutes": 60,
        },
        "accumulation": {
            # Catch wallets splitting orders to stay under min_trade_size
            "enabled": True,
            "window_minutes": 60,
            "threshold": 0,  # 0 = use min_trade_size
        },
    }

    if os.path.exists(path):
        with open(path, "r") as f:
            loaded = yaml.safe_load(f)
            if loaded:
                # Deep merge
                for key, val in loaded.items():
                    if isinstance(val, dict) and key in config:
                        config[key].update(val)
                    else:
                        config[key] = val

    # Environment variable overrides (takes priority over YAML)
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        config["telegram"]["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN")
    if os.getenv("TELEGRAM_CHAT_ID"):
        config["telegram"]["chat_id"] = os.getenv("TELEGRAM_CHAT_ID")
    if os.getenv("DISCORD_WEBHOOK_URL"):
        config["discord"]["webhook_url"] = os.getenv("DISCORD_WEBHOOK_URL")
    if os.getenv("MIN_TRADE_SIZE"):
        config["min_trade_size"] = float(os.getenv("MIN_TRADE_SIZE"))
    if os.getenv("MARKET_FILTER"):
        config["filters"]["markets"] = [
            s.strip() for s in os.getenv("MARKET_FILTER").split(",") if s.strip()
        ]
    if os.getenv("CATEGORY_FILTER"):
        config["filters"]["categories"] = [
            s.strip() for s in os.getenv("CATEGORY_FILTER").split(",") if s.strip()
        ]
    if os.getenv("ALERT_COOLDOWN"):
        config["alert_cooldown"] = int(os.getenv("ALERT_COOLDOWN"))

    return config


# ─────────────────────────────────────────────
# Polymarket API
# ─────────────────────────────────────────────
def fetch_recent_trades(api_url: str, limit: int = 100, offset: int = 0) -> list:
    """Fetch one page of recent trades (newest first)."""
    url = f"{api_url}/trades"
    params = {"limit": limit}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # The CLOB API returns {"data": [...], "next_cursor": ...}
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        # Some endpoints return a list directly
        if isinstance(data, list):
            return data
        return []
    except requests.exceptions.ConnectionError:
        logger.warning("⚠️  Network error: could not reach Polymarket API.")
        return []
    except requests.exceptions.Timeout:
        logger.warning("⚠️  Request timed out fetching trades.")
        return []
    except requests.exceptions.HTTPError as e:
        logger.warning(f"⚠️  HTTP error fetching trades: {e}")
        return []
    except Exception as e:
        logger.warning(f"⚠️  Unexpected error fetching trades: {e}")
        return []


def fetch_new_trades(api_url: str, seen_ids: set, max_pages: int = 5,
                     page_size: int = 100) -> list:
    """
    Page through /trades (newest first) until we reach a trade we've already
    seen, so bursts bigger than one page aren't silently dropped.
    """
    trades: list = []
    for page in range(max_pages):
        batch = fetch_recent_trades(api_url, limit=page_size,
                                    offset=page * page_size)
        if not batch:
            break
        trades.extend(batch)
        if any(trade_unique_id(t) in seen_ids for t in batch):
            break
    else:
        if seen_ids:
            logger.warning(
                f"⚠️  Trade burst exceeded {max_pages * page_size} — "
                "oldest trades this cycle may be missed."
            )
    return trades


class SeenTrades:
    """Bounded set of trade ids. Never wholesale-replaced, so one failed
    fetch can't wipe history and cause a wave of duplicate alerts."""

    def __init__(self, cap: int = 20000):
        self.cap = cap
        self._ids: set = set()
        self._order: deque = deque()

    def __contains__(self, trade_id) -> bool:
        return trade_id in self._ids

    def add(self, trade_id) -> None:
        if trade_id in self._ids:
            return
        self._ids.add(trade_id)
        self._order.append(trade_id)
        while len(self._order) > self.cap:
            self._ids.discard(self._order.popleft())

    @property
    def ids(self) -> set:
        return self._ids


def fetch_market_info(condition_id: str) -> dict:
    """
    Fetch market metadata (title, etc.) from Polymarket Gamma API.
    Returns a dict with at least 'question' key.
    """
    url = "https://gamma-api.polymarket.com/markets"
    params = {"condition_ids": condition_id}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        if isinstance(data, dict):
            return data
        return {}
    except Exception as e:
        logger.debug(f"Could not fetch market info for {condition_id}: {e}")
        return {}


_event_tags_cache: dict = {}
_CACHE_CAP = 2000  # dicts are insertion-ordered, so eviction below is FIFO


def _cache_put(cache: dict, key, value) -> None:
    """Insert into a module-level cache, evicting oldest entries past the cap."""
    while len(cache) >= _CACHE_CAP:
        cache.pop(next(iter(cache)))
    cache[key] = value


def fetch_event_tags(event_slug: str) -> list:
    """Fetch an event's tag labels (Sports, Politics, Crypto, …) from the Gamma API."""
    if not event_slug:
        return []
    if event_slug in _event_tags_cache:
        return _event_tags_cache[event_slug]
    tags = []
    try:
        resp = requests.get("https://gamma-api.polymarket.com/events",
                            params={"slug": event_slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            tags = [t.get("label", "") for t in data[0].get("tags", []) if t.get("label")]
    except Exception as e:
        logger.debug(f"Could not fetch event tags for {event_slug}: {e}")
    _cache_put(_event_tags_cache, event_slug, tags)
    return tags


# ─────────────────────────────────────────────
# Trade processing
# ─────────────────────────────────────────────
def parse_trade_usd_size(trade: dict) -> float:
    """
    Calculate the USD size of a trade.
    size * price gives approximate USD value for a YES trade;
    size * (1 - price) for a NO trade — but simpler: use size as USDC shares.
    Polymarket CLOB: 'size' is the number of outcome shares, 'price' is in USD.
    USD value = size * price (for market buys).
    """
    try:
        size = float(trade.get("size", 0))
        price = float(trade.get("price", 0))
        return size * price
    except (TypeError, ValueError):
        return 0.0


def format_side(side: str) -> str:
    """Normalize side string to YES/NO."""
    s = str(side).upper()
    if s in ("YES", "BUY", "1"):
        return "YES"
    if s in ("NO", "SELL", "0"):
        return "NO"
    return side.upper()


def trade_unique_id(trade: dict) -> str:
    """Generate a unique identifier for a trade to avoid duplicate alerts."""
    if trade.get("id") or trade.get("trade_id"):
        return trade.get("id") or trade.get("trade_id")
    # Data API trades have no id — a tx hash can hold several fills,
    # so combine it with the asset and size
    if trade.get("transactionHash"):
        return f"{trade['transactionHash']}-{trade.get('asset', '')}-{trade.get('size', '')}"
    return str(trade)


def extract_wallet(trade: dict) -> str:
    """Pull the trader's wallet address from a trade, whatever the API calls it."""
    for key in ("proxyWallet", "proxy_wallet", "maker_address", "taker_address",
                "owner", "wallet", "user"):
        val = trade.get(key)
        if val and isinstance(val, str) and val.startswith("0x"):
            return val.lower()
    return ""


def short_wallet(address: str) -> str:
    """0x1234567890abcdef... → 0x1234…cdef"""
    if len(address) > 12:
        return f"{address[:6]}…{address[-4:]}"
    return address


def matches_filters(market_title: str, condition_id: str, category: str,
                    market_filters: list, category_filters: list) -> bool:
    """
    Check a trade against the configured market/category filters.
    Empty filter lists mean "match everything". Market filters match on
    title keyword (case-insensitive) or exact condition ID.
    """
    if market_filters:
        title_lower = (market_title or "").lower()
        ok = any(
            f.lower() in title_lower or f == condition_id
            for f in market_filters
        )
        if not ok:
            return False

    if category_filters:
        # category may be a single string or a list of event tags
        cats = [category] if isinstance(category, str) else (category or [])
        cats_lower = [c.lower() for c in cats if c]
        ok = any(
            f.lower() == c or f.lower() in c
            for f in category_filters for c in cats_lower
        )
        if not ok:
            return False

    return True


# ─────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────
DIVIDER = "─" * 43


def format_terminal_alert(market_title: str, side: str, amount_usd: float,
                           price: float, timestamp: str,
                           wallet_line: str = "") -> str:
    """Format a colorful terminal alert message."""
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    side_color = Fore.GREEN if side == "YES" else Fore.RED
    prob_pct = int(price * 100) if side == "YES" else int((1 - price) * 100)

    lines = [
        f"\n{Fore.CYAN}🐋 WHALE ALERT{Style.RESET_ALL}  {Fore.YELLOW}{ts}{Style.RESET_ALL}",
        f"{Fore.WHITE}{DIVIDER}{Style.RESET_ALL}",
        f"{Fore.WHITE}Market:{Style.RESET_ALL} {market_title}",
        f"{Fore.WHITE}Side:  {Style.RESET_ALL} {side_color}{side}{Style.RESET_ALL}",
        f"{Fore.WHITE}Amount:{Style.RESET_ALL} {Fore.YELLOW}${amount_usd:,.2f}{Style.RESET_ALL}",
        f"{Fore.WHITE}Price: {Style.RESET_ALL} {price:.4f} ({side_color}{prob_pct}% {side}{Style.RESET_ALL})",
    ]
    if wallet_line:
        lines.append(f"{Fore.WHITE}Wallet:{Style.RESET_ALL} {Fore.MAGENTA}{wallet_line}{Style.RESET_ALL}")
    lines.append(f"{Fore.WHITE}{DIVIDER}{Style.RESET_ALL}")
    return "\n".join(lines)


def format_telegram_message(market_title: str, side: str, amount_usd: float,
                             price: float, timestamp: str,
                             wallet_line: str = "") -> str:
    """Format a Telegram alert message (plain text, emoji-rich)."""
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    side_emoji = "✅" if side == "YES" else "❌"
    prob_pct = int(price * 100) if side == "YES" else int((1 - price) * 100)

    wallet_row = f"*Wallet:* `{wallet_line}`\n" if wallet_line else ""
    return (
        f"🐋 *WHALE ALERT*  `{ts}`\n"
        f"{'─' * 30}\n"
        f"*Market:* {market_title}\n"
        f"*Side:*    {side_emoji} {side}\n"
        f"*Amount:* `${amount_usd:,.2f}`\n"
        f"*Price:*   `{price:.4f}` ({prob_pct}% {side})\n"
        f"{wallet_row}"
        f"{'─' * 30}"
    )


# ─────────────────────────────────────────────
# Telegram sender
# ─────────────────────────────────────────────
def send_telegram_alert(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not bot_token or not chat_id:
        logger.debug("Telegram not configured — skipping alert.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        logger.warning(f"Telegram HTTP error: {e} — response: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"Failed to send Telegram alert: {e}")
        return False


def format_discord_message(market_title: str, side: str, amount_usd: float,
                            price: float, timestamp: str,
                            wallet_line: str = "") -> str:
    """Format a Discord alert message."""
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    side_emoji = "✅" if side == "YES" else "❌"
    prob_pct = int(price * 100) if side == "YES" else int((1 - price) * 100)

    wallet_row = f"**Wallet:** `{wallet_line}`\n" if wallet_line else ""
    return (
        f"🐋 **WHALE ALERT**  `{ts}`\n"
        f"{'─' * 30}\n"
        f"**Market:** {market_title}\n"
        f"**Side:**    {side_emoji} {side}\n"
        f"**Amount:** `${amount_usd:,.2f}`\n"
        f"**Price:**   `{price:.4f}` ({prob_pct}% {side})\n"
        f"{wallet_row}"
        f"{'─' * 30}"
    )


def describe_position(outcome: str, side: str) -> str:
    """'YES (BUY)' for binary markets, 'France (BUY)' for multi-outcome ones."""
    outcome = (outcome or "?").upper() if (outcome or "").upper() in ("YES", "NO") else (outcome or "?")
    return f"{outcome} ({side})" if side and side != outcome else str(outcome)


def format_watch_terminal_alert(wallet_line: str, market_title: str,
                                position: str, amount_usd: float,
                                price: float, timestamp: str) -> str:
    """Terminal alert for a trade by a watched wallet."""
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"\n{Fore.MAGENTA}👀 WATCHED WALLET{Style.RESET_ALL}  {Fore.YELLOW}{ts}{Style.RESET_ALL}",
        f"{Fore.WHITE}{DIVIDER}{Style.RESET_ALL}",
        f"{Fore.WHITE}Wallet:{Style.RESET_ALL} {Fore.MAGENTA}{wallet_line}{Style.RESET_ALL}",
        f"{Fore.WHITE}Market:{Style.RESET_ALL} {market_title}",
        f"{Fore.WHITE}Side:  {Style.RESET_ALL} {position}",
        f"{Fore.WHITE}Amount:{Style.RESET_ALL} {Fore.YELLOW}${amount_usd:,.2f}{Style.RESET_ALL}",
        f"{Fore.WHITE}Price: {Style.RESET_ALL} {price:.4f} ({int(price * 100)}%)",
        f"{Fore.WHITE}{DIVIDER}{Style.RESET_ALL}",
    ]
    return "\n".join(lines)


def format_watch_message(wallet_line: str, market_title: str, position: str,
                         amount_usd: float, price: float, timestamp: str,
                         bold: str = "*") -> str:
    """Watched-wallet alert for Telegram (bold='*') or Discord (bold='**')."""
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    b = bold
    return (
        f"👀 {b}WATCHED WALLET{b}  `{ts}`\n"
        f"{'─' * 30}\n"
        f"{b}Wallet:{b} `{wallet_line}`\n"
        f"{b}Market:{b} {market_title}\n"
        f"{b}Side:{b}    {position}\n"
        f"{b}Amount:{b} `${amount_usd:,.2f}`\n"
        f"{b}Price:{b}   `{price:.4f}` ({int(price * 100)}%)\n"
        f"{'─' * 30}"
    )


def format_consensus_message(market_title: str, side: str, wallets: int,
                             volume: float, window_minutes: int,
                             bold: str = "*") -> str:
    """Smart-money consensus alert for Telegram (bold='*') or Discord (bold='**')."""
    b = bold
    return (
        f"🚨 {b}SMART MONEY CONSENSUS{b}\n"
        f"{'─' * 30}\n"
        f"{b}Market:{b} {market_title}\n"
        f"{b}Side:{b}    {side}\n"
        f"{b}Whales:{b} `{wallets}` distinct wallets in the last {window_minutes} min\n"
        f"{b}Volume:{b} `${volume:,.0f}` combined\n"
        f"{'─' * 30}"
    )


def format_consensus_terminal(market_title: str, side: str, wallets: int,
                              volume: float, window_minutes: int) -> str:
    lines = [
        f"\n{Fore.RED}🚨 SMART MONEY CONSENSUS{Style.RESET_ALL}",
        f"{Fore.WHITE}{DIVIDER}{Style.RESET_ALL}",
        f"{Fore.WHITE}Market:{Style.RESET_ALL} {market_title}",
        f"{Fore.WHITE}Side:  {Style.RESET_ALL} {side}",
        f"{Fore.WHITE}Whales:{Style.RESET_ALL} {Fore.YELLOW}{wallets} distinct wallets in the last {window_minutes} min{Style.RESET_ALL}",
        f"{Fore.WHITE}Volume:{Style.RESET_ALL} {Fore.YELLOW}${volume:,.0f} combined{Style.RESET_ALL}",
        f"{Fore.WHITE}{DIVIDER}{Style.RESET_ALL}",
    ]
    return "\n".join(lines)


def format_accumulation_message(wallet_line: str, market_title: str,
                                total: float, count: int,
                                window_minutes: int, bold: str = "*") -> str:
    """Accumulation alert — a wallet quietly built up size in small clips."""
    b = bold
    return (
        f"🧮 {b}WHALE ACCUMULATION{b}\n"
        f"{'─' * 30}\n"
        f"{b}Wallet:{b} `{wallet_line}`\n"
        f"{b}Market:{b} {market_title}\n"
        f"{b}Total:{b}  `${total:,.0f}` across {count} trades in {window_minutes} min\n"
        f"{b}Note:{b}   every trade stayed under the whale threshold\n"
        f"{'─' * 30}"
    )


def format_accumulation_terminal(wallet_line: str, market_title: str,
                                 total: float, count: int,
                                 window_minutes: int) -> str:
    lines = [
        f"\n{Fore.BLUE}🧮 WHALE ACCUMULATION{Style.RESET_ALL}",
        f"{Fore.WHITE}{DIVIDER}{Style.RESET_ALL}",
        f"{Fore.WHITE}Wallet:{Style.RESET_ALL} {Fore.MAGENTA}{wallet_line}{Style.RESET_ALL}",
        f"{Fore.WHITE}Market:{Style.RESET_ALL} {market_title}",
        f"{Fore.WHITE}Total: {Style.RESET_ALL} {Fore.YELLOW}${total:,.0f} across {count} trades in {window_minutes} min{Style.RESET_ALL}",
        f"{Fore.WHITE}Note:  {Style.RESET_ALL} every trade stayed under the whale threshold",
        f"{Fore.WHITE}{DIVIDER}{Style.RESET_ALL}",
    ]
    return "\n".join(lines)


def send_discord_alert(webhook_url: str, message: str) -> bool:
    """Send a message via Discord Webhook API. Returns True on success."""
    if not webhook_url:
        logger.debug("Discord not configured — skipping alert.")
        return False

    payload = {
        "content": message,
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        logger.warning(f"Discord HTTP error: {e}")
        return False
    except Exception as e:
        logger.warning(f"Failed to send Discord alert: {e}")
        return False




def export_trade(file_path: str, trade_data: dict) -> None:
    """Export trade data to a CSV or JSON file."""
    if not file_path:
        return

    ext = os.path.splitext(file_path)[1].lower()
    
    if ext == ".csv":
        file_exists = os.path.isfile(file_path)
        with open(file_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=trade_data.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(trade_data)
    elif ext == ".json":
        all_data = []
        if os.path.isfile(file_path):
            try:
                with open(file_path, "r") as f:
                    all_data = json.load(f)
            except (json.JSONDecodeError, ValueError):
                all_data = []
        
        all_data.append(trade_data)
        with open(file_path, "w") as f:
            json.dump(all_data, f, indent=2)
    else:
        logger.warning(f"Unsupported export format: {ext}. Use .csv or .json.")

# ─────────────────────────────────────────────
# Market info cache (avoid hammering the API)
# ─────────────────────────────────────────────
_market_cache: dict = {}


def get_market_details(condition_id: str) -> dict:
    """Return {'title', 'category'} for a market, cached to reduce API calls."""
    if condition_id in _market_cache:
        return _market_cache[condition_id]

    info = fetch_market_info(condition_id)
    details = {
        "title": (
            info.get("question")
            or info.get("title")
            or info.get("name")
            or ""
        ),
        "category": info.get("category") or "",
    }
    _cache_put(_market_cache, condition_id, details)
    return details


# ─────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────
def run(config: dict, export_path: str = None) -> None:
    """Main monitoring loop."""
    min_size = float(config["min_trade_size"])
    interval = int(config["check_interval"])
    # Allow env var override — useful for geo-restricted regions
    # Set POLYMARKET_API_URL=https://polyclawster.com/api/clob-relay to bypass geo-blocks
    api_url = os.getenv("POLYMARKET_API_URL", config["polymarket"]["api_url"])
    bot_token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]
    discord_webhook = config["discord"]["webhook_url"]

    market_filters = list(config["filters"]["markets"] or [])
    category_filters = list(config["filters"]["categories"] or [])
    cooldown = int(config["alert_cooldown"] or 0)
    recurring_threshold = int(config["wallets"]["recurring_threshold"] or 0)
    custom_wallet_tags = {
        str(k).lower(): v for k, v in (config["wallets"]["tags"] or {}).items()
    }

    consensus_cfg = config["consensus"]
    consensus_enabled = bool(consensus_cfg.get("enabled", True))
    consensus_min = int(consensus_cfg.get("min_wallets", 3))
    consensus_window = int(consensus_cfg.get("window_minutes", 60))

    accum_cfg = config["accumulation"]
    accum_enabled = bool(accum_cfg.get("enabled", True))
    accum_window = int(accum_cfg.get("window_minutes", 60))
    accum_threshold = float(accum_cfg.get("threshold", 0) or 0) or min_size

    telegram_enabled = bool(bot_token and chat_id and
                            bot_token != "YOUR_BOT_TOKEN" and
                            chat_id != "YOUR_CHAT_ID")
    discord_enabled = bool(discord_webhook and
                           discord_webhook != "YOUR_DISCORD_WEBHOOK_URL")

    # Persistence: SQLite by default, Postgres when DATABASE_URL is set
    Session = db.init_db()

    # Watchlist: wallets followed at any trade size. Config-file entries are
    # seeded into the DB so the dashboard sees them too; the dashboard can
    # add/remove more at runtime.
    watch_min = float(config["watchlist"]["min_trade_size"] or 0)
    config_watch = {
        str(a).lower(): str(lbl or "")
        for a, lbl in (config["watchlist"]["addresses"] or {}).items()
    }
    watch_count = len(config_watch)
    if Session:
        try:
            with Session() as session:
                for addr, lbl in config_watch.items():
                    db.add_watched_address(session, addr, lbl)
                watch_count = len(db.get_watched_addresses(session))
        except Exception as e:
            logger.debug(f"Watchlist seed failed: {e}")

    filters_desc = ", ".join(market_filters + category_filters) or "all markets"

    print(f"\n{Fore.CYAN}{'═' * 50}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}🐋  Polymarket Whale Tracker — Starting up{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * 50}{Style.RESET_ALL}")
    print(f"  Min trade size : {Fore.YELLOW}${min_size:,.0f}{Style.RESET_ALL}")
    print(f"  Check interval : {Fore.YELLOW}{interval}s{Style.RESET_ALL}")
    print(f"  Filters        : {Fore.YELLOW}{filters_desc}{Style.RESET_ALL}")
    print(f"  Alert cooldown : {Fore.YELLOW}{cooldown}s{Style.RESET_ALL}")
    print(f"  Telegram alerts: {Fore.GREEN+'ON' if telegram_enabled else Fore.RED+'OFF'}{Style.RESET_ALL}")
    print(f"  Discord alerts : {Fore.GREEN+'ON' if discord_enabled else Fore.RED+'OFF'}{Style.RESET_ALL}")
    print(f"  Database       : {Fore.GREEN+'ON' if Session else Fore.RED+'OFF'}{Style.RESET_ALL}")
    print(f"  Watched wallets: {Fore.YELLOW}{watch_count}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * 50}{Style.RESET_ALL}\n")

    if not (telegram_enabled or discord_enabled):
        logger.info("ℹ️  Alerts not configured — terminal-only mode.")

    seen = SeenTrades()
    last_alert_at: dict = {}  # condition_id -> monotonic time of last alert
    # Consensus: (condition_id, side) -> (wallet count last alerted at, monotonic ts)
    consensus_alerted: dict = {}
    # Accumulation: (wallet, condition_id) -> deque of (monotonic ts, usd);
    # only sub-threshold trades — a whale-sized clip alerts on its own
    accum: dict = {}
    accum_alerted: dict = {}  # (wallet, condition_id) -> monotonic ts
    first_run = True

    while True:
        try:
            if first_run:
                trades = fetch_recent_trades(api_url)  # seed only, no paging
            else:
                trades = fetch_new_trades(api_url, seen.ids)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"Error in fetch loop: {e}")
            trades = []

        # Refresh the watchlist every cycle so dashboard adds/removes apply live
        watched = dict(config_watch)
        if Session:
            try:
                with Session() as session:
                    for w in db.get_watched_addresses(session):
                        watched[w.address] = w.label or watched.get(w.address, "")
            except Exception as e:
                logger.debug(f"Watchlist load failed: {e}")

        whale_count = 0

        for trade in trades:
            trade_id = trade_unique_id(trade)

            # Skip already-seen trades (and mark this one seen)
            if trade_id in seen:
                continue
            seen.add(trade_id)

            # On first run, just populate the seen set (don't alert on old trades)
            if first_run:
                continue

            # Calculate USD size, identify the trader, and filter
            amount_usd = parse_trade_usd_size(trade)
            wallet_addr = extract_wallet(trade)
            is_watched = bool(wallet_addr) and wallet_addr in watched \
                and amount_usd >= watch_min
            is_whale = amount_usd >= min_size

            # ── Accumulation: sub-threshold trades summed per wallet+market ──
            if (accum_enabled and wallet_addr and amount_usd > 0
                    and not is_whale and not is_watched):
                cid = (trade.get("market") or trade.get("conditionId")
                       or trade.get("condition_id", ""))
                key = (wallet_addr, cid)
                nowm = time.monotonic()
                dq = accum.setdefault(key, deque())
                dq.append((nowm, amount_usd))
                while dq and nowm - dq[0][0] > accum_window * 60:
                    dq.popleft()
                total = sum(a for _, a in dq)
                last_fired = accum_alerted.get(key, float("-inf"))
                if total >= accum_threshold and nowm - last_fired > accum_window * 60:
                    accum_alerted[key] = nowm
                    acc_title = (trade.get("title") or trade.get("question")
                                 or (trade.get("slug") or "").replace("-", " ")
                                 or "Unknown Market")
                    acc_line = short_wallet(wallet_addr)
                    print(format_accumulation_terminal(
                        acc_line, acc_title, total, len(dq), accum_window))
                    if telegram_enabled:
                        send_telegram_alert(bot_token, chat_id, format_accumulation_message(
                            acc_line, acc_title, total, len(dq), accum_window, bold="*"))
                    if discord_enabled:
                        send_discord_alert(discord_webhook, format_accumulation_message(
                            acc_line, acc_title, total, len(dq), accum_window, bold="**"))

            if not (is_whale or is_watched):
                continue

            # Get trade details
            condition_id = (trade.get("market") or trade.get("conditionId")
                            or trade.get("condition_id", ""))
            outcome = str(trade.get("outcome", ""))
            raw_side = str(trade.get("side", "")).upper()  # BUY / SELL
            if outcome.upper() in ("YES", "NO"):
                side = outcome.upper()
            else:
                side = format_side(trade.get("side", ""))
            price = float(trade.get("price", 0))
            ts_raw = trade.get("timestamp") or trade.get("created_at", "")

            # Parse timestamp
            traded_at = datetime.now(timezone.utc)
            if ts_raw:
                try:
                    if isinstance(ts_raw, (int, float)):
                        traded_at = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
                        ts = traded_at.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        ts = str(ts_raw)[:19].replace("T", " ")
                        traded_at = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            else:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Market title: Data API trades carry it; fall back to a Gamma
            # lookup, then to the market slug — never just the condition id
            market_title = trade.get("title") or trade.get("question") or ""
            if not market_title and condition_id:
                market_title = get_market_details(condition_id)["title"]
            if not market_title:
                slug = trade.get("slug") or trade.get("eventSlug") or ""
                market_title = slug.replace("-", " ").strip().capitalize()
            if not market_title:
                market_title = (f"Market {condition_id[:10]}…" if condition_id
                                else "Unknown Market")
            base_title = market_title
            # Multi-outcome market (sports, elections): name the outcome traded
            if outcome and outcome.upper() not in ("YES", "NO"):
                market_title = f"{market_title} — {outcome}"

            # Category: event tags (Sports, Politics, Crypto, …), first tag is primary
            tags = fetch_event_tags(trade.get("eventSlug", ""))
            if not tags and condition_id:
                cat = get_market_details(condition_id)["category"]
                tags = [cat] if cat else []
            category = tags[0] if tags else ""

            # ── Watched wallet: record + alert on every trade, any size ──
            if is_watched:
                label = watched.get(wallet_addr, "")
                short = short_wallet(wallet_addr)
                watch_line = f"{short} — {label}" if label else short
                position = describe_position(outcome or side, raw_side)

                if Session:
                    try:
                        with Session() as session:
                            db.record_watched_trade(
                                session,
                                trade_id=str(trade_id),
                                address=wallet_addr,
                                condition_id=condition_id,
                                market_title=base_title,
                                outcome=str(outcome or side),
                                side=raw_side,
                                price=price,
                                amount_usd=amount_usd,
                                traded_at=traded_at,
                            )
                            if condition_id:
                                db.upsert_market(
                                    session,
                                    condition_id=condition_id,
                                    title=base_title,
                                    slug=trade.get("slug", "") or "",
                                    event_slug=trade.get("eventSlug", "") or "",
                                    category=category,
                                )
                    except Exception as e:
                        logger.warning(f"Watched trade persist failed: {e}")

                print(format_watch_terminal_alert(watch_line, market_title,
                                                  position, amount_usd, price, ts))
                if telegram_enabled:
                    send_telegram_alert(bot_token, chat_id, format_watch_message(
                        watch_line, market_title, position, amount_usd, price, ts, bold="*"))
                if discord_enabled:
                    send_discord_alert(discord_webhook, format_watch_message(
                        watch_line, market_title, position, amount_usd, price, ts, bold="**"))

            # ── Whale pipeline: size threshold + market/category filters ──
            if not is_whale:
                continue

            # Market / category filters (any tag can match)
            if not matches_filters(market_title, condition_id, tags,
                                   market_filters, category_filters):
                continue

            whale_count += 1

            # Track the whale wallet and build its display tag
            wallet_line = ""
            if wallet_addr:
                wallet_line = short_wallet(wallet_addr)
                if Session:
                    try:
                        with Session() as session:
                            w = db.upsert_wallet(session, wallet_addr, amount_usd,
                                                 recurring_threshold, custom_wallet_tags)
                            if w.tag:
                                wallet_line = f"{wallet_line} {w.tag} ({w.trade_count} trades)"
                    except Exception as e:
                        logger.debug(f"Wallet upsert failed: {e}")
                elif wallet_addr in custom_wallet_tags:
                    wallet_line = f"{wallet_line} {custom_wallet_tags[wallet_addr]}"

            # Persist the trade
            if Session:
                try:
                    with Session() as session:
                        db.record_trade(
                            session,
                            trade_id=str(trade_id),
                            condition_id=condition_id,
                            market_title=market_title,
                            category=category,
                            side=side,
                            price=price,
                            amount_usd=amount_usd,
                            wallet=wallet_addr,
                            traded_at=traded_at,
                        )
                        if condition_id:
                            db.upsert_market(
                                session,
                                condition_id=condition_id,
                                title=base_title,
                                slug=trade.get("slug", "") or "",
                                event_slug=trade.get("eventSlug", "") or "",
                                category=category,
                            )
                except Exception as e:
                    logger.warning(f"Trade persist failed: {e}")

            # ── Smart money consensus: N distinct whales, same market+side ──
            if consensus_enabled and Session and condition_id and wallet_addr:
                try:
                    with Session() as session:
                        c = db.market_side_whales(session, condition_id, side,
                                                  consensus_window)
                    ckey = (condition_id, side)
                    prev_count, prev_ts = consensus_alerted.get(ckey, (0, 0))
                    # Reset once the window has fully passed since the last alert
                    if time.monotonic() - prev_ts > consensus_window * 60:
                        prev_count = 0
                    if c["wallets"] >= consensus_min and c["wallets"] > prev_count:
                        consensus_alerted[ckey] = (c["wallets"], time.monotonic())
                        print(format_consensus_terminal(
                            market_title, side, c["wallets"], c["volume"],
                            consensus_window))
                        if telegram_enabled:
                            send_telegram_alert(bot_token, chat_id, format_consensus_message(
                                market_title, side, c["wallets"], c["volume"],
                                consensus_window, bold="*"))
                        if discord_enabled:
                            send_discord_alert(discord_webhook, format_consensus_message(
                                market_title, side, c["wallets"], c["volume"],
                                consensus_window, bold="**"))
                except Exception as e:
                    logger.debug(f"Consensus check failed: {e}")

            # Export to CSV/JSON if requested
            if export_path:
                export_trade(export_path, {
                    "timestamp": ts,
                    "market": market_title,
                    "category": category,
                    "side": side,
                    "amount_usd": round(amount_usd, 2),
                    "price": price,
                    "wallet": wallet_addr,
                    "condition_id": condition_id,
                })

            # Watched wallets already got their own alert above —
            # don't send a second notification for the same trade
            if is_watched:
                continue

            # Per-market alert cooldown — trade is still recorded above,
            # we just skip the noisy notifications
            if cooldown and condition_id:
                since_last = time.monotonic() - last_alert_at.get(condition_id, -cooldown)
                if since_last < cooldown:
                    logger.info(
                        f"🔇 Cooldown ({cooldown - since_last:.0f}s left) — "
                        f"muted ${amount_usd:,.0f} on {market_title[:50]}"
                    )
                    continue
                last_alert_at[condition_id] = time.monotonic()

            # Print terminal alert
            print(format_terminal_alert(market_title, side, amount_usd, price, ts, wallet_line))

            # Send Telegram alert
            if telegram_enabled:
                tg_msg = format_telegram_message(market_title, side, amount_usd, price, ts, wallet_line)
                ok = send_telegram_alert(bot_token, chat_id, tg_msg)
                if ok:
                    logger.debug("✅ Telegram alert sent.")

            # Send Discord alert
            if discord_enabled:
                ds_msg = format_discord_message(market_title, side, amount_usd, price, ts, wallet_line)
                ok = send_discord_alert(discord_webhook, ds_msg)
                if ok:
                    logger.debug("✅ Discord alert sent.")

        # Prune accumulation state so quiet wallet/market pairs don't pile up
        if accum_enabled:
            nowm = time.monotonic()
            stale = [k for k, dq in accum.items()
                     if not dq or nowm - dq[-1][0] > accum_window * 60]
            for k in stale:
                del accum[k]
            accum_alerted = {k: ts for k, ts in accum_alerted.items()
                             if nowm - ts <= accum_window * 60}

        # Heartbeat: let the dashboard know the tracker loop is alive
        if Session:
            try:
                with Session() as session:
                    db.heartbeat(session, interval)
            except Exception as e:
                logger.debug(f"Heartbeat failed: {e}")

        if not first_run and whale_count == 0:
            logger.info(f"No whale trades found this cycle. Sleeping {interval}s...")
        first_run = False

        time.sleep(interval)


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Whale Tracker")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file")
    parser.add_argument("--export", help="Export path for whale trades (.csv or .json)")
    parser.add_argument("--market", action="append", default=[],
                        help="Only alert on markets matching this keyword or condition ID (repeatable)")
    parser.add_argument("--category", action="append", default=[],
                        help="Only alert on markets in this category, e.g. Politics (repeatable)")
    args = parser.parse_args()
    cfg_path = args.config

    config = load_config(cfg_path)
    if args.market:
        config["filters"]["markets"] = args.market
    if args.category:
        config["filters"]["categories"] = args.category

    try:
        run(config, export_path=args.export)
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}👋 Whale Tracker stopped.{Style.RESET_ALL}\n")
        sys.exit(0)
