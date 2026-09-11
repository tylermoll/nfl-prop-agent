def american_to_probability(odds: int) -> float:
    """Convert raw American odds to their vig-inclusive implied probability."""
    if not isinstance(odds, int) or isinstance(odds, bool):
        raise TypeError("American odds must be an integer")
    if odds == 0:
        raise ValueError("American odds cannot be zero")
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)

def no_vig_probabilities(over_odds: int, under_odds: int) -> tuple[float, float]:
    po = american_to_probability(over_odds)
    pu = american_to_probability(under_odds)
    total = po + pu
    return po / total, pu / total
