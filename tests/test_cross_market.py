from datetime import datetime, timezone

from app.cross_market import unified_opportunity_scan
from app.models import MarketSnapshot, MarketType, Side
from app.providers.kalshi import normalize_kalshi_market

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
GAME = "Los Angeles Rams at San Francisco 49ers"


def sportsbook(book, side, odds, *, line=79.5, player="Puka Nacua", game=GAME):
    return MarketSnapshot(
        source=book, source_market_id=f"{book}-{side}-{line}", game_id="provider-native-id",
        event_name=game, player_name=player, market_type=MarketType.RECEPTION_YDS,
        line=line, side=side, american_odds=odds, observed_at_utc=NOW,
    )


def contract(threshold, bid, ask, *, player="Puka Nacua", game=GAME, ticker=None):
    integer = int(threshold + .5)
    return normalize_kalshi_market({
        "ticker": ticker or f"KXNFL-{integer}", "event_ticker": "KXNFL-other-native-id",
        "event_title": game, "title": f"{player}: {integer}+ receiving yards",
        "yes_bid": bid * 100, "yes_ask": ask * 100, "last_price": (bid + ask) * 50,
    }, NOW)


def scan(*, hr_line=79.5, ref_line=74.5, kalshi_mid=.65, extra=()):
    rows = [sportsbook("hardrockbet", Side.OVER, -110, line=hr_line),
            sportsbook("hardrockbet", Side.UNDER, -110, line=hr_line),
            sportsbook("draftkings", Side.OVER, -110, line=ref_line),
            sportsbook("draftkings", Side.UNDER, -110, line=ref_line),
            contract(hr_line, kalshi_mid - .02, kalshi_mid + .02), *extra]
    return unified_opportunity_scan(rows, reference_bookmakers=("draftkings",))


def test_exact_event_player_market_and_threshold_match():
    result = scan()
    item = result["opportunities"][0]
    assert (item["player"], item["game"], item["market"], item["hard_rock_line"]) == (
        "Puka Nacua", GAME, "player_reception_yds", 79.5)
    assert item["kalshi_yes_bid"] == .63 and item["kalshi_yes_ask"] == .67
    assert item["contributing_reference_books"] == ["draftkings"]


def test_strict_event_and_player_matching_and_unmatched_reporting():
    wrong_event = contract(79.5, .4, .5, game="Other Team at Another Team", ticker="KXNFL-EVENT")
    wrong_player = contract(79.5, .4, .5, player="Cooper Kupp", ticker="KXNFL-PLAYER")
    rows = [sportsbook("hardrockbet", Side.OVER, -110), sportsbook("hardrockbet", Side.UNDER, -110),
            wrong_event, wrong_player]
    result = unified_opportunity_scan(rows, reference_bookmakers=("draftkings",))
    assert result["opportunities"] == []
    assert len(result["unmatched_hard_rock_props"]) == 1
    assert {x["source_market_id"] for x in result["unmatched_kalshi_contracts"]} == {"KXNFL-EVENT", "KXNFL-PLAYER"}


def test_curve_monotonicity_nearest_points_and_no_interpolation():
    result = scan(extra=(contract(69.5, .78, .82), contract(89.5, .38, .42)))
    curve = result["opportunities"][0]["kalshi_curve"]
    assert curve["thresholds"] == [69.5, 79.5, 89.5]
    assert curve["is_monotonic_non_increasing"] is True
    assert curve["nearest_below"]["threshold"] == 69.5
    assert curve["nearest_exact"]["threshold"] == 79.5
    assert curve["nearest_above"]["threshold"] == 89.5
    assert curve["interpolated_probability"] is None


def test_curve_detects_non_monotonic_prices():
    result = scan(extra=(contract(89.5, .78, .82),))
    assert result["opportunities"][0]["kalshi_curve"]["is_monotonic_non_increasing"] is False


def test_line_price_confirmation_and_conflict_signals():
    confirmed = scan(ref_line=84.5, kalshi_mid=.65)["opportunities"][0]
    assert confirmed["signals"]["line_divergence"] is True
    assert confirmed["signals"]["price_divergence"] is True
    assert confirmed["signals"]["cross_market_confirmation"] is True
    assert confirmed["signals"]["cross_market_conflict"] is False
    conflict = scan(ref_line=74.5, kalshi_mid=.65)["opportunities"][0]
    assert conflict["signals"]["cross_market_conflict"] is True


def test_executable_prices_are_distinct_from_descriptive_midpoint():
    item = scan(kalshi_mid=.65)["opportunities"][0]
    assert item["kalshi_yes_bid"] != item["kalshi_midpoint"] != item["kalshi_yes_ask"]
    assert item["price_comparison"]["midpoint_is_executable"] is False


def test_missing_exact_threshold_reports_both_unmatched_sides():
    rows = [sportsbook("hardrockbet", Side.OVER, -110), sportsbook("hardrockbet", Side.UNDER, -110),
            sportsbook("draftkings", Side.OVER, -110), sportsbook("draftkings", Side.UNDER, -110),
            contract(74.5, .6, .7)]
    result = unified_opportunity_scan(rows, reference_bookmakers=("draftkings",))
    assert not result["opportunities"]
    assert result["unmatched_hard_rock_props"][0]["reason"] == "no_exact_kalshi_threshold"
    assert result["unmatched_kalshi_contracts"][0]["reason"] == "no_exact_hard_rock_prop"


def test_no_wagering_or_recommendation_labels():
    serialized = str(scan())
    assert all(label not in serialized for label in ("BET", "PASS", "LOCK", "positive EV", "profitable"))
