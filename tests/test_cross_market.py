from datetime import datetime, timezone
import pytest

from app.cross_market import unified_opportunity_scan
from app.consensus import _player_key
from app.identities import canonical_event_identity
from app.models import MarketSnapshot, MarketType, Side
from app.providers.kalshi import normalize_kalshi_market

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
GAME = "Los Angeles Rams at San Francisco 49ers"


def sportsbook(book, side, odds, *, line=79.5, player="Puka Nacua", game=GAME,
               away="Los Angeles Rams", home="San Francisco 49ers"):
    return MarketSnapshot(
        source=book, source_market_id=f"{book}-{side}-{line}", game_id="provider-native-id",
        event_name=game, away_team=away, home_team=home,
        player_name=player, market_type=MarketType.RECEPTION_YDS,
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
    assert result["unmatched_kalshi_contracts"][0]["reason"] == "no_exact_hard_rock_threshold"


def test_real_style_kalshi_ticker_matches_odds_api_teams_without_event_title():
    kalshi = normalize_kalshi_market({
        "ticker": "KXNFLRECYDS-LARSF-PNACUA-80",
        "event_ticker": "KXNFLRECYDS-LARSF",
        "series_ticker": "KXNFLRECYDS",
        "title": "Puka Nacua: 80+ receiving yards",
        "yes_bid": 55, "yes_ask": 61,
    }, NOW)
    rows = [sportsbook("hardrockbet", Side.OVER, -110),
            sportsbook("hardrockbet", Side.UNDER, -110),
            sportsbook("draftkings", Side.OVER, -110),
            sportsbook("draftkings", Side.UNDER, -110), kalshi]
    result = unified_opportunity_scan(rows, reference_bookmakers=("draftkings",))
    assert len(result["opportunities"]) == 1
    assert (kalshi.away_team, kalshi.home_team) == (
        "Los Angeles Rams", "San Francisco 49ers")


def test_reversed_home_away_still_has_same_unordered_canonical_matchup():
    assert canonical_event_identity("San Francisco 49ers", "Los Angeles Rams") == (
        canonical_event_identity("LAR", "SF"))


@pytest.mark.parametrize("name", [
    "Patrick Mahomes", "Jayden Daniels", "Lamar Jackson", "Emeka Egbuka", "Kenny Gainwell",
])
def test_observed_player_names_normalize_identically_across_providers(name):
    assert _player_key(f"  {name.upper()}  ") == _player_key(name)


def test_safe_player_punctuation_normalization_without_fuzzy_spelling():
    assert _player_key("D.J. Moore") == _player_key("DJ Moore")
    assert _player_key("Patrick Mahomes") != _player_key("Pat Mahomes")


@pytest.mark.parametrize(("title", "market", "expected"), [
    ("Jayden Daniels: 200+ passing yards", MarketType.PASS_YDS, 199.5),
    ("Emeka Egbuka: 50+ receiving yards", MarketType.RECEPTION_YDS, 49.5),
    ("Kenny Gainwell: 5+ receptions", MarketType.RECEPTIONS, 4.5),
])
def test_integer_kalshi_thresholds_are_exact_float_sportsbook_lines(title, market, expected):
    row = normalize_kalshi_market({
        "ticker": "KXNFL-LARSF-PROP", "event_ticker": "KXNFL-LARSF",
        "series_ticker": "KXNFL", "title": title,
    }, NOW)
    assert row.market_type == market and row.line == expected
    assert isinstance(row.line, float) and row.line == float(expected)


def test_diagnostic_funnels_and_staged_unmatched_reasons():
    exact = contract(79.5, .5, .6, ticker="EXACT")
    absent_threshold = contract(89.5, .4, .5, ticker="THRESHOLD")
    wrong_event = contract(79.5, .4, .5, game="Los Angeles Rams at Seattle Seahawks", ticker="EVENT")
    wrong_player = contract(79.5, .4, .5, player="Cooper Kupp", ticker="PLAYER")
    wrong_market = normalize_kalshi_market({
        "ticker": "MARKET", "event_ticker": "KXNFL-LARSF", "event_title": GAME,
        "title": "Puka Nacua: 5+ receptions",
    }, NOW)
    rows = [sportsbook("hardrockbet", Side.OVER, -110),
            sportsbook("hardrockbet", Side.UNDER, -110),
            sportsbook("draftkings", Side.OVER, -110),
            sportsbook("draftkings", Side.UNDER, -110),
            exact, absent_threshold, wrong_event, wrong_player, wrong_market]
    result = unified_opportunity_scan(rows, reference_bookmakers=("draftkings",))
    funnel = result["diagnostic_funnel"]
    assert (funnel["candidate_relationships"], funnel["after_event_match"],
            funnel["after_player_match"], funnel["after_market_match"],
            funnel["after_threshold_match"]) == (5, 4, 3, 2, 1)
    assert funnel["dimension_audit_market_player_event_threshold"] == {
        "after_canonical_market_match": 4, "after_player_name_match": 3,
        "after_event_game_match": 2, "after_exact_normalized_threshold_match": 1,
    }
    reasons = {item["source_market_id"]: item["reason"]
               for item in result["unmatched_kalshi_contracts"]}
    assert reasons == {
        "THRESHOLD": "no_exact_hard_rock_threshold",
        "EVENT": "no_matching_hard_rock_event",
        "PLAYER": "no_matching_hard_rock_player",
        "MARKET": "no_matching_hard_rock_market",
    }


@pytest.mark.parametrize(("candidate", "expected"), [
    (contract(79.5, .4, .5, player="Cooper Kupp"), "no_matching_kalshi_player"),
    (contract(79.5, .4, .5, game="Los Angeles Rams at Seattle Seahawks"),
     "no_matching_kalshi_event"),
    (normalize_kalshi_market({
        "ticker": "KXNFL-LARSF-REC", "event_ticker": "KXNFL-LARSF",
        "event_title": GAME, "title": "Puka Nacua: 5+ receptions",
    }, NOW), "no_matching_kalshi_market"),
    (contract(89.5, .4, .5), "no_exact_kalshi_threshold"),
])
def test_hard_rock_staged_unmatched_reasons(candidate, expected):
    rows = [sportsbook("hardrockbet", Side.OVER, -110),
            sportsbook("hardrockbet", Side.UNDER, -110),
            sportsbook("draftkings", Side.OVER, -110),
            sportsbook("draftkings", Side.UNDER, -110), candidate]
    result = unified_opportunity_scan(rows, reference_bookmakers=("draftkings",))
    assert result["unmatched_hard_rock_props"][0]["reason"] == expected


def test_no_wagering_or_recommendation_labels():
    serialized = str(scan())
    assert all(label not in serialized for label in ("BET", "PASS", "LOCK", "positive EV", "profitable"))
