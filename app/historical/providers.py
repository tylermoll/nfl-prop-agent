"""Optional pregame enrichment contracts; core features do not require them."""

from __future__ import annotations

from typing import Protocol

import pandas as pd


ADVANCED_USAGE_COLUMNS = (
    "routes_run", "route_participation", "targets_per_route_run",
    "yards_per_route_run", "alignment", "coverage_splits",
)
WEATHER_COLUMNS = (
    "temperature", "wind_speed", "wind_gust", "precipitation",
    "precipitation_probability",
)


class AdvancedUsageProvider(Protocol):
    """Licensed providers return rows keyed by player/game with observed_at."""

    def pregame_features(self, events: pd.DataFrame) -> pd.DataFrame: ...


class WeatherProvider(Protocol):
    """Providers return forecasts keyed by game with forecast_observed_at."""

    def pregame_features(self, events: pd.DataFrame) -> pd.DataFrame: ...


def join_asof_pregame(base: pd.DataFrame, enrichment: pd.DataFrame, *, by: list[str], timestamp: str) -> pd.DataFrame:
    """Join the last observation strictly before kickoff; never use hindsight."""
    if enrichment.empty:
        return base
    left = base.sort_values("kickoff").copy()
    right = enrichment.sort_values(timestamp).copy()
    return pd.merge_asof(left, right, left_on="kickoff", right_on=timestamp, by=by,
                         direction="backward", allow_exact_matches=False)
