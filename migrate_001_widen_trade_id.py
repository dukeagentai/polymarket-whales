#!/usr/bin/env python3
"""
Migration 001 — widen trade_id columns to varchar(256).

trade_id is txHash-asset-size (~150 chars); the original varchar(128)/(160)
made Postgres reject every insert. Idempotent — safe to run more than once.

    railway ssh --service tracker -- /opt/venv/bin/python migrate_001_widen_trade_id.py
"""

from sqlalchemy import create_engine, text

import db

engine = create_engine(db.get_db_url())

with engine.begin() as conn:
    conn.execute(text(
        "ALTER TABLE whale_trades ALTER COLUMN trade_id TYPE varchar(256)"))
    conn.execute(text(
        "ALTER TABLE watched_trades ALTER COLUMN trade_id TYPE varchar(256)"))
    rows = conn.execute(text(
        "SELECT table_name, character_maximum_length "
        "FROM information_schema.columns WHERE column_name = 'trade_id'"
    )).all()

for table, length in rows:
    print(f"{table}.trade_id -> varchar({length})")
print("Migration 001 complete.")
