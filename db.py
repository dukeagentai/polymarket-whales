#!/usr/bin/env python3
"""
🐋 Polymarket Whale Tracker — database layer.

SQLite by default (zero setup), Postgres when DATABASE_URL is set
(Railway injects DATABASE_URL automatically when you add a Postgres service).
"""

import os
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    Float,
    String,
    DateTime,
    func,
)
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)

Base = declarative_base()

DEFAULT_DB_URL = "sqlite:///whales.db"

# Auto-tag tiers by lifetime whale volume (USD)
TAG_TIERS = [
    (250_000, "🐳 Mega Whale"),
    (50_000, "🐋 Whale"),
    (10_000, "🦈 Shark"),
]


class WhaleTrade(Base):
    __tablename__ = "whale_trades"

    id = Column(Integer, primary_key=True)
    # txHash-asset-size: asset ids are ~77-digit ints, so this runs ~150+ chars
    trade_id = Column(String(256), unique=True, index=True)
    condition_id = Column(String(128), index=True)
    market_title = Column(String(512))
    category = Column(String(128), index=True)
    side = Column(String(16))
    price = Column(Float)
    amount_usd = Column(Float)
    wallet = Column(String(64), index=True)
    traded_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # "" = market still open, "WIN" / "LOSS" once the market resolves.
    # Existing deployments need migrate_002_trade_results.py to add this column.
    result = Column(String(8), default="", index=True)


class Wallet(Base):
    __tablename__ = "wallets"

    address = Column(String(64), primary_key=True)
    tag = Column(String(128), default="")
    trade_count = Column(Integer, default=0)
    total_usd = Column(Float, default=0.0)
    first_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class WatchedAddress(Base):
    """A wallet the user explicitly follows — every trade it makes is recorded."""
    __tablename__ = "watched_addresses"

    address = Column(String(64), primary_key=True)
    label = Column(String(128), default="")
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TrackerStatus(Base):
    """Single-row heartbeat the tracker updates every poll cycle, so the
    dashboard can tell when the tracker has silently died."""
    __tablename__ = "tracker_status"

    id = Column(Integer, primary_key=True)  # always 1
    last_poll_at = Column(DateTime)
    interval_seconds = Column(Integer, default=30)


class WatchedTrade(Base):
    """Every trade made by a watched address, regardless of size."""
    __tablename__ = "watched_trades"

    id = Column(Integer, primary_key=True)
    trade_id = Column(String(256), unique=True, index=True)
    address = Column(String(64), index=True)
    condition_id = Column(String(128), index=True)
    market_title = Column(String(512))
    outcome = Column(String(128))  # YES / NO / France / Morocco / …
    side = Column(String(16))      # BUY / SELL
    price = Column(Float)
    amount_usd = Column(Float)
    traded_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # "" = market still open, "WIN" / "LOSS" once the market resolves.
    # Existing deployments need migrate_002_trade_results.py to add this column.
    result = Column(String(8), default="", index=True)


class Market(Base):
    """A Polymarket market we've seen at least one trade in. Populated
    incrementally by the trade pipeline; resolution state is filled in by
    resolve_markets.py polling the Gamma API."""
    __tablename__ = "markets"

    condition_id = Column(String(128), primary_key=True)
    title = Column(String(512), default="")
    slug = Column(String(256), default="")
    event_slug = Column(String(256), default="")
    category = Column(String(128), default="", index=True)
    end_date = Column(DateTime, nullable=True)
    resolved = Column(Integer, default=0, index=True)  # 0 = open, 1 = resolved
    winning_outcome = Column(String(128), default="")
    resolved_at = Column(DateTime, nullable=True)
    first_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_checked = Column(DateTime, nullable=True)


class WalletPnlSnapshot(Base):
    """Point-in-time PnL snapshot for a wallet, written periodically by the
    position-sync job so we can see whether a whale's edge is trending up
    or down, not just a single live number."""
    __tablename__ = "wallet_pnl_snapshots"

    id = Column(Integer, primary_key=True)
    address = Column(String(64), index=True)
    taken_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    open_value = Column(Float, default=0.0)
    unrealized = Column(Float, default=0.0)
    realized = Column(Float, default=0.0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)


def get_db_url() -> str:
    url = os.getenv("DATABASE_URL", "") or DEFAULT_DB_URL
    # Railway/Heroku hand out postgres:// URLs; SQLAlchemy wants postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def init_db(url: str = None):
    """Create engine + tables, return a session factory. Returns None on failure."""
    url = url or get_db_url()
    try:
        engine = create_engine(url, pool_pre_ping=True, future=True)
        Base.metadata.create_all(engine)
        scheme = url.split("://", 1)[0]
        logger.info(f"💾 Database ready ({scheme})")
        return sessionmaker(bind=engine, future=True)
    except Exception as e:
        logger.warning(f"⚠️  Database unavailable ({e}) — running without persistence.")
        return None


def auto_tag(total_usd: float, trade_count: int, recurring_threshold: int) -> str:
    """Pick an automatic tag from volume tiers / recurrence."""
    for threshold, tag in TAG_TIERS:
        if total_usd >= threshold:
            return tag
    if recurring_threshold and trade_count >= recurring_threshold:
        return "🔁 Recurring Whale"
    return ""


def record_trade(session, *, trade_id: str, condition_id: str, market_title: str,
                 category: str, side: str, price: float, amount_usd: float,
                 wallet: str, traded_at: datetime) -> None:
    """Insert a whale trade (idempotent on trade_id)."""
    exists = session.query(WhaleTrade.id).filter_by(trade_id=trade_id).first()
    if exists:
        return
    session.add(WhaleTrade(
        trade_id=trade_id,
        condition_id=condition_id,
        market_title=market_title,
        category=category,
        side=side,
        price=price,
        amount_usd=amount_usd,
        wallet=wallet,
        traded_at=traded_at,
    ))
    session.commit()


def upsert_wallet(session, address: str, amount_usd: float,
                  recurring_threshold: int, custom_tags: dict) -> Wallet:
    """Update wallet stats for a new trade and (re)compute its tag."""
    now = datetime.now(timezone.utc)
    wallet = session.get(Wallet, address)
    if wallet is None:
        wallet = Wallet(address=address, first_seen=now)
        session.add(wallet)
    wallet.trade_count = (wallet.trade_count or 0) + 1
    wallet.total_usd = (wallet.total_usd or 0.0) + amount_usd
    wallet.last_seen = now
    # Custom tags from config win over auto tags
    custom = custom_tags.get(address) or custom_tags.get(address.lower())
    wallet.tag = custom or auto_tag(wallet.total_usd, wallet.trade_count,
                                    recurring_threshold)
    session.commit()
    return wallet


def upsert_market(session, *, condition_id: str, title: str = "", slug: str = "",
                  event_slug: str = "", category: str = "",
                  end_date: datetime = None) -> "Market":
    """Insert a market if new, or fill in any blank fields — never overwrite
    a non-empty value with an empty one."""
    if not condition_id:
        return None
    market = session.get(Market, condition_id)
    if market is None:
        market = Market(condition_id=condition_id)
        session.add(market)
    if title and not market.title:
        market.title = title
    if slug and not market.slug:
        market.slug = slug
    if event_slug and not market.event_slug:
        market.event_slug = event_slug
    if category and not market.category:
        market.category = category
    if end_date and not market.end_date:
        market.end_date = end_date
    session.commit()
    return market


def unresolved_markets(session, limit: int = 100) -> list:
    """Open markets, never-checked ones first, then oldest-checked — works
    the same on SQLite and Postgres (they order NULLs differently)."""
    return (
        session.query(Market)
        .filter(Market.resolved == 0)
        .order_by(Market.last_checked.is_(None).desc(), Market.last_checked.asc())
        .limit(limit)
        .all()
    )


def mark_market_resolved(session, condition_id: str, winning_outcome: str) -> None:
    """Flag a market as resolved with its winning outcome."""
    market = session.get(Market, condition_id)
    if market is None:
        return
    market.resolved = 1
    market.winning_outcome = winning_outcome
    market.resolved_at = datetime.now(timezone.utc)
    session.commit()


def settle_market_trades(session, condition_id: str, winning_outcome: str) -> dict:
    """Mark every unsettled trade in a resolved market WIN or LOSS.

    whale_trades only carries `side` (the outcome traded, e.g. YES/NO or the
    raw outcome name for multi-outcome markets) — treated as an implicit BUY.
    watched_trades carries both `outcome` (what was bet on) and `side`
    (BUY/SELL), so a SELL of the winner is a LOSS and a SELL of a loser is a
    WIN. Comparisons are case-insensitive. Idempotent: only touches rows
    where result == "".
    """
    winner = (winning_outcome or "").strip().upper()

    whale_updated = 0
    whale_rows = (
        session.query(WhaleTrade)
        .filter(WhaleTrade.condition_id == condition_id, WhaleTrade.result == "")
        .all()
    )
    for t in whale_rows:
        traded_outcome = (t.side or "").strip().upper()
        t.result = "WIN" if traded_outcome == winner else "LOSS"
        whale_updated += 1

    watched_updated = 0
    watched_rows = (
        session.query(WatchedTrade)
        .filter(WatchedTrade.condition_id == condition_id, WatchedTrade.result == "")
        .all()
    )
    for t in watched_rows:
        traded_outcome = (t.outcome or "").strip().upper()
        is_buy = (t.side or "").strip().upper() != "SELL"
        won = (traded_outcome == winner) if is_buy else (traded_outcome != winner)
        t.result = "WIN" if won else "LOSS"
        watched_updated += 1

    session.commit()
    return {"whale_trades": whale_updated, "watched_trades": watched_updated}


def heartbeat(session, interval_seconds: int) -> None:
    """Record that the tracker just completed a poll cycle."""
    row = session.get(TrackerStatus, 1)
    if row is None:
        row = TrackerStatus(id=1)
        session.add(row)
    row.last_poll_at = datetime.now(timezone.utc)
    row.interval_seconds = interval_seconds
    session.commit()


def tracker_health(session) -> dict:
    """
    {'alive': True/False/None, 'last_poll_at': datetime|None, 'stale_seconds': float|None}
    alive is None when the tracker has never reported (fresh install).
    Stale = no heartbeat for 3 poll intervals (min 120s).
    """
    row = session.get(TrackerStatus, 1)
    if row is None or row.last_poll_at is None:
        return {"alive": None, "last_poll_at": None, "stale_seconds": None}
    last = row.last_poll_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    allowed = max(3 * (row.interval_seconds or 30), 120)
    return {"alive": age <= allowed, "last_poll_at": last, "stale_seconds": age}


def get_watched_addresses(session) -> list:
    """All watched addresses, oldest first."""
    return session.query(WatchedAddress).order_by(WatchedAddress.added_at).all()


def add_watched_address(session, address: str, label: str = "") -> WatchedAddress:
    """Add a wallet to the watchlist (or update its label)."""
    address = address.lower()
    watched = session.get(WatchedAddress, address)
    if watched is None:
        watched = WatchedAddress(address=address)
        session.add(watched)
    if label:
        watched.label = label
    session.commit()
    return watched


def remove_watched_address(session, address: str) -> bool:
    """Remove a wallet from the watchlist. Its trade history is kept."""
    watched = session.get(WatchedAddress, address.lower())
    if watched is None:
        return False
    session.delete(watched)
    session.commit()
    return True


def record_watched_trade(session, *, trade_id: str, address: str,
                         condition_id: str, market_title: str, outcome: str,
                         side: str, price: float, amount_usd: float,
                         traded_at: datetime) -> None:
    """Insert a watched-wallet trade (idempotent on trade_id)."""
    exists = session.query(WatchedTrade.id).filter_by(trade_id=trade_id).first()
    if exists:
        return
    session.add(WatchedTrade(
        trade_id=trade_id,
        address=address.lower(),
        condition_id=condition_id,
        market_title=market_title,
        outcome=outcome,
        side=side,
        price=price,
        amount_usd=amount_usd,
        traded_at=traded_at,
    ))
    session.commit()


def watched_address_stats(session) -> dict:
    """Per-address aggregates: {address: {trades, volume, last_traded}}."""
    rows = (
        session.query(
            WatchedTrade.address,
            func.count(WatchedTrade.id),
            func.coalesce(func.sum(WatchedTrade.amount_usd), 0),
            func.max(WatchedTrade.traded_at),
        )
        .group_by(WatchedTrade.address)
        .all()
    )
    return {
        addr: {"trades": count, "volume": float(volume), "last_traded": last}
        for addr, count, volume, last in rows
    }


def market_convergence(session, limit_trades: int = 2000) -> list:
    """
    Group watched-wallet trades by market so overlapping bets stand out.
    Returns markets sorted by how many watched wallets are in them, each with
    a per-(outcome, side) breakdown of wallets and dollars wagered.
    """
    rows = (
        session.query(WatchedTrade)
        .join(WatchedAddress, WatchedAddress.address == WatchedTrade.address)
        .order_by(WatchedTrade.traded_at.desc())
        .limit(limit_trades)
        .all()
    )
    markets: dict = {}
    for t in rows:
        key = t.condition_id or t.market_title
        m = markets.setdefault(key, {
            "title": t.market_title,
            "wallets": set(),
            "positions": {},   # (outcome, side) -> {wallets, total}
            "total": 0.0,
            "last_traded": t.traded_at,
        })
        m["wallets"].add(t.address)
        m["total"] += t.amount_usd or 0
        if t.traded_at and (m["last_traded"] is None or t.traded_at > m["last_traded"]):
            m["last_traded"] = t.traded_at
        pos = m["positions"].setdefault((t.outcome or "?", t.side or "?"),
                                        {"wallets": set(), "total": 0.0})
        pos["wallets"].add(t.address)
        pos["total"] += t.amount_usd or 0

    result = []
    for m in markets.values():
        positions = [
            {"outcome": outcome, "side": side,
             "wallets": sorted(p["wallets"]), "total": p["total"]}
            for (outcome, side), p in m["positions"].items()
        ]
        positions.sort(key=lambda p: -p["total"])
        result.append({
            "title": m["title"],
            "wallet_count": len(m["wallets"]),
            "wallets": sorted(m["wallets"]),
            "positions": positions,
            "total": m["total"],
            "last_traded": m["last_traded"],
            # Everyone in this market is on the same outcome AND side
            "consensus": len(m["positions"]) == 1 and len(m["wallets"]) >= 2,
        })
    result.sort(key=lambda m: (-m["wallet_count"], -m["total"]))
    return result


def market_side_whales(session, condition_id: str, side: str,
                       window_minutes: int = 60) -> dict:
    """Distinct whale wallets (and their volume) on one side of a market
    within the trailing window — the 'smart money consensus' signal."""
    since = (datetime.now(timezone.utc)
             - timedelta(minutes=window_minutes)).replace(tzinfo=None)
    count, volume = (
        session.query(
            func.count(func.distinct(WhaleTrade.wallet)),
            func.coalesce(func.sum(WhaleTrade.amount_usd), 0),
        )
        .filter(
            WhaleTrade.condition_id == condition_id,
            WhaleTrade.side == side,
            WhaleTrade.traded_at >= since,
            WhaleTrade.wallet != "",
            WhaleTrade.wallet.isnot(None),
        )
        .one()
    )
    return {"wallets": count, "volume": float(volume)}


def wallet_leaderboard(session, days: int = 7, limit: int = 50) -> list:
    """
    Top whale wallets by volume over the trailing window.
    Returns [{address, trades, volume, biggest, last_traded, tag}].
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    volume = func.coalesce(func.sum(WhaleTrade.amount_usd), 0)
    rows = (
        session.query(
            WhaleTrade.wallet,
            func.count(WhaleTrade.id),
            volume,
            func.max(WhaleTrade.amount_usd),
            func.max(WhaleTrade.traded_at),
        )
        .filter(WhaleTrade.wallet != "", WhaleTrade.wallet.isnot(None),
                WhaleTrade.traded_at >= since)
        .group_by(WhaleTrade.wallet)
        .order_by(volume.desc())
        .limit(limit)
        .all()
    )
    addresses = [r[0] for r in rows]
    tags = {
        w.address: w.tag
        for w in session.query(Wallet).filter(Wallet.address.in_(addresses))
    } if addresses else {}
    return [
        {"address": addr, "trades": count, "volume": float(vol),
         "biggest": float(biggest or 0), "last_traded": last,
         "tag": tags.get(addr, "")}
        for addr, count, vol, biggest, last in rows
    ]


def wallet_market_breakdown(session, address: str, limit: int = 15) -> list:
    """A wallet's whale volume grouped by market, biggest first."""
    volume = func.coalesce(func.sum(WhaleTrade.amount_usd), 0)
    rows = (
        session.query(
            WhaleTrade.market_title,
            func.count(WhaleTrade.id),
            volume,
            func.max(WhaleTrade.traded_at),
        )
        .filter(WhaleTrade.wallet == address.lower())
        .group_by(WhaleTrade.market_title)
        .order_by(volume.desc())
        .limit(limit)
        .all()
    )
    return [
        {"market": title, "trades": count, "volume": float(vol), "last_traded": last}
        for title, count, vol, last in rows
    ]


def wallet_record(session, address: str) -> dict:
    """Win/loss record + realized PnL from settled trades, for one wallet.

    Draws on both whale_trades and watched_trades — a trade that appears in
    both (a watched wallet's whale-sized trade) is deduped by trade_id so it
    only counts once. Realized PnL is an approximation from what we actually
    recorded, not a full cost-basis ledger:
      - WIN:  shares ~= amount_usd / price (a winning share redeems for $1),
              pnl = shares - amount_usd
      - LOSS: pnl = -amount_usd
      - SELL trades (watched_trades only) count toward wins/losses but are
        excluded from realized_pnl — we don't track their cost basis.
    Returns {"wins", "losses", "open", "win_rate", "realized_pnl"}.
    """
    address = address.lower()

    whale_rows = (
        session.query(WhaleTrade.trade_id, WhaleTrade.result,
                     WhaleTrade.amount_usd, WhaleTrade.price)
        .filter(WhaleTrade.wallet == address)
        .all()
    )
    watched_rows = (
        session.query(WatchedTrade.trade_id, WatchedTrade.result,
                     WatchedTrade.amount_usd, WatchedTrade.price, WatchedTrade.side)
        .filter(WatchedTrade.address == address)
        .all()
    )

    seen_ids: set = set()
    wins = losses = open_count = 0
    realized_pnl = 0.0

    def _tally(trade_id, result, amount_usd, price, is_sell):
        nonlocal wins, losses, open_count, realized_pnl
        if trade_id in seen_ids:
            return
        seen_ids.add(trade_id)
        if result == "WIN":
            wins += 1
            if not is_sell:
                shares = (amount_usd / price) if price else 0.0
                realized_pnl += shares - amount_usd
        elif result == "LOSS":
            losses += 1
            if not is_sell:
                realized_pnl -= amount_usd
        else:
            open_count += 1

    for trade_id, result, amount_usd, price in whale_rows:
        _tally(trade_id, result, amount_usd or 0.0, price or 0.0, is_sell=False)
    for trade_id, result, amount_usd, price, side in watched_rows:
        _tally(trade_id, result, amount_usd or 0.0, price or 0.0,
              is_sell=(side or "").strip().upper() == "SELL")

    decided = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "open": open_count,
        "win_rate": (wins / decided) if decided else None,
        "realized_pnl": realized_pnl,
    }


def latest_pnl_snapshot(session, address: str):
    """Most recent PnL snapshot for a wallet, or None."""
    return (
        session.query(WalletPnlSnapshot)
        .filter(WalletPnlSnapshot.address == address.lower())
        .order_by(WalletPnlSnapshot.taken_at.desc())
        .first()
    )


def record_pnl_snapshot(session, address: str, *, open_value: float,
                        unrealized: float, realized: float, wins: int,
                        losses: int, min_gap_hours: float = 6.0) -> bool:
    """Write a PnL snapshot for a wallet, at most once per min_gap_hours.
    Returns True if a snapshot was written, False if skipped (too soon)."""
    address = address.lower()
    last = latest_pnl_snapshot(session, address)
    now = datetime.now(timezone.utc)
    if last and last.taken_at:
        last_taken = last.taken_at
        if last_taken.tzinfo is None:
            last_taken = last_taken.replace(tzinfo=timezone.utc)
        if (now - last_taken).total_seconds() < min_gap_hours * 3600:
            return False
    session.add(WalletPnlSnapshot(
        address=address, taken_at=now, open_value=open_value,
        unrealized=unrealized, realized=realized, wins=wins, losses=losses,
    ))
    session.commit()
    return True


def pnl_snapshot_history(session, address: str, limit: int = 60) -> list:
    """A wallet's PnL snapshots, oldest first (for charting trend over time)."""
    rows = (
        session.query(WalletPnlSnapshot)
        .filter(WalletPnlSnapshot.address == address.lower())
        .order_by(WalletPnlSnapshot.taken_at.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


def get_stats(session) -> dict:
    """Aggregate stats for the dashboard."""
    total_trades = session.query(func.count(WhaleTrade.id)).scalar() or 0
    total_volume = session.query(func.coalesce(func.sum(WhaleTrade.amount_usd), 0)).scalar()
    unique_wallets = session.query(func.count(Wallet.address)).scalar() or 0
    biggest = session.query(func.coalesce(func.max(WhaleTrade.amount_usd), 0)).scalar()
    return {
        "total_trades": total_trades,
        "total_volume": float(total_volume),
        "unique_wallets": unique_wallets,
        "biggest_trade": float(biggest),
    }
