from app.models import MarketSnapshot, MarketType, Side

def test_contract_probability():
    m = MarketSnapshot(
        source="kalshi",
        source_market_id="x",
        game_id="g",
        player_name="Example Player",
        market_type=MarketType.RECEPTION_YDS,
        line=75.5,
        side=Side.YES,
        contract_price=0.61,
    )
    assert m.implied_probability == 0.61
