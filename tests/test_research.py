from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from app.main import app
from app.research import ResearchRepository, get_repository
from app.shadow_storage import (metadata, observations, pregame_context_snapshots,
                                scheduler_executions, scheduler_slots, settlements)

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def research_client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    repo = ResearchRepository(engine=engine)
    app.dependency_overrides[get_repository] = lambda: repo
    calls = []
    async def forbidden(*_args, **_kwargs):
        calls.append(1)
        pytest.fail("research endpoints must not call providers")
    monkeypatch.setattr("app.main.load_rows", forbidden)
    try:
        yield TestClient(app), engine, calls
    finally:
        app.dependency_overrides.clear()


def observation_row(identifier="obs-1", **changes):
    row = {"observation_id": identifier, "observed_at_utc": NOW, "kickoff_utc": NOW + timedelta(hours=2),
        "game_id": "game-1", "player_id": "player-1", "player_name": "Example Player", "team": "BUF", "opponent": "MIA",
        "canonical_market": "player_pass_yds", "line": 250.5, "side": "over", "hard_rock_offered_odds": -110,
        "offered_price_break_even_probability": .5238, "model_probability": .56, "model_version": "model-v1",
        "raw_probability_edge_pp": 3.62, "hypothetical_expected_return_per_dollar": .069,
        "edge_bucket": "edge_2_to_5pp", "feature_built_at_utc": NOW - timedelta(minutes=10),
        "uncertainty_method": "residual", "uncertainty_version": "u1", "residual_bucket": 2,
        "uncertainty_scale": 15.0, "point_prediction": 260.0,
        "reference_context": {"reference_book_count": 3, "median_line": 249.5,
            "exact_threshold_over_no_vig_probability": .52, "raw_payload": "must disappear"},
        "kalshi_context": {"yes_bid": .53, "yes_ask": .57, "midpoint": .55, "volume": 100, "open_interest": 20,
                           "source_market_id": "safe-market"},
        "source_observation_ids": {"hardrock": "safe-source", "api_key": "nope"},
        "freshness": {"model_feature_age_seconds": 600, "model_stale": False, "provider_age_seconds": 20,
                      "provider_stale": False},
        "context": {"capture_slot": "90m", "artifact_provenance": {"path": "https://private/path"}},
        "confirmation_flags": {"both_agree": True, "both_disagree": False},
        "research_config": {"minimum_report_sample": 30}}
    row.update(changes)
    return row


def seed(engine):
    details = {"dry_run": False, "due": [1, 2], "selected": [1, 2], "completed": [1], "deferred": [2], "failed": [{
                   "event_id": "game-1", "provider_event_id": "game-1", "matchup": "BUF at MIA", "slot": "24h",
                   "kickoff_utc": NOW.isoformat(), "failure_category": "provider_http_failure",
                   "reason": "token=slot-secret https://slot.invalid/x", "provider_request_made": True,
                   "estimated_credit_cost": 3, "actual_credit_cost": 3, "retryable": True}],
               "estimated_credits": 3, "actual_credits_consumed": 3, "quota_before": 50, "quota_after": 47,
               "http_request_count": 2, "errors": [{"message": "token=secret https://private.invalid/x"}]}
    with engine.begin() as conn:
        conn.execute(insert(observations), [observation_row(), observation_row("obs-2", player_name="Other Player",
            canonical_market="player_receptions", side="under", edge_bucket="edge_lt_2pp", context={"capture_slot": "15m"})])
        conn.execute(insert(settlements), {"observation_id": "obs-1", "settled_at_utc": NOW + timedelta(hours=4),
            "actual_value": 270, "result": "win", "profit_loss_per_dollar": .909, "fixed_unit": 10,
            "fixed_unit_profit_loss": 9.09, "result_source_id": "stats-1"})
        conn.execute(insert(scheduler_executions), {"execution_id": "exec-1", "started_at_utc": NOW,
            "ended_at_utc": NOW + timedelta(seconds=4), "details": details})
        conn.execute(insert(scheduler_slots), {"event_id": "game-2", "slot": "15m", "target_time_utc": NOW,
            "status": "failed", "attempts": 1, "reason": "offline", "updated_at_utc": NOW})


def test_empty_database_and_html(research_client):
    client, _, calls = research_client
    assert client.get("/research/executions").json()["items"] == []
    assert client.get("/research/observations").json()["items"] == []
    summary = client.get("/research/summary").json()
    assert summary["total_observations"] == 0 and summary["latest_quota_state"] is None
    assert summary["performance_metrics"] is None
    assert summary["performance_note"] == "Performance metrics will appear after prospective observations are settled."
    page = client.get("/research").text
    assert "not betting recommendations" in page
    assert "Waiting for first scheduled capture" in page
    assert "Player Prop Opportunities" in page
    assert calls == []


def test_dashboard_is_read_only_and_has_filters_without_recommendation_labels(research_client):
    client, _, calls = research_client
    page = client.get("/research").text
    assert all(name in page for name in ('id="market"', 'id="side"', 'id="window"',
                                         'id="tier"', 'id="settled"', 'id="search"'))
    assert "fetch('/research/summary')" in page
    assert "fetch('/research/observations?limit=200')" in page
    assert "method=\"post\"" not in page.lower()
    assert not any(label in page for label in (">BET<", ">PASS<", ">LOCK<", "BEST BET"))
    assert "Performance metrics will appear after prospective observations are settled." in page
    assert calls == []


def test_execution_listing_newest_first_counts_and_sanitizes(research_client):
    client, engine, _ = research_client; seed(engine)
    item = client.get("/research/executions?limit=1&offset=0").json()["items"][0]
    assert item["execution_id"] == "exec-1" and item["due_count"] == 2 and item["deferred_count"] == 1
    assert item["failed_count"] == 1 and item["failed_slots"][0]["failure_category"] == "provider_http_failure"
    serialized = str(item)
    assert "slot-secret" not in serialized and "private.invalid" not in serialized and "slot.invalid" not in serialized
    assert "[REDACTED]" in serialized


def test_observation_listing_detail_settlement_and_unsettled(research_client):
    client, engine, _ = research_client; seed(engine)
    items = client.get("/research/observations").json()["items"]
    assert [x["observation_id"] for x in items] == ["obs-2", "obs-1"]
    settled = client.get("/research/observations/obs-1").json()
    assert settled["settlement_status"] == "settled" and settled["settlement"]["paper_profit_loss"] == 9.09
    assert settled["kalshi"]["spread"] == pytest.approx(.04)
    serialized = str(settled)
    assert "safe-source" in serialized and "safe-market" in serialized
    assert "api_key" not in serialized and "private/path" not in serialized and "raw_payload" not in serialized
    unsettled = client.get("/research/observations/obs-2").json()
    assert unsettled["settlement_status"] == "unsettled" and unsettled["settlement"] is None


def test_observation_timeline_groups_identity_without_provider_calls(research_client):
    client, engine, calls = research_client
    seed(engine)
    earlier = observation_row("obs-0", observed_at_utc=NOW-timedelta(hours=4), line=248.5,
                              context={"capture_slot": "6h"})
    unrelated = observation_row("other-game", game_id="game-x", context={"capture_slot": "6h"})
    with engine.begin() as conn:
        conn.execute(insert(observations), [earlier, unrelated])
    result = client.get("/research/observations/obs-1/timeline")
    assert result.status_code == 200
    items = result.json()["items"]
    assert [item["observation_id"] for item in items] == ["obs-0", "obs-1"]
    assert [item["capture_window"] for item in items] == ["6h", "90m"]
    assert client.get("/research/observations/missing/timeline").status_code == 404
    assert calls == []


@pytest.mark.parametrize("query, expected", [
    ("market=player_receptions", ["obs-2"]), ("player=Example%20Player", ["obs-1"]),
    ("event=BUF", ["obs-2", "obs-1"]), ("side=under", ["obs-2"]),
    ("capture_window=90m", ["obs-1"]), ("edge_tier=edge_lt_2pp", ["obs-2"]),
    ("settled=true", ["obs-1"]), ("settled=false", ["obs-2"]),
])
def test_filters_are_exact(research_client, query, expected):
    client, engine, _ = research_client; seed(engine)
    assert [x["observation_id"] for x in client.get(f"/research/observations?{query}").json()["items"]] == expected


def test_date_filter_summary_and_missing_404(research_client):
    client, engine, calls = research_client; seed(engine)
    assert len(client.get("/research/observations", params={"start": (NOW-timedelta(seconds=1)).isoformat()}).json()["items"]) == 2
    result = client.get("/research/summary").json()
    assert result["total_observations"] == 2
    assert result["observations_by_market"] == {"player_pass_yds": 1, "player_receptions": 1}
    assert result["settlement_counts"] == {"settled": 1, "unsettled": 1}
    assert result["scheduler_capture_counts"]["failed"] == 1
    assert result["latest_successful_execution"]["execution_id"] == "exec-1"
    performance = result["performance_metrics"]
    assert performance["settled_count"] == 1 and performance["wins"] == 1
    assert performance["paper_profit_loss"] == pytest.approx(9.09)
    assert performance["realized_roi"] == pytest.approx(.909)
    assert performance["brier_score"] == pytest.approx((.56-1) ** 2)
    assert result["represented_matchups"] == 1 and calls == []
    assert client.get("/research/observations/missing").status_code == 404


def test_research_has_no_mutation_routes(research_client):
    client, _, _ = research_client
    paths = client.get("/openapi.json").json()["paths"]
    for path, methods in paths.items():
        if path.startswith("/research"):
            assert set(methods) <= {"get"}
    assert client.post("/research/observations").status_code == 405


def test_context_endpoint_is_append_only_as_of_and_does_not_change_observation(research_client):
    client, engine, calls = research_client
    seed(engine)
    old = {"context_id": "ctx-old", "observation_id": "obs-1", "as_of_utc": NOW-timedelta(hours=2),
           "collected_at_utc": NOW-timedelta(hours=2), "weather": {"venue_type": "indoor"},
           "injuries": {}, "role": {}, "sources": {"weather": "NOAA"}}
    new = {**old, "context_id": "ctx-new", "as_of_utc": NOW+timedelta(minutes=1),
           "collected_at_utc": NOW+timedelta(minutes=1), "weather": {"venue_type": "outdoor", "wind_mph": 24}}
    with engine.begin() as conn:
        conn.execute(insert(pregame_context_snapshots), [old, new])
    before = client.get("/research/observations/obs-1").json()
    historical = client.get("/research/observations/obs-1/context",
                            params={"as_of": NOW.isoformat()}).json()
    latest = client.get("/research/observations/obs-1/context",
                        params={"as_of": (NOW+timedelta(minutes=2)).isoformat()}).json()
    after = client.get("/research/observations/obs-1").json()
    assert historical["context_id"] == "ctx-old" and historical["flags"]["indoor_weather_irrelevant"]
    assert latest["context_id"] == "ctx-new" and latest["flags"]["wind_high"]
    for field in ("model_over_probability", "model_under_probability", "probability_edge_pp", "model_point_prediction"):
        assert before[field] == after[field]
    assert calls == []
