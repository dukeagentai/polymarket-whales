"""Pure helper functions in main.py — trade parsing, ids, and filters."""

import main


def test_parse_trade_usd_size():
    assert main.parse_trade_usd_size({"size": "100", "price": "0.5"}) == 50.0
    assert main.parse_trade_usd_size({"size": "bad", "price": "0.5"}) == 0.0
    assert main.parse_trade_usd_size({}) == 0.0


def test_trade_unique_id_prefers_explicit_id():
    assert main.trade_unique_id({"id": "abc"}) == "abc"
    assert main.trade_unique_id({"trade_id": "xyz"}) == "xyz"


def test_trade_unique_id_falls_back_to_tx_hash_asset_size():
    t = {"transactionHash": "0xhash", "asset": "123", "size": "10"}
    assert main.trade_unique_id(t) == "0xhash-123-10"


def test_matches_filters_market_keyword():
    assert main.matches_filters("Fed rate hike", "0xabc", "Politics", ["Fed rate"], []) is True
    assert main.matches_filters("Some other market", "0xabc", "Politics", ["Fed rate"], []) is False


def test_matches_filters_condition_id_exact_match():
    assert main.matches_filters("Some title", "0xabc", "Politics", ["0xabc"], []) is True


def test_matches_filters_category_list():
    assert main.matches_filters("Title", "0xabc", ["Politics", "US"], [], ["politics"]) is True
    assert main.matches_filters("Title", "0xabc", ["Sports"], [], ["politics"]) is False


def test_matches_filters_empty_filters_match_everything():
    assert main.matches_filters("Anything", "0xabc", "Category", [], []) is True
