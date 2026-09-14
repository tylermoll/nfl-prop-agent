from datetime import datetime, timedelta, timezone

import pandas as pd

from app.historical.normalize import normalize_schedules
from app.settlement import SettlementCycle

KICKOFF = datetime(2026, 9, 13, 20, 25, tzinfo=timezone.utc)


def _schedules():
    return normalize_schedules(pd.DataFrame([
        {"season": 2026, "week": 1, "game_id": "2026_01_GB_MIN",
         "home_team": "MIN", "away_team": "GB", "gameday": "2026-09-13",
         "gametime": "16:25", "result": "final"},
    ]))


def test_audited_game_accepts_only_exact_preverified_provider_event_and_kickoff():
    observation = {"game_id": "opaque-provider-event", "kickoff_utc": KICKOFF}
    audited = {("opaque-provider-event", KICKOFF.isoformat()): "2026_01_GB_MIN"}
    game = SettlementCycle._audited_game(observation, _schedules(), audited)
    assert game is not None
    assert str(game.game_id) == "2026_01_GB_MIN"


def test_audited_game_fails_closed_without_preverified_mapping():
    observation = {"game_id": "opaque-provider-event", "kickoff_utc": KICKOFF}
    assert SettlementCycle._audited_game(observation, _schedules(), {}) is None


def test_audited_game_fails_closed_when_schedule_kickoff_differs():
    observation = {"game_id": "opaque-provider-event", "kickoff_utc": KICKOFF + timedelta(minutes=1)}
    audited = {("opaque-provider-event", observation["kickoff_utc"].isoformat()): "2026_01_GB_MIN"}
    assert SettlementCycle._audited_game(observation, _schedules(), audited) is None
