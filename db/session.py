"""
Подключение к PostgreSQL и фабрика сессий.

Два движка живут параллельно:

  • async_engine + AsyncSessionLocal — основной путь для bot, web и
    scheduler. Драйвер asyncpg.
  • sync_engine — нужен только Alembic'у (миграции выполняются
    синхронно через psycopg2).

Параметры пула подобраны под три сервиса (web, bot, scheduler), каждый
из которых открывает по своему пулу размером ``pool_size``. С учётом
``max_overflow`` суммарно может быть до 60 соединений на инстанс
PostgreSQL — укладываемся в дефолтный лимит (max_connections=100).
"""
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.config import get_settings

settings = get_settings()

# Async engine — используется bot, web и scheduler.
async_engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    # pool_pre_ping: проверять соединение перед выдачей из пула.
    # Лечит «server closed the connection unexpectedly» после долгих
    # пауз (PgBouncer/Postgres могут разорвать idle-соединения).
    pool_pre_ping=True,
    # Перерабатывать соединения раз в 30 минут — на случай, если на
    # стороне БД настроен короткий idle_in_transaction_session_timeout.
    pool_recycle=1800,
    # application_name виден в pg_stat_activity — облегчает диагностику,
    # какой именно сервис сейчас держит коннекты/блокировки.
    connect_args={"server_settings": {"application_name": "maxbot"}},
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    # expire_on_commit=False: после commit'а ORM-объекты сохраняют
    # свои атрибуты, не делая повторный SELECT. Важно для FastAPI,
    # где данные нужны сразу после commit'а в том же handler'е.
    expire_on_commit=False,
)


async def get_async_session() -> AsyncSession:
    """FastAPI-зависимость: открывает сессию на время запроса.

    Контекст-менеджер сам закроет сессию по выходу из handler'а
    (вернёт соединение в пул).
    """
    async with AsyncSessionLocal() as session:
        yield session


# Sync engine — только для Alembic, который не умеет async.
sync_engine = create_engine(settings.sync_database_url, echo=False)
