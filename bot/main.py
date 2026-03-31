"""
Bot service entry point.

Modes:
  polling (default) — BotSupervisor manages one asyncio Task per active bot.
                      New bots are picked up every REFRESH_INTERVAL seconds.
  webhook           — Registers all active bots' webhook URLs with Max API,
                      then keeps running to re-register on restarts.
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
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _register_webhooks() -> None:
    """Register webhook URL for every active bot (webhook mode)."""
    if not settings.webhook_url:
        logger.error("BOT_MODE=webhook but WEBHOOK_URL is not set.")
        sys.exit(1)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Bot).where(Bot.is_active == True))  # noqa
        bots = result.scalars().all()

    for bot in bots:
        try:
            token = decrypt_token(bot.encrypted_token)
        except ValueError as exc:
            logger.error("Bot %d: cannot decrypt token — %s", bot.id, exc)
            continue

        async with MaxClient(token=token) as client:
            try:
                resp = await client.subscribe_webhook(
                    settings.webhook_url, secret=settings.webhook_secret
                )
                logger.info("Bot %d (%s): webhook registered — %s", bot.id, bot.name, resp)
            except MaxAPIError as exc:
                logger.error("Bot %d (%s): webhook registration failed — %s", bot.id, bot.name, exc)


async def main() -> None:
    await _ensure_schema()

    mode = settings.bot_mode.lower()
    if mode == "webhook":
        await _register_webhooks()
        logger.info("Webhook mode: all bots registered. Web service handles incoming events.")
        # Keep alive so docker doesn't restart; re-register periodically
        while True:
            await asyncio.sleep(3600)
            await _register_webhooks()
    else:
        supervisor = BotSupervisor()
        await supervisor.run()


if __name__ == "__main__":
    asyncio.run(main())
