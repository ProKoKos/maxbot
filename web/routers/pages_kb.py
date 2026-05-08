"""Страница базы знаний (/kb)."""
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from bot.crypto import decrypt_token
from db.models import AssistantConfig, Bot
from db.session import get_async_session
from web.deps import DBSession
from web.routers.pages import _require_user

router = APIRouter()
templates = Jinja2Templates(directory="/app/web/templates")


@router.get("/kb")
async def kb_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    configs_result = await session.execute(
        select(AssistantConfig)
        .where(AssistantConfig.user_id == user.id)
        .order_by(AssistantConfig.created_at.desc())
    )
    configs = configs_result.scalars().all()

    bots_result = await session.execute(select(Bot).where(Bot.user_id == user.id))
    bots = bots_result.scalars().all()
    bot_map = {b.id: b for b in bots}

    configs_data = [
        {
            "id": c.id,
            "group_name": c.group_name or c.group_id,
            "bot_name": (
                bot_map[c.bot_id].name or f"@{bot_map[c.bot_id].max_username}"
                if c.bot_id and c.bot_id in bot_map
                else str(c.bot_id)
            ),
            "embedding_model": c.embedding_model or "",
            "embedding_api_url": c.embedding_api_url or "",
            "embedding_api_key": decrypt_token(c.embedding_api_key) if c.embedding_api_key else "",
            "retrieval_top_k": c.retrieval_top_k,
            "retrieval_threshold": c.retrieval_threshold,
        }
        for c in configs
    ]

    return templates.TemplateResponse(
        "kb.html",
        {
            "request": request,
            "user": user,
            "configs_data": configs_data,
            "active_page": "kb",
        },
    )
