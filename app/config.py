"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "QR Claim & Rewards Bot"
    app_env: str = Field(default="production", description="development|staging|production")
    debug: bool = False
    secret_key: str = Field(..., min_length=32, description="Application secret key")
    jwt_secret: str = Field(..., min_length=32, description="JWT signing secret")
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 8  # 8 hours
    session_expire_minutes: int = 60 * 12  # 12 hours
    csrf_secret: str = Field(..., min_length=32)

    # Server
    host: str = "0.0.0.0"
    port: int = Field(default=8000, description="Overridden by $PORT on Railway")

    # Database
    database_url: str = Field(
        ...,
        description="PostgreSQL connection string, e.g. postgresql+asyncpg://user:pass@host:5432/db",
    )

    # Telegram
    bot_token: str = Field(..., min_length=10)
    bot_username: str = Field(default="", description="Bot username without @")
    owner_telegram_id: int = Field(..., description="Telegram ID of the OWNER")
    public_base_url: str = Field(
        default="",
        description="Public HTTPS URL of this app e.g. https://xxx.up.railway.app",
    )
    admin_panel_path: str = Field(default="/admin", description="Admin panel path")


    # Admin bootstrap
    admin_username: str = Field(..., min_length=3)
    admin_password: str = Field(..., min_length=8)

    # Defaults
    default_reward_usd: float = 0.50
    min_withdrawal_usd: float = 5.0
    max_withdrawal_usd: float = 1000.0
    force_join_enabled: bool = True
    claim_approval_required: bool = True
    qr_default_expiry_hours: int = 72
    maintenance_mode: bool = False
    bot_welcome_message: str = (
        "Welcome to QR Claim Bot!\n\n"
        "Claim exclusive one-time QR rewards, track your balance, and request withdrawals.\n\n"
        "How it works:\n"
        "1. Join required channels (if asked)\n"
        "2. Tap CLAIM REWARD on a QR post\n"
        "3. Submit your claim and wait for approval\n"
        "4. Get rewarded in your wallet\n"
        "5. Withdraw when you reach the minimum\n\n"
        "Each QR can be claimed by only ONE user."
    )

    # Rate limiting
    login_rate_limit: str = "10/minute"
    api_rate_limit: str = "100/minute"

    # Security
    password_min_length: int = 8
    max_login_attempts: int = 5
    lockout_minutes: int = 15

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        """Ensure asyncpg driver is used."""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://") and "+asyncpg" not in v:
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def default_reward_cents(self) -> int:
        return int(round(self.default_reward_usd * 100))

    @property
    def min_withdrawal_cents(self) -> int:
        return int(round(self.min_withdrawal_usd * 100))

    @property
    def max_withdrawal_cents(self) -> int:
        return int(round(self.max_withdrawal_usd * 100))

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance."""
    return Settings()
