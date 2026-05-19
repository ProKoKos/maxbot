"""
Точка входа сервиса bot (отдельный контейнер docker-compose).

Поддерживаются два режима, выбираются переменной окружения BOT_MODE:

  polling (по умолчанию)
      :class:`bot.supervisor.BotSupervisor` поднимает по asyncio.Task на
      каждый активный бот. Новые/удалённые боты подхватываются каждые
      ``SUPERVISOR_REFRESH_INTERVAL`` секунд (см. ``bot/constants.py``).
      Для большинства инсталляций — именно этот режим.

  webhook
      Регистрирует у MAX API webhook-URL для всех активных ботов
      и периодически (раз в час) переподтверждает регистрацию,
      чтобы пережить рестарты MAX-серверов. Сами апдейты приходят
      на ``/api/webhook/{bot_id}`` сервиса web (см. web/routers/api.py).
"""
import asyncio
import logging
import sys

sys.path.insert(0, "/app")

from bot.client import MaxAPIError, MaxClient
from bot.crypto import decrypt_token
from bot.supervisor import BotSupervisor
from db.models import Base, Bot
from db.session import AsyncSessionLocal, async_engine
from shared.config import get_settings
from sqlalchemy import select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot.main")
settings = get_settings()


async def _ensure_schema() -> None:
    """Подстраховка от случая «alembic не применил миграции».

    Создаёт недостающие таблицы (но не модифицирует существующие).
    Основная схема — через alembic upgrade head в сервисе migrate.
    """
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _register_webhooks() -> None:
    """Регистрирует webhook-URL у всех активных ботов (только режим webhook).

    Каждый бот получает свой уникальный URL вида:
    ``{WEBHOOK_URL}/api/webhook/{bot_id}``
    """
    if not settings.webhook_url:
        logger.error("BOT_MODE=webhook but WEBHOOK_URL is not set.")
        sys.exit(1)

    base_url = settings.webhook_url.rstrip("/")

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Bot).where(Bot.is_active == True))  # noqa
        bots = result.scalars().all()

    for bot in bots:
        try:
            token = decrypt_token(bot.encrypted_token)
        except ValueError as exc:
            logger.error("Bot %d: cannot decrypt token — %s", bot.id, exc)
            continue

        bot_webhook_url = f"{base_url}/api/webhook/{bot.id}"
        async with MaxClient(token=token) as client:
            try:
                resp = await client.subscribe_webhook(
                    bot_webhook_url, secret=settings.webhook_secret
                )
                logger.info("Bot %d (%s): webhook registered at %s — %s", bot.id, bot.name, bot_webhook_url, resp)
            except MaxAPIError as exc:
                logger.error("Bot %d (%s): webhook registration failed — %s", bot.id, bot.name, exc)


async def main() -> None:
    await _ensure_schema()

    mode = settings.bot_mode.lower()
    if mode == "webhook":
        await _register_webhooks()
        logger.info("Webhook mode: all bots registered. Web service handles incoming events.")
        # Перерегистрация раз в час — на случай, если MAX обнулил
        # подписку (например, после своего рестарта). Цикл также не даёт
        # docker считать процесс «завершённым» и рестартовать контейнер.
        while True:
            await asyncio.sleep(3600)
            await _register_webhooks()
    else:
        supervisor = BotSupervisor()
        await supervisor.run()


if __name__ == "__main__":
    asyncio.run(main())
