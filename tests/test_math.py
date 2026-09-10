from app.math_utils import american_to_probability, no_vig_probabilities

def test_american_negative():
    assert round(american_to_probability(-110), 4) == 0.5238

def test_american_positive():
    assert round(american_to_probability(150), 4) == 0.4

def test_no_vig_sums_to_one():
    over, under = no_vig_probabilities(-110, -110)
    assert round(over + under, 10) == 1.0
    assert round(over, 3) == 0.5
