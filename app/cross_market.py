"""Unified, read-only sportsbook/Kalshi player-prop opportunity scanner."""

from collections import defaultdict
from statistics import median

from app.consensus import _book_quote, _player_key
from app.models import MarketSnapshot


def _event_key(row: MarketSnapshot) -> str | None:
    """Return a strict cross-provider event identity (never guess from a ticker)."""
    if row.event_name:
        return _player_key(row.event_name)
    if row.away_team and row.home_team:
        return f"{_player_key(row.away_team)} at {_player_key(row.home_team)}"
    return None


def _curve(rows: list[MarketSnapshot], hard_rock_line: float) -> dict:
    points = []
    for row in sorted(rows, key=lambda item: item.line if item.line is not None else float("inf")):
        spread = (row.yes_ask - row.yes_bid
                  if row.yes_bid is not None and row.yes_ask is not None else None)
        points.append({
            "threshold": row.line, "yes_bid": row.yes_bid, "yes_ask": row.yes_ask,
            "midpoint": row.contract_price, "spread": spread,
            "last_price": row.last_traded_price, "observed_at_utc": row.observed_at_utc.isoformat(),
            "source_market_id": row.source_market_id,
        })
    comparable = [p for p in points if p["midpoint"] is not None]
    monotonic = all(a["midpoint"] >= b["midpoint"] for a, b in zip(comparable, comparable[1:]))
    below = [p for p in points if p["threshold"] < hard_rock_line]
    above = [p for p in points if p["threshold"] > hard_rock_line]
    exact = next((p for p in points if p["threshold"] == hard_rock_line), None)
    return {
        "points": points,
        "thresholds": [p["threshold"] for p in points],
        "is_monotonic_non_increasing": monotonic,
        "nearest_below": below[-1] if below else None,
        "nearest_exact": exact,
        "nearest_above": above[0] if above else None,
        "interpolated_probability": None,
    }


def unified_opportunity_scan(
    rows: list[MarketSnapshot], *, reference_bookmakers: tuple[str, ...],
    target_bookmaker: str = "hardrockbet", min_reference_books: int = 1,
    material_line_difference: float = .5, material_probability_difference: float = .05,
) -> dict:
    """Match exact identities and thresholds and emit descriptive signals only."""
    if min_reference_books < 1:
        raise ValueError("min_reference_books must be at least one")
    sports, kalshi = defaultdict(list), defaultdict(list)
    for row in rows:
        event = _event_key(row)
        if event is None or row.line is None:
            continue
        key = (event, _player_key(row.player_name), row.market_type.value)
        (kalshi if row.source == "kalshi" else sports)[key].append(row)

    opportunities, unmatched_hard_rock = [], []
    matched_kalshi_ids: set[str] = set()
    for key, items in sports.items():
        by_book = defaultdict(list)
        for row in items:
            by_book[row.source].append(row)
        hard_rock = _book_quote(by_book.get(target_bookmaker, []))
        if not hard_rock:
            continue
        refs = {book: quote for book in reference_bookmakers
                if (quote := _book_quote(by_book.get(book, []))) is not None}
        if len(refs) < min_reference_books:
            target_row = next(r for r in items if r.source == target_bookmaker)
            unmatched_hard_rock.append({
                "game": target_row.event_name or key[0], "player": target_row.player_name,
                "market": key[2], "threshold": hard_rock["line"],
                "reason": "insufficient_reference_books",
            })
            continue
        curve_rows = kalshi.get(key, [])
        curve = _curve(curve_rows, hard_rock["line"])
        exact = next((r for r in curve_rows if r.line == hard_rock["line"]), None)
        if exact is None:
            unmatched_hard_rock.append({
                "game": key[0], "player": next(r.player_name for r in items if r.source == target_bookmaker),
                "market": key[2], "threshold": hard_rock["line"],
                "reason": "no_exact_kalshi_threshold",
            })
            continue
        matched_kalshi_ids.add(exact.source_market_id)
        reference_lines = [quote["line"] for quote in refs.values()]
        reference_median = float(median(reference_lines)) if reference_lines else None
        same_line_probs = [quote["over_no_vig_probability"] for quote in refs.values()
                           if quote["line"] == hard_rock["line"] and quote["over_no_vig_probability"] is not None]
        consensus_probability = float(median(same_line_probs)) if same_line_probs else None
        hr_probability = hard_rock["over_no_vig_probability"]
        line_diff = (hard_rock["line"] - reference_median) if reference_median is not None else None
        midpoint_diff = (exact.contract_price - hr_probability
                         if exact.contract_price is not None and hr_probability is not None else None)
        outside_executable = None
        if hr_probability is not None and exact.yes_bid is not None and exact.yes_ask is not None:
            outside_executable = (hr_probability < exact.yes_bid - material_probability_difference or
                                  hr_probability > exact.yes_ask + material_probability_difference)
        # A lower reference line points Under relative to Hard Rock; a higher
        # reference line points Over. This is descriptive, not an action label.
        line_direction = None if line_diff in (None, 0) else ("under" if line_diff > 0 else "over")
        price_direction = None if midpoint_diff in (None, 0) else ("over" if midpoint_diff > 0 else "under")
        signals = {
            "line_divergence": bool(line_diff is not None and abs(line_diff) >= material_line_difference),
            "price_divergence": bool(
                (midpoint_diff is not None and abs(midpoint_diff) >= material_probability_difference)
                or outside_executable),
            "cross_market_confirmation": bool(line_direction and price_direction and line_direction == price_direction),
            "cross_market_conflict": bool(line_direction and price_direction and line_direction != price_direction),
            "sportsbook_direction": line_direction, "kalshi_direction": price_direction,
        }
        opportunities.append({
            "player": next(r.player_name for r in items if r.source == target_bookmaker),
            "game": next(r.event_name for r in items if r.source == target_bookmaker and r.event_name),
            "market": key[2], "hard_rock_line": hard_rock["line"],
            "hard_rock_over_odds": hard_rock["over_odds"], "hard_rock_under_odds": hard_rock["under_odds"],
            "hard_rock_over_implied_probability": hard_rock["over_implied_probability"],
            "hard_rock_under_implied_probability": hard_rock["under_implied_probability"],
            "hard_rock_over_no_vig_probability": hr_probability,
            "hard_rock_under_no_vig_probability": hard_rock["under_no_vig_probability"],
            "reference_median_line": reference_median,
            "reference_over_no_vig_consensus_probability_at_hard_rock_line": consensus_probability,
            "contributing_reference_books": sorted(refs), "reference_books": refs,
            "kalshi_yes_bid": exact.yes_bid, "kalshi_yes_ask": exact.yes_ask,
            "kalshi_midpoint": exact.contract_price,
            "kalshi_spread": (exact.yes_ask - exact.yes_bid
                              if exact.yes_bid is not None and exact.yes_ask is not None else None),
            "kalshi_last_price": exact.last_traded_price,
            "kalshi_observed_at_utc": exact.observed_at_utc.isoformat(),
            "kalshi_volume": exact.volume, "kalshi_open_interest": exact.open_interest,
            "kalshi_curve": curve, "signals": signals,
            "price_comparison": {"midpoint_difference": midpoint_diff,
                                 "hard_rock_probability_outside_executable_range": outside_executable,
                                 "midpoint_is_executable": False},
        })

    unmatched_kalshi = [{
        "source_market_id": row.source_market_id, "game": row.event_name,
        "player": row.player_name, "market": row.market_type.value, "threshold": row.line,
        "reason": "no_exact_hard_rock_prop",
    } for row in rows if row.source == "kalshi" and row.source_market_id not in matched_kalshi_ids]
    return {"opportunities": opportunities, "unmatched_hard_rock_props": unmatched_hard_rock,
            "unmatched_kalshi_contracts": unmatched_kalshi}


def cross_market_comparisons(rows: list[MarketSnapshot], **kwargs) -> list[dict]:
    """Backward-compatible exact-match view."""
    return unified_opportunity_scan(rows, **kwargs)["opportunities"]
