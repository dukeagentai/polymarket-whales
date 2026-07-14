"""settle_market_trades — the core of the resolution/settlement feature.
Covers all four BUY/SELL x winner/loser combinations, plus idempotency."""

from datetime import datetime, timezone

import db


def _now():
    return datetime.now(timezone.utc)


def test_settle_market_trades_all_combinations(session_factory):
    cid = "0xtest1"
    with session_factory() as s:
        db.upsert_market(s, condition_id=cid, title="Test Market")

        db.record_trade(s, trade_id="w-yes", condition_id=cid, market_title="M",
                        category="Crypto", side="YES", price=0.6, amount_usd=100,
                        wallet="0xaaa", traded_at=_now())
        db.record_trade(s, trade_id="w-no", condition_id=cid, market_title="M",
                        category="Crypto", side="NO", price=0.4, amount_usd=50,
                        wallet="0xbbb", traded_at=_now())

        db.record_watched_trade(s, trade_id="wt-buy-win", address="0xccc",
                                condition_id=cid, market_title="M", outcome="Yes",
                                side="BUY", price=0.6, amount_usd=80, traded_at=_now())
        db.record_watched_trade(s, trade_id="wt-sell-win", address="0xddd",
                                condition_id=cid, market_title="M", outcome="Yes",
                                side="SELL", price=0.6, amount_usd=30, traded_at=_now())
        db.record_watched_trade(s, trade_id="wt-buy-loss", address="0xeee",
                                condition_id=cid, market_title="M", outcome="No",
                                side="BUY", price=0.4, amount_usd=20, traded_at=_now())
        db.record_watched_trade(s, trade_id="wt-sell-loss", address="0xfff",
                                condition_id=cid, market_title="M", outcome="No",
                                side="SELL", price=0.4, amount_usd=10, traded_at=_now())

        counts = db.settle_market_trades(s, cid, "Yes")
        assert counts == {"whale_trades": 2, "watched_trades": 4}

        def result(model, trade_id):
            return s.query(model).filter_by(trade_id=trade_id).one().result

        assert result(db.WhaleTrade, "w-yes") == "WIN"
        assert result(db.WhaleTrade, "w-no") == "LOSS"
        assert result(db.WatchedTrade, "wt-buy-win") == "WIN"
        assert result(db.WatchedTrade, "wt-sell-win") == "LOSS"   # sold the winner
        assert result(db.WatchedTrade, "wt-buy-loss") == "LOSS"
        assert result(db.WatchedTrade, "wt-sell-loss") == "WIN"   # sold the loser


def test_settle_market_trades_is_idempotent(session_factory):
    cid = "0xtest2"
    with session_factory() as s:
        db.upsert_market(s, condition_id=cid, title="Test Market")
        db.record_trade(s, trade_id="idem-1", condition_id=cid, market_title="M",
                        category="Crypto", side="YES", price=0.5, amount_usd=100,
                        wallet="0xaaa", traded_at=_now())

        first = db.settle_market_trades(s, cid, "Yes")
        assert first == {"whale_trades": 1, "watched_trades": 0}

        second = db.settle_market_trades(s, cid, "Yes")
        assert second == {"whale_trades": 0, "watched_trades": 0}


def test_settle_market_trades_case_insensitive(session_factory):
    cid = "0xtest3"
    with session_factory() as s:
        db.upsert_market(s, condition_id=cid, title="Test Market")
        db.record_trade(s, trade_id="case-1", condition_id=cid, market_title="M",
                        category="Crypto", side="yes", price=0.5, amount_usd=100,
                        wallet="0xaaa", traded_at=_now())
        db.settle_market_trades(s, cid, "YES")
        assert s.query(db.WhaleTrade).filter_by(trade_id="case-1").one().result == "WIN"
