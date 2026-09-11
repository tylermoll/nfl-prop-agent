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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
