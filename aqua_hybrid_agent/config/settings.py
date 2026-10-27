from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    aqua_base_url: str
    aqua_team_code: str
    gemini_api_key: str = ""
    dry_run: bool = True
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
