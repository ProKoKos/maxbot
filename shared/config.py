from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Max API (global token deprecated — bots are now stored per-user in DB)
    # Kept for backward-compat and webhook secret validation
    bot_mode: str = "polling"
    webhook_url: str = ""
    webhook_secret: str = "webhook_secret"

    # Token encryption (Fernet key, 44 chars)
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = "CHANGE_ME_generate_with_fernet_generate_key"

    # Database
    database_url: str = "postgresql+asyncpg://maxbot:maxbot_pass@db:5432/maxbot"
    sync_database_url: str = "postgresql://maxbot:maxbot_pass@db:5432/maxbot"

    # Security
    secret_key: str = "insecure_dev_key_change_in_production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 1 day

    # Web
    web_host: str = "0.0.0.0"
    web_port: int = 8000

    # Admin seed
    admin_email: str = "admin@example.com"
    admin_password: str = "changeme123"

    # Rate limiting
    rate_limit_per_minute: int = 60

    # Max API base
    max_api_base: str = "https://platform-api.max.ru"

    # Groq AI (free cloud LLM)
    groq_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
