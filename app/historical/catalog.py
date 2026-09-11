"""Declarative nflverse source catalog.

URLs are intentionally data-release URLs rather than HTML or scraped pages.
Coverage is conservative and is recorded in every cache manifest.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    url_template: str
    season_min: int
    season_max: int | None
    limitation: str
    partitioned: bool = True


BASE = "https://github.com/nflverse/nflverse-data/releases/download"
DATASETS: dict[str, DatasetSpec] = {
    "weekly_stats": DatasetSpec("weekly_stats", f"{BASE}/player_stats/player_stats_{{season}}.parquet", 1999, None, "Participation and some advanced fields vary by era."),
    "play_by_play": DatasetSpec("play_by_play", f"{BASE}/pbp/play_by_play_{{season}}.parquet", 1999, None, "Air yards and player IDs are missing on some plays/older seasons."),
    "schedules": DatasetSpec("schedules", f"{BASE}/schedules/games.parquet", 1999, None, "Single all-season file; future schedule changes may occur.", False),
    "rosters": DatasetSpec("rosters", f"{BASE}/rosters/roster_{{season}}.parquet", 1920, None, "Roster timing and identifiers are less complete in older seasons."),
    "players": DatasetSpec("players", f"{BASE}/players/players.parquet", 1920, None, "Crosswalk fields can be nullable.", False),
    "injuries": DatasetSpec("injuries", f"{BASE}/injuries/injuries_{{season}}.parquet", 2009, None, "Practice reports are not a complete game-time availability history."),
    "depth_charts": DatasetSpec("depth_charts", f"{BASE}/depth_charts/depth_charts_{{season}}.parquet", 2002, None, "Weekly publication timing and historical coverage vary."),
    "snap_counts": DatasetSpec("snap_counts", f"{BASE}/snap_counts/snap_counts_{{season}}.parquet", 2012, None, "Availability and team-total denominators vary by season."),
}
