"""
Окружение Alembic.

Использует sync_database_url (psycopg2) — async-драйвер asyncpg
не поддерживается стандартными Alembic-операциями. Метаданные
берутся из ``db.models.Base.metadata``, чтобы автогенерация (autogenerate)
видела все модели.
"""
import sys
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Корень проекта в sys.path — иначе импорт ``db.models`` не сработает,
# когда alembic запущен из подкаталога db/alembic.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db.models import Base  # noqa: E402
from shared.config import get_settings  # noqa: E402

settings = get_settings()

config = context.config
# URL берётся из настроек, а не из alembic.ini — чтобы все сервисы
# использовали одну и ту же конфигурацию подключения через .env.
config.set_main_option("sqlalchemy.url", settings.sync_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
