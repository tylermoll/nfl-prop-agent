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

def american_to_decimal(odds: int) -> float:
    """Return total decimal payout (stake included) for an American price."""
    american_to_probability(odds)  # shared validation
    return 1 + (100 / abs(odds) if odds < 0 else odds / 100)

def hypothetical_return(probability: float, odds: int) -> float:
    """Expected net return for one dollar risked at the *offered* price."""
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between zero and one")
    return probability * (american_to_decimal(odds) - 1) - (1 - probability)
