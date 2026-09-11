from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    demo_mode: bool = True
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/prop_agent"
    the_odds_api_key: str | None = None
    the_odds_api_lookahead_days: float = 4
    oddspapi_api_key: str | None = None
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    poll_seconds: int = 60
    stale_after_seconds: int = 180

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
