from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.player_history import build_player_history, split_statistics
from app.research_export import (build_export_rows, calibration_table, canonical_captures,
                                 edge_bucket, performance_summary, probability_bucket)
from app.research_journal import SelectionJournal
from app.shadow_storage import ShadowStore

KICKOFF = datetime(2026, 9, 13, 20, tzinfo=timezone.utc)


def row(identifier, hours, *, slot, side="over", line=250.5, result="win"):
    return {"observation_id": identifier, "observed_at_utc": KICKOFF-timedelta(hours=hours),
        "kickoff_utc": KICKOFF, "game_id": "g1", "player_id": "p1", "player_name": "Player",
        "team": "BUF", "opponent": "MIA", "canonical_market": "player_pass_yds", "side": side,
        "line": line, "hard_rock_offered_odds": -110, "point_prediction": 260,
        "model_probability": .61, "offered_price_break_even_probability": .524,
        "raw_probability_edge_pp": 8.6, "reference_context": {"reference_book_count": 3,
        "exact_threshold_over_no_vig_probability": .55}, "kalshi_context": {},
        "confirmation_flags": {"both_agree": True}, "context": {"capture_slot": slot},
        "settled_at_utc": KICKOFF+timedelta(hours=4), "actual_value": 270, "result": result,
        "profit_loss_per_dollar": .9090909, "fixed_unit": 10,
        "fixed_unit_profit_loss": 9.090909 if result == "win" else 0, "american_odds": -110}


def test_capture_selection_dedup_side_and_missing_windows():
    rows = [row("far", 25, slot="24h"), row("near", 24.1, slot="24h"),
            row("under", 24, slot="24h", side="under", line=249.5)]
    captures = canonical_captures(rows)
    assert len(captures) == 2
    assert captures[("g1", "p1", "player_pass_yds", "over")]["24h"]["observation_id"] == "near"
    exported = build_export_rows(rows)
    over = next(item for item in exported if item["side"] == "over")
    assert over["capture_6h"] is None and over["capture_15m"] is None
    assert over["actual_result"] == 270 and over["flat_10_pnl"] == pytest.approx(9.090909)
    assert over["american_odds_payout_used"] == -110


def test_summary_probability_and_edge_buckets_push_handling():
    rows = [row("win", 1.5, slot="90m"), row("push", 1.5, slot="90m", result="push")]
    summary = performance_summary(rows)[0]
    assert (summary["bets"], summary["wins"], summary["pushes"], summary["hit_rate"]) == (2, 1, 1, 1)
    assert edge_bucket(1.99) == "<2pp" and edge_bucket(35) == "35pp+"
    assert probability_bucket(.549) == "50–55%" and probability_bucket(.9) == "90%+"
    calibration = calibration_table(rows)
    assert calibration[0]["observations"] == 1  # pushes are excluded from realized calibration


def test_player_history_and_small_sample_split():
    weekly = pd.DataFrame([{"season": 2026, "week": 1, "game_id": "g1", "player_id": "p1",
        "player_name": "Player", "team": "BUF", "passing_yards": 270, "receiving_yards": 0,
        "receptions": 0, "targets": 0, "attempts": 30}])
    schedules = pd.DataFrame([{"season": 2026, "week": 1, "game_id": "g1", "home_team": "BUF",
        "away_team": "MIA", "gameday": "2026-09-13", "gametime": "16:00", "home_score": 24,
        "away_score": 17, "spread_line": -3, "roof": "outdoors", "wind": 22}])
    history = build_player_history(weekly, schedules)
    assert history.iloc[0].opponent == "mia" and history.iloc[0].weather_status == "high_wind"
    split = split_statistics(history, statistic="passing_yards", threshold=250.5)
    assert split["sample_size"] == split["hits"] == 1 and split["small_sample"]
    assert "not a model probability" in split["warning"]


def test_selection_journal_is_append_only_and_validated():
    store = ShadowStore("sqlite://")
    journal = SelectionJournal(store)
    first = journal.append("obs", selected=True, intended_stake=10, reason_codes=["model_edge"], journal_id="j1")
    second = journal.append("obs", selected=False, reason_codes=["injury_context"], journal_id="j2")
    assert [item["journal_id"] for item in journal.all()] == [first["journal_id"], second["journal_id"]]
    with pytest.raises(Exception):
        journal.append("obs", selected=True, journal_id="j1")
    with pytest.raises(ValueError):
        journal.append("obs", selected=True, reason_codes=["made_up"])
