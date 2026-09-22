from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    demo_mode: bool = True
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/prop_agent"
    the_odds_api_key: str | None = None
    the_odds_api_lookahead_days: float = 4
    the_odds_api_reference_bookmakers: tuple[str, ...] = (
        "draftkings",
        "fanduel",
        "betmgm",
        "williamhill_us",
    )
    the_odds_api_target_bookmaker: str = "hardrockbet"
    consensus_min_reference_books: int = 2
    oddspapi_api_key: str | None = None
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    # Empty means discover the exact NFL prop series from Kalshi's public
    # Football-tagged series catalog. Set a JSON list to use it as an allowlist.
    kalshi_nfl_series_tickers: tuple[str, ...] = ()
    kalshi_lookahead_days: float = 4
    kalshi_max_pages: int = 10
    kalshi_max_requests: int = 25
    kalshi_fetch_order_books: bool = False
    kalshi_order_book_shortlist_limit: int = 20
    poll_seconds: int = 60
    stale_after_seconds: int = 180
    shadow_edge_boundaries_pp: tuple[float, float, float] = (2, 5, 8)
    shadow_starting_bankroll: float = 100
    shadow_nominal_unit: float = 10
    shadow_minimum_report_sample: int = 30
    # V2 decision writes remain disabled while this is unset. Choosing a window
    # is an explicit prospective deployment decision, never a backtest result.
    shadow_selection_eligible_window: str | None = None
    shadow_selection_policy_name: str = "integrity"
    shadow_selection_policy_version: str = "v2-phase1"
    settlement_cache_dir: str = "data/historical/raw"
    settlement_nflverse_refresh: bool = True
    scheduler_slots_minutes: tuple[int, ...] = (1440, 360, 90, 15)
    scheduler_tolerance_minutes: int = 10
    scheduler_final_capture_minutes: int | None = None
    scheduler_min_quota_reserve: int = 25
    scheduler_max_credits_per_run: int = 12
    scheduler_max_games_per_run: int = 4
    scheduler_retry_budget: int = 2
    scheduler_provider_stale_seconds: int = 300
    scheduler_model_stale_seconds: int = 3600
    football_artifact_player_pass_yds: str | None = None
    football_artifact_player_reception_yds: str | None = None
    football_artifact_player_receptions: str | None = None
    # A separately refreshed, pregame-only table. Bulk nflverse data and model
    # artifacts are deployment inputs, not files downloaded by the scheduler.
    football_current_feature_path: str | None = None
    # Railway's five-minute cycle reuses this cache until it reaches this age.
    football_current_feature_max_age_seconds: int = 21600

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
