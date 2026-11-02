from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    news_enabled: bool = False
    news_use_in_llm: bool = False
    naver_client_id: str = ""
    naver_client_secret: str = ""
    news_providers: str = "gnews"
    gnews_api_key: str = ""
    gnews_symbol_aliases: str = '{"005930":["Samsung Electronics","Samsung"],"000660":["SK hynix"],"035420":["NAVER"]}'
    gnews_delay_hours: int = Field(default=12, ge=0, le=48)
    gnews_cache_seconds: int = Field(default=3600, ge=900, le=86400)
    gnews_daily_budget: int = Field(default=80, ge=1, le=100)
    news_api_db_path: str = "data/news_api_cache.db"
    news_allow_delayed_in_llm: bool = False
    news_strategy_enabled: bool = True
    news_allow_delayed_in_strategy: bool = True
    news_half_life_hours: float = Field(default=12, gt=0, le=168)
    news_min_sources: int = Field(default=2, ge=2, le=10)
    news_max_adjustment: float = Field(default=.15, ge=0, le=.2)
    decision_threshold: float = Field(default=.35, ge=.2, le=.8)
    llm_weight: float = Field(default=.2, ge=0, le=.3)
    max_price_age_seconds: int = Field(default=900, ge=60, le=3600)
    news_max_age_hours: int = Field(default=48, ge=1, le=168)
    news_cache_seconds: int = Field(default=900, ge=60, le=86400)
    news_display: int = Field(default=20, ge=1, le=100)
    news_symbol_aliases: str = '{"005930":["삼성전자"],"000660":["SK하이닉스","SK 하이닉스"],"035420":["네이버","NAVER"]}'

    @property
    def symbols(self):
        return [x.strip() for x in self.watchlist.split(",") if x.strip()]

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

@lru_cache
def get_settings():
    return Settings()
