#!/usr/bin/env python3
"""
Migration 002 — add the `result` column to whale_trades and watched_trades.

Powers win/loss tracking: "" = still open, "WIN" / "LOSS" once
resolve_markets.py settles the market. Works on both SQLite and Postgres
(plain ADD COLUMN, no type change) — idempotent, safe to run more than once.

New tables this release needs (markets, wallet_pnl_snapshots) don't need a
migration — db.init_db() creates any missing table automatically. Only
columns added to *existing* tables need a script like this one.

    railway ssh --service tracker -- /opt/venv/bin/python migrate_002_trade_results.py
"""

from sqlalchemy import create_engine, text, inspect

import db

engine = create_engine(db.get_db_url())
inspector = inspect(engine)

TABLES = ["whale_trades", "watched_trades"]

with engine.begin() as conn:
    for table in TABLES:
        existing_cols = {c["name"] for c in inspector.get_columns(table)}
        if "result" in existing_cols:
            print(f"{table}.result already exists — skipping.")
            continue
        conn.execute(text(
            f"ALTER TABLE {table} ADD COLUMN result VARCHAR(8) DEFAULT ''"))
        print(f"{table}.result added.")

print("Migration 002 complete.")
