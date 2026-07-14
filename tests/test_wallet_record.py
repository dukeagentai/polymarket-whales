"""wallet_record — dedupe across whale_trades/watched_trades and PnL math."""

from datetime import datetime, timezone

import db


def _now():
    return datetime.now(timezone.utc)


def test_wallet_record_dedupes_shared_trade_id(session_factory):
    """A watched wallet's whale-sized trade lands in both tables under the
    same trade_id — must only count once."""
    cid = "0xtest1"
    addr = "0xshared"
    with session_factory() as s:
        db.upsert_market(s, condition_id=cid, title="M")
        db.record_trade(s, trade_id="shared-1", condition_id=cid, market_title="M",
                        category="Crypto", side="YES", price=0.5, amount_usd=100,
                        wallet=addr, traded_at=_now())
        db.record_watched_trade(s, trade_id="shared-1", address=addr, condition_id=cid,
                                market_title="M", outcome="Yes", side="BUY", price=0.5,
                                amount_usd=100, traded_at=_now())
        db.settle_market_trades(s, cid, "Yes")

        record = db.wallet_record(s, addr)
        assert record["wins"] == 1
        assert record["losses"] == 0
        assert record["open"] == 0
        assert record["win_rate"] == 1.0
        # shares = 100 / 0.5 = 200; pnl = 200 - 100 = 100 — counted once, not twice
        assert record["realized_pnl"] == 100.0


def test_wallet_record_loss_pnl(session_factory):
    cid = "0xtest2"
    addr = "0xloser"
    with session_factory() as s:
        db.upsert_market(s, condition_id=cid, title="M")
        db.record_trade(s, trade_id="loss-1", condition_id=cid, market_title="M",
                        category="Crypto", side="NO", price=0.4, amount_usd=500,
                        wallet=addr, traded_at=_now())
        db.settle_market_trades(s, cid, "Yes")

        record = db.wallet_record(s, addr)
        assert record["wins"] == 0
        assert record["losses"] == 1
        assert record["realized_pnl"] == -500.0


def test_wallet_record_sell_excluded_from_pnl(session_factory):
    """SELL trades count toward win/loss but not realized_pnl (no cost basis)."""
    cid = "0xtest3"
    addr = "0xseller"
    with session_factory() as s:
        db.upsert_market(s, condition_id=cid, title="M")
        db.record_watched_trade(s, trade_id="sell-1", address=addr, condition_id=cid,
                                market_title="M", outcome="Yes", side="SELL", price=0.6,
                                amount_usd=300, traded_at=_now())
        db.settle_market_trades(s, cid, "Yes")

        record = db.wallet_record(s, addr)
        assert record["losses"] == 1  # sold the winner -> loss
        assert record["realized_pnl"] == 0.0


def test_wallet_record_open_trades_not_counted_as_win_or_loss(session_factory):
    cid = "0xtest4"
    addr = "0xopen"
    with session_factory() as s:
        db.upsert_market(s, condition_id=cid, title="M")
        db.record_trade(s, trade_id="open-1", condition_id=cid, market_title="M",
                        category="Crypto", side="YES", price=0.5, amount_usd=100,
                        wallet=addr, traded_at=_now())
        # market never resolved — result stays ""

        record = db.wallet_record(s, addr)
        assert record["wins"] == 0
        assert record["losses"] == 0
        assert record["open"] == 1
        assert record["win_rate"] is None
