"""
Глобальные настройки приложения, читаются из переменных окружения /
файла ``.env``. Используется pydantic-settings — он автоматически
конвертирует типы и поддерживает префиксы.

Доступ к настройкам — через :func:`get_settings`, кешируется
``lru_cache``: один раз прочитали, дальше переиспользуем тот же объект.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все настройки приложения в одном месте.

    Любое поле можно переопределить переменной окружения с тем же именем
    в верхнем регистре (например, BOT_MODE, DATABASE_URL).
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # extra="ignore" — лишние переменные в .env не вызывают ошибок;
        # удобно при общем .env для всех сервисов.
        extra="ignore",
    )

    # ── Режим работы бота ────────────────────────────────────────────────────
    # "polling" (по умолчанию) — long-polling через supervisor.
    # "webhook" — регистрируем URL у MAX, апдейты приходят на /api/webhook.
    bot_mode: str = "polling"
    webhook_url: str = ""
    # Любой бот, регистрируясь в webhook-режиме, использует этот секрет.
    # MAX подписывает запрос заголовком X-Max-Bot-Api-Secret.
    webhook_secret: str = "webhook_secret"

    # ── Шифрование чувствительных строк (Fernet) ─────────────────────────────
    # 44 символа URL-safe base64. Генерируется один раз и кладётся в .env.
    # Смена ключа делает все токены ботов и API-ключи AI нечитаемыми —
    # см. предупреждение в bot/crypto.py.
    encryption_key: str = "CHANGE_ME_generate_with_fernet_generate_key"

    # ── База данных ──────────────────────────────────────────────────────────
    # Async-URL для приложения (asyncpg).
    database_url: str = "postgresql+asyncpg://maxbot:maxbot_pass@db:5432/maxbot"
    # Sync-URL для Alembic (psycopg2). Должен указывать на ту же БД.
    sync_database_url: str = "postgresql://maxbot:maxbot_pass@db:5432/maxbot"

    # ── Безопасность сессий ──────────────────────────────────────────────────
    # JWT-подпись (HS256). В проде ОБЯЗАТЕЛЬНО заменить на случайные 32+ байт.
    secret_key: str = "insecure_dev_key_change_in_production"
    algorithm: str = "HS256"
    # Срок жизни access-токена. Сутки — компромисс между UX (юзеру
    # не нужно часто логиниться) и безопасностью.
    access_token_expire_minutes: int = 60 * 24

    # ── Web ──────────────────────────────────────────────────────────────────
    web_host: str = "0.0.0.0"
    web_port: int = 8000

    # ── Сидирование первого админа ───────────────────────────────────────────
    # При первом старте web создаёт пользователя с этими credentials,
    # если ни одного юзера ещё нет (см. web/main.py:startup).
    admin_email: str = "admin@example.com"
    admin_password: str = "changeme123"

    # ── Лимит запросов на IP (in-memory sliding window) ──────────────────────
    # Hard-cap на каждое IP-адрес/минута. При горизонтальном масштабировании
    # сбрасывается per-process — для production нужен Redis-backed лимитер.
    rate_limit_per_minute: int = 60

    # ── Адрес MAX API ────────────────────────────────────────────────────────
    # Параметризуется на случай, если MAX введёт песочницу/прокси.
    max_api_base: str = "https://platform-api.max.ru"


@lru_cache
def get_settings() -> Settings:
    """Возвращает singleton с настройками. Чтение .env — один раз за процесс."""
    return Settings()
