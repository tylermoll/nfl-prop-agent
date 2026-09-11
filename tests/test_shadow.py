from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyRegressor

from app.math_utils import american_to_decimal, american_to_probability, hypothetical_return
from app.modeling.live import FootballArtifactScorer
from app.shadow import ShadowConfig, confirmation_flags, edge_bucket, make_observation, settle
from app.shadow_reporting import aggregate, maximum_drawdown
from app.shadow_storage import ShadowStore

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)

def score():
    return SimpleNamespace(over_probability=.6, under_probability=.4, model_version="artifact-1",
        point_prediction=250., feature_built_at_utc=NOW-timedelta(minutes=2),
        uncertainty_method="prediction_conditional_empirical_residual_ecdf", uncertainty_version="1",
        residual_bucket=2, uncertainty_scale=21.)

def observation(at=NOW, odds=-110, side="over"):
    return make_observation(observed_at_utc=at, kickoff_utc=NOW+timedelta(hours=1), game_id="g1",
        player_id="p1", player_name="Player", team="A", opponent="B", canonical_market="player_pass_yds",
        line=249.5, side=side, offered_odds=odds, model_score=score(),
        reference_context={"exact_threshold_over_no_vig_probability": .55},
        kalshi_context={"midpoint": .56, "yes_bid": .54, "yes_ask": .58}, source_ids={"hardrock": "q1"})

@pytest.mark.parametrize("odds,expected", [(-110, 110/210), (150, 100/250)])
def test_american_break_even(odds, expected): assert american_to_probability(odds) == pytest.approx(expected)

@pytest.mark.parametrize("odds,p,expected", [(150, .5, .25), (-200, .75, .125)])
def test_ev_uses_offered_positive_and_negative_price(odds, p, expected):
    assert hypothetical_return(p, odds) == pytest.approx(expected)

@pytest.mark.parametrize("actual,side,result", [(251,"over","win"),(248,"over","loss"),(248,"under","win"),(251,"under","loss"),(249.5,"over","push")])
def test_over_under_and_push_settlement(actual, side, result): assert settle(actual, 249.5, side, 100)["result"] == result

def test_ten_dollar_paper_unit_profit_loss(): assert settle(251, 249.5, "over", 150)["fixed_unit_profit_loss"] == 15

def test_edge_buckets():
    assert [edge_bucket(x) for x in (.0199,.02,.05,.08)] == ["edge_lt_2pp","edge_2_to_5pp","edge_5_to_8pp","edge_8pp_plus"]

def test_external_confirmation_flags():
    assert confirmation_flags("over", .6, .55, .56)["both_agree"]
    assert confirmation_flags("over", .6, .45, .44)["both_disagree"]
    assert confirmation_flags("over", .6, None, .56)["insufficient_external_confirmation"]

def test_immutable_snapshots_and_final_pregame(tmp_path):
    store = ShadowStore(f"sqlite:///{tmp_path/'shadow.db'}")
    first, final = observation(NOW-timedelta(minutes=10), -110), observation(NOW, 120)
    store.append(first); store.append(final)
    rows = store.all(); assert len(rows) == 2 and rows[0]["observation_id"] != rows[1]["observation_id"]
    timeline = store.timeline("g1", "p1", "Player", "player_pass_yds", 249.5, "over")
    assert timeline["first"]["hard_rock_offered_odds"] == -110
    assert timeline["final_pregame"]["hard_rock_offered_odds"] == 120
    assert timeline["best_price"]["hard_rock_offered_odds"] == 120

def test_post_kickoff_rejected():
    with pytest.raises(ValueError, match="post-kickoff"):
        observation(NOW+timedelta(hours=1))

def test_probability_provenance_and_exact_threshold(tmp_path):
    model = DummyRegressor(strategy="constant", constant=10).fit([[0],[1]], [10,10])
    path = tmp_path/"artifact.joblib"
    joblib.dump({"pipeline": model, "features":["x"], "artifact_id":"football-v1",
                 "calibration_predictions":np.array([5.,10.,15.,20.]),
                 "calibration_residuals":np.array([-2.,-1.,1.,3.]),
                 "prediction_bin_edges":[-float("inf"), float("inf")]}, path)
    result = FootballArtifactScorer(path).score(pd.DataFrame({"x":[4]}), 10, NOW)
    assert result.model_version == "football-v1" and result.point_prediction == 10
    assert result.over_probability == .5 and result.uncertainty_method.startswith("prediction_conditional")

def test_drawdown_brier_and_calibration():
    assert maximum_drawdown([10,-4,-8,3]) == 12
    rows=[]
    for p,result,pl in [(.8,"win",10),(.2,"loss",-10)]:
        row=observation(); row.update(model_probability=p,result=result,profit_loss_per_dollar=pl/10,
                                      fixed_unit_profit_loss=pl); rows.append(row)
    report=aggregate(rows, minimum_sample=3)[0]
    assert report["brier_score"] == pytest.approx(.04)
    assert len(report["calibration"]) == 2 and report["small_sample_warning"]

def test_no_order_or_wager_behavior_exists():
    import app.shadow as module
    assert not any(name.startswith(("place_", "submit_", "execute_")) for name in dir(module))
