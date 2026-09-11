"""Read-only reference-market consensus and target-book divergence metrics."""

from collections import defaultdict
from statistics import median

from app.math_utils import american_to_probability, no_vig_probabilities
from app.models import MarketSnapshot, Side


def _player_key(name: str) -> str:
    """Normalize harmless display differences without fuzzy identity matching."""
    return " ".join(name.split()).casefold()


def _book_quote(rows: list[MarketSnapshot]) -> dict | None:
    lines = {row.line for row in rows if row.line is not None}
    if len(lines) != 1:
        return None
    prices = {"over": None, "under": None}
    probabilities = {"over": None, "under": None}
    for row in rows:
        if row.side not in (Side.OVER, Side.UNDER) or row.line not in lines:
            continue
        side = row.side.value
        if prices[side] is not None:  # ambiguous duplicate outcome
            return None
        prices[side] = row.american_odds
        probabilities[side] = (
            american_to_probability(row.american_odds)
            if row.american_odds is not None else None
        )
    if prices["over"] is None and prices["under"] is None:
        return None
    no_vig = (no_vig_probabilities(prices["over"], prices["under"])
              if prices["over"] is not None and prices["under"] is not None else (None, None))
    updated = [r.source_updated_at_utc for r in rows if r.source_updated_at_utc]
    observed = [r.observed_at_utc for r in rows]
    return {
        "line": next(iter(lines)),
        "over_odds": prices["over"],
        "under_odds": prices["under"],
        "over_implied_probability": probabilities["over"],
        "under_implied_probability": probabilities["under"],
        "over_no_vig_probability": no_vig[0],
        "under_no_vig_probability": no_vig[1],
        "source_updated_at_utc": max(updated).isoformat() if updated else None,
        "observed_at_utc": max(observed).isoformat() if observed else None,
    }


def market_divergences(
    rows: list[MarketSnapshot],
    *,
    reference_bookmakers: tuple[str, ...],
    target_bookmaker: str = "hardrockbet",
    min_reference_books: int = 2,
) -> list[dict]:
    """Build a sanitized line comparison; no profitability claim is made."""
    if min_reference_books < 1:
        raise ValueError("min_reference_books must be at least one")
    grouped = defaultdict(list)
    display_names = {}
    for row in rows:
        key = (row.game_id, _player_key(row.player_name), row.market_type.value)
        grouped[key].append(row)
        display_names.setdefault(key, " ".join(row.player_name.split()))

    report = []
    for key, items in grouped.items():
        by_book = defaultdict(list)
        for row in items:
            by_book[row.source].append(row)
        target = _book_quote(by_book.get(target_bookmaker, []))
        if target is None:
            continue
        reference_quotes = {
            book: quote
            for book in reference_bookmakers
            if (quote := _book_quote(by_book.get(book, []))) is not None
        }
        if len(reference_quotes) < min_reference_books:
            continue
        reference_lines = [q["line"] for q in reference_quotes.values()]
        reference_median = float(median(reference_lines))
        difference = float(target["line"] - reference_median)
        report.append({
            "game_id": key[0],
            "game": next((r.event_name for r in items if r.event_name), key[0]),
            "home_team": next((r.home_team for r in items if r.home_team), None),
            "away_team": next((r.away_team for r in items if r.away_team), None),
            "player": display_names[key],
            "market": key[2],
            "hard_rock_line": target["line"],
            "hard_rock_over_odds": target["over_odds"],
            "hard_rock_under_odds": target["under_odds"],
            "hard_rock_over_implied_probability": target["over_implied_probability"],
            "hard_rock_under_implied_probability": target["under_implied_probability"],
            "hard_rock_over_no_vig_probability": target["over_no_vig_probability"],
            "hard_rock_under_no_vig_probability": target["under_no_vig_probability"],
            "hard_rock_source_updated_at_utc": target["source_updated_at_utc"],
            "hard_rock_observed_at_utc": target["observed_at_utc"],
            "reference_books": reference_quotes,
            "median_reference_line": reference_median,
            "min_reference_line": min(reference_lines),
            "max_reference_line": max(reference_lines),
            "reference_line_range": max(reference_lines) - min(reference_lines),
            "reference_book_count": len(reference_quotes),
            "hard_rock_line_difference": difference,
            "consensus_direction": (
                "higher" if difference > 0 else "lower" if difference < 0 else "same"
            ),
            "comparison": _side_comparison(target, reference_quotes, reference_median),
        })
    return sorted(
        report,
        key=lambda item: (
            -abs(item["hard_rock_line_difference"]),
            item["game_id"],
            item["player"],
            item["market"],
        ),
    )


def _side_comparison(target: dict, references: dict[str, dict], line: float) -> dict:
    """Separate line value from price value; never infer expected value."""
    same_line = [q for q in references.values() if q["line"] == target["line"]]
    result = {}
    for side in ("over", "under"):
        target_odds = target[f"{side}_odds"]
        odds = [q[f"{side}_odds"] for q in same_line if q[f"{side}_odds"] is not None]
        reference_odds = float(median(odds)) if odds else None
        better_line = target["line"] < line if side == "over" else target["line"] > line
        result[side] = {
            "better_line": better_line,
            "same_line_price_comparable": bool(odds) and target_odds is not None,
            "median_same_line_reference_odds": reference_odds,
            "better_price": (target_odds > reference_odds
                             if reference_odds is not None and target_odds is not None else None),
        }
    return result
