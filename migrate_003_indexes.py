#!/usr/bin/env python3
"""
Migration 003 — composite indexes on whale_trades for hot query paths.

market_side_whales() (the smart-money consensus check) runs on every whale
trade, filtered by (condition_id, side, traded_at) — and wallet-scoped
queries filter by (wallet, traded_at). Neither was covered by a composite
index before. CREATE INDEX IF NOT EXISTS works on both SQLite and Postgres —
idempotent, safe to run more than once.

    railway ssh --service tracker -- /opt/venv/bin/python migrate_003_indexes.py
"""

from sqlalchemy import create_engine, text

import db

engine = create_engine(db.get_db_url())

with engine.begin() as conn:
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_whale_trades_condition_side_traded "
        "ON whale_trades (condition_id, side, traded_at)"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_whale_trades_wallet_traded "
        "ON whale_trades (wallet, traded_at)"))

print("Migration 003 complete — composite indexes created (or already present).")
