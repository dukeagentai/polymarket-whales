#!/usr/bin/env python3
"""
🐋 Polymarket Whale Tracker — database layer.

SQLite by default (zero setup), Postgres when DATABASE_URL is set
(Railway injects DATABASE_URL automatically when you add a Postgres service).
"""

import os
import logging
from datetime import datetime, timezone

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
    trade_id = Column(String(128), unique=True, index=True)
    condition_id = Column(String(128), index=True)
    market_title = Column(String(512))
    category = Column(String(128), index=True)
    side = Column(String(16))
    price = Column(Float)
    amount_usd = Column(Float)
    wallet = Column(String(64), index=True)
    traded_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


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


class WatchedTrade(Base):
    """Every trade made by a watched address, regardless of size."""
    __tablename__ = "watched_trades"

    id = Column(Integer, primary_key=True)
    trade_id = Column(String(160), unique=True, index=True)
    address = Column(String(64), index=True)
    condition_id = Column(String(128), index=True)
    market_title = Column(String(512))
    outcome = Column(String(128))  # YES / NO / France / Morocco / …
    side = Column(String(16))      # BUY / SELL
    price = Column(Float)
    amount_usd = Column(Float)
    traded_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


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
