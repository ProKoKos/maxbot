"""Логи и Webhook: GET /logs, POST /webhook/{bot_id}."""
import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from bot.client import MaxClient
from bot.crypto import decrypt_token
from bot.handlers import handle_update
from db.models import Bot, EventLog
from shared.config import get_settings
from web.deps import CurrentUser, DBSession, RateLimit

router = APIRouter()
settings = get_settings()


@router.get("/logs")
async def get_logs(current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(EventLog)
        .where(EventLog.user_id == current_user.id)
        .order_by(EventLog.created_at.desc())
        .limit(50)
    )
    logs = result.scalars().all()
    return [
        {
            "id": l.id,
            "bot_id": l.bot_id,
            "level": l.level,
            "message": l.message,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]


# ── Webhook endpoint (только для BOT_MODE=webhook) ───────────────────────────

@router.post("/webhook/{bot_id}")
async def webhook(bot_id: int, request: Request, session: DBSession):
    """Принимает входящий webhook от MAX для конкретного бота.

    URL: ``/api/webhook/{bot_id}`` — bot_id зашит в URL, потому что
    у каждого бота свой токен и свой webhook-эндпоинт. Подпись
    запроса проверяется через заголовок ``X-Max-Bot-Api-Secret`` —
    тот же секрет, что отдавали MAX'у при регистрации webhook'а.
    """
    secret = request.headers.get("X-Max-Bot-Api-Secret", "")
    if secret != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    result = await session.execute(select(Bot).where(Bot.id == bot_id, Bot.is_active == True))  # noqa
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found or inactive")

    try:
        token = decrypt_token(bot.encrypted_token)
    except ValueError:
        raise HTTPException(500, "Token decryption failed")

    update = await request.json()
    async with MaxClient(token=token) as client:
        try:
            await handle_update(update, session, client, bot_id=bot_id)
        except Exception as exc:
            logging.getLogger("web.webhook").exception("Webhook handler error: %s", exc)
