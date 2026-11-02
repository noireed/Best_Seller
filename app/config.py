from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    api_base_url: str = "http://136.119.248.78"
    team_code: str = ""
    dry_run: bool = True
    enable_trading: bool = False
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    watchlist: str = "005930,000660,035420"
    loop_seconds: int = 900
    candle_count: int = 100
    max_position_pct: float = Field(default=0.15, ge=0.01, le=1.0)
    max_order_value_krw: int = 2_000_000
    cash_reserve_pct: float = Field(default=0.10, ge=0, le=0.9)
    min_confidence: float = Field(default=0.60, ge=0, le=1)
    db_path: str = "data/trading.db"
    log_level: str = "INFO"

    @property
    def symbols(self):
        return [x.strip() for x in self.watchlist.split(",") if x.strip()]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

@lru_cache
def get_settings():
    return Settings()
