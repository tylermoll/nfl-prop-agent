from datetime import datetime, timezone

import pytest

from app.consensus import market_divergences
from app.models import MarketSnapshot, MarketType, Side


REFERENCES = ("draftkings", "fanduel", "betmgm", "williamhill_us")


def quote(book, line, over=-110, under=-110, player="Puka Nacua", market=MarketType.RECEPTION_YDS):
    return [
        MarketSnapshot(
            source=book, source_market_id=f"{book}:{side.value}", game_id="game-1",
            player_name=player, market_type=market, line=line, side=side,
            american_odds=odds, observed_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for side, odds in ((Side.OVER, over), (Side.UNDER, under))
    ]


def test_matches_same_event_normalized_player_and_market_across_books():
    rows = quote("hardrockbet", 78.5, -115, -105, " Puka   Nacua ")
    rows += quote("draftkings", 80.5, player="puka nacua")
    rows += quote("fanduel", 80.5)
    result = market_divergences(rows, reference_bookmakers=REFERENCES)
    assert len(result) == 1
    assert result[0]["player"] == "Puka Nacua"
    assert result[0]["hard_rock_over_odds"] == -115
    assert result[0]["consensus_direction"] == "lower"


def test_median_uses_one_line_per_reference_book_with_different_lines():
    rows = quote("hardrockbet", 82.5)
    rows += quote("draftkings", 78.5) + quote("fanduel", 79.5)
    rows += quote("betmgm", 80.5) + quote("williamhill_us", 84.5)
    item = market_divergences(rows, reference_bookmakers=REFERENCES)[0]
    assert item["median_reference_line"] == 80.0
    assert item["reference_book_count"] == 4
    assert item["hard_rock_line_difference"] == 2.5
    assert item["reference_books"]["draftkings"]["over_odds"] == -110


def test_missing_books_and_minimum_coverage():
    rows = quote("hardrockbet", 78.5) + quote("draftkings", 80.5)
    assert market_divergences(rows, reference_bookmakers=REFERENCES) == []
    result = market_divergences(rows, reference_bookmakers=REFERENCES, min_reference_books=1)
    assert result[0]["reference_book_count"] == 1
    assert set(result[0]["reference_books"]) == {"draftkings"}


def test_malformed_or_ambiguous_outcomes_do_not_contribute():
    rows = quote("hardrockbet", 78.5) + quote("draftkings", 80.5)
    malformed = quote("fanduel", 81.5)
    malformed[1].line = 82.5
    rows += malformed
    assert market_divergences(rows, reference_bookmakers=REFERENCES) == []


def test_report_is_ranked_by_absolute_divergence_and_contains_no_actions():
    rows = quote("hardrockbet", 78.5) + quote("draftkings", 80.5) + quote("fanduel", 80.5)
    report = market_divergences(rows, reference_bookmakers=REFERENCES)
    serialized = repr(report).lower()
    assert "wager" not in serialized and "order" not in serialized and "recommend" not in serialized
    assert report[0]["hard_rock_over_implied_probability"] == pytest.approx(0.5238095238)


def test_invalid_minimum_coverage_is_rejected():
    with pytest.raises(ValueError, match="at least one"):
        market_divergences([], reference_bookmakers=REFERENCES, min_reference_books=0)

def test_dispersion_no_vig_freshness_and_same_line_price_are_separate():
    rows = quote("hardrockbet", 80.5, -105, -115)
    rows += quote("draftkings", 80.5, -110, -110)
    rows += quote("fanduel", 82.5, -120, 100)
    item = market_divergences(rows, reference_bookmakers=REFERENCES)[0]
    assert (item["min_reference_line"], item["max_reference_line"], item["reference_line_range"]) == (80.5, 82.5, 2)
    assert item["reference_books"]["draftkings"]["over_no_vig_probability"] == pytest.approx(.5)
    assert item["comparison"]["over"]["better_price"] is True
    assert item["comparison"]["over"]["better_line"] is True
    assert item["hard_rock_observed_at_utc"] == "2026-01-01T00:00:00+00:00"
