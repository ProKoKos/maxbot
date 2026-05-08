"""AI-ассистент конфиги: CRUD-эндпоинты /assistant/configs/*."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from bot.crypto import decrypt_token, encrypt_token
from db.models import AssistantConfig, Bot
from web.deps import CurrentUser, DBSession, RateLimit

router = APIRouter()


class AssistantConfigCreate(BaseModel):
    bot_id: int
    group_id: str
    group_name: str = ""
    is_enabled: bool = False
    system_prompt: str | None = None
    model_name: str = ""
    api_url: str = ""
    api_key: str = ""


class AssistantConfigUpdate(BaseModel):
    group_name: str | None = None
    is_enabled: bool | None = None
    system_prompt: str | None = None
    model_name: str | None = None
    api_url: str | None = None
    api_key: str | None = None
    # Поля базы знаний (RAG)
    embedding_model: str | None = None
    embedding_api_url: str | None = None
    embedding_api_key: str | None = None
    retrieval_top_k: int | None = None
    retrieval_threshold: float | None = None


@router.get("/assistant/configs")
async def list_assistant_configs(current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(AssistantConfig)
        .where(AssistantConfig.user_id == current_user.id)
        .order_by(AssistantConfig.created_at.desc())
    )
    configs = result.scalars().all()
    return [
        {
            "id": c.id,
            "bot_id": c.bot_id,
            "group_id": c.group_id,
            "group_name": c.group_name,
            "is_enabled": c.is_enabled,
            "system_prompt": c.system_prompt,
            "model_name": c.model_name,
            "api_url": c.api_url,
            "api_key": decrypt_token(c.api_key) if c.api_key else "",
            "embedding_model": c.embedding_model or "",
            "embedding_api_url": c.embedding_api_url or "",
            "embedding_api_key": decrypt_token(c.embedding_api_key) if c.embedding_api_key else "",
            "retrieval_top_k": c.retrieval_top_k,
            "retrieval_threshold": c.retrieval_threshold,
            "created_at": c.created_at.isoformat(),
        }
        for c in configs
    ]


@router.post("/assistant/configs", status_code=201)
async def create_assistant_config(
    body: AssistantConfigCreate,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    bot_result = await session.execute(
        select(Bot).where(Bot.id == body.bot_id, Bot.user_id == current_user.id)
    )
    if not bot_result.scalar_one_or_none():
        raise HTTPException(404, "Bot not found")

    existing = await session.execute(
        select(AssistantConfig).where(
            AssistantConfig.bot_id == body.bot_id,
            AssistantConfig.group_id == body.group_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "An assistant config for this bot+group already exists")

    config = AssistantConfig(
        user_id=current_user.id,
        bot_id=body.bot_id,
        group_id=body.group_id,
        group_name=body.group_name,
        is_enabled=body.is_enabled,
        system_prompt=body.system_prompt or None,
        model_name=body.model_name,
        api_url=body.api_url,
        api_key=encrypt_token(body.api_key) if body.api_key else "",
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return {"id": config.id}


@router.patch("/assistant/configs/{config_id}")
async def update_assistant_config(
    config_id: int,
    body: AssistantConfigUpdate,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    result = await session.execute(
        select(AssistantConfig).where(
            AssistantConfig.id == config_id,
            AssistantConfig.user_id == current_user.id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Assistant config not found")

    if body.group_name is not None:
        config.group_name = body.group_name
    if body.is_enabled is not None:
        config.is_enabled = body.is_enabled
    if body.system_prompt is not None:
        config.system_prompt = body.system_prompt or None
    if body.model_name is not None:
        config.model_name = body.model_name
    if body.api_url is not None:
        config.api_url = body.api_url
    if body.api_key is not None:
        config.api_key = encrypt_token(body.api_key) if body.api_key else ""
    if body.embedding_model is not None:
        config.embedding_model = body.embedding_model or None
    if body.embedding_api_url is not None:
        config.embedding_api_url = body.embedding_api_url or None
    if body.embedding_api_key is not None:
        config.embedding_api_key = encrypt_token(body.embedding_api_key) if body.embedding_api_key else None
    if body.retrieval_top_k is not None:
        config.retrieval_top_k = body.retrieval_top_k
    if body.retrieval_threshold is not None:
        config.retrieval_threshold = body.retrieval_threshold

    await session.commit()
    return {"ok": True}


@router.delete("/assistant/configs/{config_id}", status_code=204)
async def delete_assistant_config(
    config_id: int,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    result = await session.execute(
        select(AssistantConfig).where(
            AssistantConfig.id == config_id,
            AssistantConfig.user_id == current_user.id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Assistant config not found")
    await session.delete(config)
    await session.commit()
