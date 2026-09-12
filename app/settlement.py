"""Automated, nflverse-only settlement of immutable shadow observations."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterator
from uuid import uuid4

import pandas as pd
from sqlalchemy import text

from app.historical.features import MARKETS
from app.historical.normalize import normalize_schedules, normalize_weekly
from app.identities import canonical_team
from app.scheduler import sanitized_error
from app.shadow import settle_observations

_LOCAL_LOCK = Lock()
_ADVISORY_LOCK_ID = 681_944_731


def _utc(value: Any) -> datetime:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC").to_pydatetime()


def _is_final(row: pd.Series) -> bool:
    """Accept only explicit nflverse completion evidence, never elapsed time."""
    for column in ("game_status", "status", "game_state"):
        if column in row and pd.notna(row[column]) and str(row[column]).strip().lower() in {
            "final", "final/ot", "post", "completed", "closed"
        }:
            return True
    # nflverse schedules publish a non-null result only after a game finishes.
    return "result" in row and pd.notna(row["result"]) and str(row["result"]).strip() != ""


@contextmanager
def settlement_lock(engine) -> Iterator[bool]:
    """Serialize executions in PostgreSQL; retain an in-process test fallback."""
    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            acquired = bool(connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _ADVISORY_LOCK_ID}).scalar())
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _ADVISORY_LOCK_ID})
                    connection.commit()
    else:
        acquired = _LOCAL_LOCK.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                _LOCAL_LOCK.release()


class SettlementCycle:
    """One bounded execution using schedules and season-partitioned weekly stats."""

    def __init__(self, *, store, nflverse, unit: float = 10.0, refresh: bool = True,
                 now=lambda: datetime.now(timezone.utc)):
        self.store, self.nflverse, self.unit, self.refresh, self.now = store, nflverse, unit, refresh, now

    def run(self) -> dict[str, Any]:
        started = self.now().astimezone(timezone.utc)
        report: dict[str, Any] = {
            "execution_id": str(uuid4()), "started_at_utc": started.isoformat(), "ended_at_utc": None,
            "unsettled_observations_inspected": 0, "games_represented": 0, "games_final": 0,
            "games_results_not_available": 0, "observations_settled": 0, "wins": 0,
            "losses": 0, "pushes": 0, "settlement_failures": 0,
            "skipped_already_settled_rows": 0, "paper_profit_loss": 0.0, "errors": []}
        with settlement_lock(self.store.engine) as acquired:
            if not acquired:
                report["errors"].append({"kind": "overlapping_execution", "message": "settlement execution already active"})
                return self._finish(report)
            try:
                candidates, skipped = self.store.settlement_candidates(started)
                report["unsettled_observations_inspected"] = len(candidates)
                report["skipped_already_settled_rows"] = skipped
                if not candidates:
                    return self._finish(report)
                schedules = normalize_schedules(self.nflverse.fetch("schedules", refresh=self.refresh))
                resolved: list[tuple[dict, pd.Series]] = []
                games: dict[str, pd.Series] = {}
                for observation in candidates:
                    game = self._exact_game(observation, schedules)
                    if game is None:
                        self._failure(report, "event_identity_not_exact")
                        continue
                    key = str(game.game_id)
                    games[key] = game
                    resolved.append((observation, game))
                report["games_represented"] = len(games)
                final_ids = {key for key, game in games.items() if _is_final(game)}
                report["games_final"] = len(final_ids)
                final = [(observation, game) for observation, game in resolved if str(game.game_id) in final_ids]
                if not final:
                    return self._finish(report)
                weekly_by_season = {season: normalize_weekly(self.nflverse.fetch(
                    "weekly_stats", int(season), refresh=self.refresh))
                    for season in sorted({int(game.season) for _, game in final})}
                realized, unavailable_games = [], set()
                for observation, game in final:
                    result = self._exact_result(observation, game, weekly_by_season[int(game.season)])
                    if result is None:
                        unavailable_games.add(str(game.game_id))
                        report["errors"].append({"kind": "result_not_available",
                            "observation_id": observation["observation_id"], "message": "result_not_available"})
                        continue
                    realized.append(result)
                report["games_results_not_available"] = len(unavailable_games)
                eligible_ids = {r["observation_id"] for r in realized}
                eligible = [o for o, _ in final if o["observation_id"] in eligible_ids]
                rows = settle_observations(eligible, realized, unit=self.unit, settled_at_utc=started)
                if rows:
                    # One transaction for the full run. Any insert failure rolls all back.
                    with self.store.engine.begin() as connection:
                        self.store.insert_settlements(rows, connection=connection)
                report["observations_settled"] = len(rows)
                for row in rows:
                    report[{"win": "wins", "loss": "losses", "push": "pushes"}[row["result"]]] += 1
                report["paper_profit_loss"] = sum(row["fixed_unit_profit_loss"] for row in rows)
            except Exception as exc:
                report["settlement_failures"] += 1
                report["errors"].append({"kind": "settlement_failure", "message": sanitized_error(exc)})
            return self._finish(report)

    def _finish(self, report: dict[str, Any]) -> dict[str, Any]:
        report["ended_at_utc"] = self.now().astimezone(timezone.utc).isoformat()
        return report

    @staticmethod
    def _failure(report: dict[str, Any], reason: str) -> None:
        report["settlement_failures"] += 1
        report["errors"].append({"kind": reason, "message": reason})

    @staticmethod
    def _exact_game(observation: dict, schedules: pd.DataFrame) -> pd.Series | None:
        # Prefer an already-canonical nflverse game ID. Legacy captures use the
        # provider event ID and require the exact kickoff plus exact team pair.
        direct = schedules[schedules.game_id.astype(str) == str(observation["game_id"])]
        if len(direct) == 1:
            game = direct.iloc[0]
            if _utc(game.kickoff) != _utc(observation["kickoff_utc"]):
                return None
            supplied = {canonical_team(observation.get("team")), canonical_team(observation.get("opponent"))}
            expected = {str(game.home_team), str(game.away_team)}
            return game if None not in supplied and supplied == expected else None
        team, opponent = canonical_team(observation.get("team")), canonical_team(observation.get("opponent"))
        if not team or not opponent:
            return None
        kickoff = _utc(observation["kickoff_utc"])
        match = schedules[(schedules.kickoff.map(_utc) == kickoff) & (
            ((schedules.home_team == team) & (schedules.away_team == opponent)) |
            ((schedules.home_team == opponent) & (schedules.away_team == team)))]
        return match.iloc[0] if len(match) == 1 else None

    @staticmethod
    def _exact_result(observation: dict, game: pd.Series, weekly: pd.DataFrame) -> dict | None:
        market = observation.get("canonical_market")
        player_id = observation.get("player_id")
        if market not in MARKETS or not player_id:
            return None
        rows = weekly[(weekly.season.astype(int) == int(game.season)) &
                      (weekly.week.astype(int) == int(game.week)) &
                      (weekly.player_id.astype(str) == str(player_id))]
        if "game_id" in rows and rows.game_id.notna().any():
            rows = rows[rows.game_id.astype(str) == str(game.game_id)]
        else:
            team = canonical_team(observation.get("team"))
            if not team:
                return None
            rows = rows[rows.team == team]
        column = MARKETS[market]
        if len(rows) != 1 or column not in rows or pd.isna(rows.iloc[0][column]):
            return None
        value = pd.to_numeric(pd.Series([rows.iloc[0][column]]), errors="coerce").iloc[0]
        if pd.isna(value):
            return None
        return {"observation_id": observation["observation_id"], "game_id": observation["game_id"],
            "player_id": str(player_id), "canonical_market": market, "actual_value": float(value),
            "result_source_id": f"nflverse:weekly_stats:{int(game.season)}:{int(game.week)}:{game.game_id}:{player_id}:{market}"}
