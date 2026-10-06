from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str
    bot_token: str
    bot_username: str = ""
    owner_telegram_id: int
    jwt_secret: str
    admin_username: str = "owner"
    admin_password: str = "change_me_now"
    api_port: int = 4000
    default_reward_usd: float = 0.50
    min_withdrawal_usd: float = 5.0
    max_withdrawal_usd: float = 1000.0
    force_join_enabled: bool = True
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

settings = Settings()
