from copy import deepcopy
from datetime import datetime, timedelta, timezone
from app.pregame_context import evaluate_context

NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
def obs(**kw):
    x={"model_probability":.58,"model_over_probability":.58,"model_under_probability":.42,"probability_edge_pp":5.6,"model_point_prediction":271.2,"canonical_market":"player_reception_yds","reference_book_coverage":3,"same_threshold_reference_probability":.55}; x.update(kw); return x
def snap(**kw):
    x={"context_id":"ctx","as_of_utc":NOW-timedelta(minutes=10),"collected_at_utc":NOW,"weather":{"venue_type":"outdoor","temperature_f":70,"wind_mph":5,"precipitation_probability":.1},"injuries":{"player_designation":"none","practice_participation":"full","status_confidence":"confirmed"},"role":{"depth_chart_position":"WR1","starter":True,"season_week":1,"change_status":"none"},"sources":{"weather":"NOAA"}}; x.update(kw); return x

def test_indoor_game():
    r=evaluate_context(obs(),snap(weather={"venue_type":"indoor","wind_mph":40}),now=NOW)
    assert r["flags"]["indoor_weather_irrelevant"] and not r["flags"]["wind_high"]
def test_high_wind_outdoor_game():
    r=evaluate_context(obs(),snap(weather={"venue_type":"outdoor","wind_mph":22,"wind_gust_mph":34}),now=NOW)
    assert r["flags"]["wind_high"] and r["dimensions"]["weather_context"]["label"]=="concern"
def test_injury_designation_and_qb_absence():
    assert evaluate_context(obs(),snap(injuries={"player_designation":"questionable"}),now=NOW)["flags"]["injury_concern"]
    assert evaluate_context(obs(),snap(injuries={"player_designation":"none","qb_status":"out"}),now=NOW)["flags"]["qb_absence_affects_receiver"]
def test_confirmed_and_uncertain_role_change():
    a=evaluate_context(obs(),snap(role={"material_change":True,"change_status":"confirmed"}),now=NOW)
    b=evaluate_context(obs(),snap(role={"material_change":True,"change_status":"reported","uncertain":True}),now=NOW)
    assert a["flags"]["confirmed_role_change"] and a["dimensions"]["role_context"]["label"]=="confirmed_change"
    assert not b["flags"]["confirmed_role_change"] and b["flags"]["role_uncertainty"]
def test_prior_season_role_mismatch_flag():
    role={"season_week":1,"prior_season_usage_dependency":True,"current_team_mismatch":True,"change_status":"confirmed","material_change":True}
    assert evaluate_context(obs(),snap(role=role),now=NOW)["flags"]["historical_role_may_be_stale"]
def test_missing_external_and_stale_context():
    assert evaluate_context(obs(),None,now=NOW)["evidence_quality"]=="insufficient_context"
    r=evaluate_context(obs(),snap(as_of_utc=NOW-timedelta(days=2)),now=NOW)
    assert r["dimensions"]["data_freshness"]["label"]=="stale" and r["evidence_quality"]=="weak"
def test_preserves_probabilities_edge_and_inputs():
    o,c=obs(),snap(); bo,bc=deepcopy(o),deepcopy(c); r=evaluate_context(o,c,now=NOW)
    assert o==bo and c==bc
    assert r.keys().isdisjoint({"model_probability","model_over_probability","model_under_probability","probability_edge_pp","model_point_prediction"})
def test_no_wagering_actions():
    import app.pregame_context as m
    assert not any(w in n.lower() for n in dir(m) for w in ("wager","bet","order","execute"))
