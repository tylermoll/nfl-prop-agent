"""Read-only matching and comparison of sportsbook and Kalshi observations."""

from collections import defaultdict
from statistics import median

from app.consensus import _book_quote, _player_key
from app.models import MarketSnapshot


def cross_market_comparisons(rows: list[MarketSnapshot], *, reference_bookmakers: tuple[str, ...],
                             target_bookmaker: str = "hardrockbet") -> list[dict]:
    sports = defaultdict(list)
    for row in rows:
        if row.source != "kalshi":
            sports[(_player_key(row.player_name), row.market_type.value, row.line)].append(row)
    output = []
    for kalshi in (row for row in rows if row.source == "kalshi"):
        candidates = sports.get((_player_key(kalshi.player_name), kalshi.market_type.value, kalshi.line), [])
        # Event names are compared exactly after whitespace/case normalization when both exist.
        candidates = [r for r in candidates if not (kalshi.event_name and r.event_name) or
                      _player_key(kalshi.event_name) == _player_key(r.event_name)]
        by_book = defaultdict(list)
        for row in candidates:
            by_book[row.source].append(row)
        hard_rock = _book_quote(by_book[target_bookmaker])
        refs = {b: q for b in reference_bookmakers if (q := _book_quote(by_book[b]))}
        if not hard_rock or not refs:
            continue
        reference_probabilities = [q["over_no_vig_probability"] for q in refs.values()
                                   if q["over_no_vig_probability"] is not None]
        consensus_probability = float(median(reference_probabilities)) if reference_probabilities else None
        midpoint = kalshi.contract_price
        direction = None if midpoint is None or consensus_probability is None else (
            "same" if (midpoint >= .5) == (consensus_probability >= .5) else "opposite")
        output.append({
            "player": kalshi.player_name, "game": kalshi.event_name or candidates[0].event_name or candidates[0].game_id,
            "market": kalshi.market_type.value, "threshold": kalshi.line,
            "hard_rock": hard_rock, "sportsbook_reference_consensus": {
                "books": refs, "median_over_no_vig_probability": consensus_probability,
            }, "kalshi_bid_ask_midpoint": midpoint,
            "kalshi_spread": (round(kalshi.yes_ask - kalshi.yes_bid, 10)
                              if kalshi.yes_ask is not None and kalshi.yes_bid is not None else None),
            "kalshi_volume": kalshi.volume, "kalshi_open_interest": kalshi.open_interest,
            "direction_agreement": direction,
        })
    return output
