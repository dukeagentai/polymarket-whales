"""check_resolution — parsing Gamma /markets payloads to detect a winner.
Feeds canned payloads rather than hitting the live API."""

import resolve_markets as rm


def test_detects_winner_from_json_strings():
    market = {"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]'}
    assert rm.check_resolution(market) == "Yes"


def test_detects_winner_from_already_parsed_lists():
    market = {"closed": True, "outcomes": ["Yes", "No"], "outcomePrices": ["0", "1"]}
    assert rm.check_resolution(market) == "No"


def test_not_closed_is_unresolved():
    market = {"closed": False, "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]'}
    assert rm.check_resolution(market) == ""


def test_closed_missing_field_is_unresolved():
    market = {"closed": True}
    assert rm.check_resolution(market) == ""


def test_below_threshold_is_unresolved():
    """Prices haven't settled to ~1.0 yet — still trading, not resolved."""
    market = {"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["0.6", "0.4"]'}
    assert rm.check_resolution(market) == ""


def test_malformed_json_is_unresolved():
    market = {"closed": True, "outcomes": "not json", "outcomePrices": '["1", "0"]'}
    assert rm.check_resolution(market) == ""


def test_mismatched_lengths_is_unresolved():
    market = {"closed": True, "outcomes": '["Yes","No","Maybe"]', "outcomePrices": '["1","0"]'}
    assert rm.check_resolution(market) == ""


def test_multi_outcome_winner():
    market = {"closed": True, "outcomes": '["Lakers", "Celtics", "Draw"]',
             "outcomePrices": '["0", "1", "0"]'}
    assert rm.check_resolution(market) == "Celtics"
