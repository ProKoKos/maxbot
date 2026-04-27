"""
JSON REST API.

Используется фронтом (fetch()) и внешними интеграциями. Все эндпоинты,
кроме ``/login`` и ``/webhook/{bot_id}``, требуют JWT (cookie или Bearer).

Структура: разделы помечены заголовками ``# ── ...`` — Auth, Bots,
Pairs, Welcome configs, Assistant configs, Logs, Posts, Verification
settings, Webhook, Inbox. Каждый ресурс соблюдает изоляцию по
``current_user.id`` — пользователь видит только свои сущности.
"""
import json
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func as sqlfunc, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from bot.client import MaxAPIError, MaxClient
from bot.crypto import decrypt_token, encrypt_token
from bot.handlers import handle_update
from db.models import (
    AssistantConfig,
    Bot,
    ChannelGroupPair,
    ConversationMessage,
    EventLog,
    InboxReadStatus,
    LogLevel,
    PostStatus,
    ScheduledPost,
    Subscription,
    User,
    UserBotContext,
    VerificationRequest,
    WelcomeConfig,
)
from shared.config import get_settings
from web.auth import create_access_token, verify_password
from web.deps import CurrentUser, DBSession, RateLimit

router = APIRouter(prefix="/api")
settings = get_settings()


# ── Auth ──────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, session: DBSession, _: RateLimit):
    """Логин по email+password. Возвращает JWT в JSON и одновременно ставит httpOnly-cookie.

    Cookie позволяет UI работать без явной передачи Bearer; JSON-токен
    нужен внешним API-клиентам. samesite=lax предотвращает простейший
    CSRF (но не заменяет полноценную CSRF-защиту форм, которой пока нет).
    """
    result = await session.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user.email})
    resp = JSONResponse({"access_token": token, "token_type": "bearer"})
    resp.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
    )
    return resp


@router.post("/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("access_token")
    return resp


# ── Bots ──────────────────────────────────────────────────────────────────────

class BotCreate(BaseModel):
    name: str
    description: str = ""
    token: str  # plain token — will be encrypted before storage


class BotUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    token: str | None = None  # plain; re-encrypts if provided


@router.get("/bots")
async def list_bots(current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(Bot)
        .where(Bot.user_id == current_user.id)
        .order_by(Bot.created_at.desc())
    )
    bots = result.scalars().all()
    return [
        {
            "id": b.id,
            "name": b.name,
            "description": b.description,
            "max_user_id": b.max_user_id,
            "max_username": b.max_username,
            "is_active": b.is_active,
            "created_at": b.created_at.isoformat(),
        }
        for b in bots
    ]


@router.post("/bots", status_code=201)
async def create_bot(body: BotCreate, current_user: CurrentUser, session: DBSession, _: RateLimit):
    """Создаёт бота с проверкой токена на стороне MAX API.

    Пользователь не должен сохранять заведомо нерабочий токен — поэтому
    сначала зовём /me, и только при успехе шифруем + сохраняем.
    """
    async with MaxClient(token=body.token) as client:
        try:
            me = await client.get_me()
        except MaxAPIError as exc:
            raise HTTPException(status_code=400, detail=f"Max API rejected the token: {exc}")

    try:
        encrypted = encrypt_token(body.token)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Token encryption failed: {exc}")

    bot = Bot(
        user_id=current_user.id,
        name=body.name,
        description=body.description or None,
        encrypted_token=encrypted,
        max_user_id=str(me.get("user_id", "")),
        max_username=me.get("username") or me.get("name", ""),
        is_active=True,
    )
    session.add(bot)
    await session.commit()
    await session.refresh(bot)
    return {
        "id": bot.id,
        "max_username": bot.max_username,
        "max_user_id": bot.max_user_id,
    }


@router.patch("/bots/{bot_id}")
async def update_bot(
    bot_id: int, body: BotUpdate, current_user: CurrentUser, session: DBSession, _: RateLimit
):
    result = await session.execute(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == current_user.id)
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found")

    if body.name is not None:
        bot.name = body.name
    if body.description is not None:
        bot.description = body.description
    if body.token:
        # Validate new token before accepting
        async with MaxClient(token=body.token) as client:
            try:
                me = await client.get_me()
                bot.max_user_id = str(me.get("user_id", ""))
                bot.max_username = me.get("username") or me.get("name", "")
            except MaxAPIError as exc:
                raise HTTPException(400, f"Max API rejected the token: {exc}")
        bot.encrypted_token = encrypt_token(body.token)
        bot.is_active = True  # re-enable if previously deactivated by bad token

    await session.commit()
    return {"ok": True}


@router.patch("/bots/{bot_id}/toggle")
async def toggle_bot(bot_id: int, current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == current_user.id)
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found")
    bot.is_active = not bot.is_active
    await session.commit()
    return {"is_active": bot.is_active}


@router.delete("/bots/{bot_id}", status_code=204)
async def delete_bot(bot_id: int, current_user: CurrentUser, session: DBSession, _: RateLimit):
    """Удаляет бота, аккуратно отвязывая пары (вместо каскадного DELETE).

    Каскад снёс бы всю историю пары (PostLink, ScheduledPost), а это
    полезные данные. Поэтому пары ``откалываются``: bot_id=NULL и enabled=False —
    UI покажет их как «без бота», и владелец сможет привязать другого.
    """
    result = await session.execute(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == current_user.id)
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found")

    pairs_result = await session.execute(
        select(ChannelGroupPair).where(ChannelGroupPair.bot_id == bot_id)
    )
    for pair in pairs_result.scalars().all():
        pair.bot_id = None
        pair.enabled = False

    await session.delete(bot)
    await session.commit()


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def bot_status(current_user: CurrentUser, session: DBSession, _: RateLimit):
    bots_result = await session.execute(
        select(Bot).where(Bot.user_id == current_user.id)
    )
    bots = bots_result.scalars().all()
    pairs_result = await session.execute(
        select(ChannelGroupPair).where(ChannelGroupPair.user_id == current_user.id)
    )
    pairs = pairs_result.scalars().all()

    return {
        "bots_total": len(bots),
        "bots_active": sum(1 for b in bots if b.is_active),
        "pairs_total": len(pairs),
        "pairs_active": sum(1 for p in pairs if p.enabled and p.bot_id is not None),
    }


# ── Bot chats (for pair builder) ──────────────────────────────────────────────

@router.get("/bots/{bot_id}/chats")
async def get_bot_chats(bot_id: int, current_user: CurrentUser, session: DBSession, _: RateLimit):
    """Fetch channels and groups the bot is a member of via MAX API."""
    result = await session.execute(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == current_user.id)
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found")

    try:
        token = decrypt_token(bot.encrypted_token)
    except ValueError:
        raise HTTPException(500, "Token decryption failed")

    async with MaxClient(token=token) as client:
        try:
            data = await client.get_chats()
        except MaxAPIError as exc:
            raise HTTPException(502, f"MAX API error: {exc}")

    channels = []
    groups = []
    for chat in data.get("chats", []):
        item = {
            "chat_id": str(chat["chat_id"]),
            "title": chat.get("title", ""),
            "link": chat.get("link"),  # may be None for private groups
        }
        if chat.get("type") == "channel":
            channels.append(item)
        elif chat.get("type") == "chat":
            groups.append(item)

    return {"channels": channels, "groups": groups}


@router.get("/bots/{bot_id}/groups")
async def get_bot_groups(bot_id: int, current_user: CurrentUser, session: DBSession, _: RateLimit):
    """Fetch only group chats where the bot is a member (used in welcome config add modal).
    Enriches the link from ChannelGroupPair if the Max API returned an empty value.
    """
    result = await session.execute(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == current_user.id)
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found")

    try:
        token = decrypt_token(bot.encrypted_token)
    except ValueError:
        raise HTTPException(500, "Token decryption failed")

    async with MaxClient(token=token) as client:
        try:
            data = await client.get_chats()
        except MaxAPIError as exc:
            raise HTTPException(502, f"MAX API error: {exc}")

    # Build a lookup: group_id → group_link from existing pairs (owned by this user)
    pairs_result = await session.execute(
        select(ChannelGroupPair).where(
            ChannelGroupPair.user_id == current_user.id,
            ChannelGroupPair.group_link.isnot(None),
            ChannelGroupPair.group_link != "",
        )
    )
    pair_link_map: dict[str, str] = {
        p.group_id: p.group_link
        for p in pairs_result.scalars().all()
    }

    groups = []
    for chat in data.get("chats", []):
        if chat.get("type") != "chat":
            continue
        chat_id = str(chat["chat_id"])
        link = chat.get("link") or pair_link_map.get(chat_id, "")
        groups.append({
            "chat_id": chat_id,
            "title": chat.get("title", ""),
            "link": link,
        })
    return {"groups": groups}


# ── Pairs ─────────────────────────────────────────────────────────────────────

class PairCreate(BaseModel):
    bot_id: int
    channel_id: str
    channel_name: str
    channel_link: str = ""
    group_id: str
    group_name: str
    group_link: str


@router.get("/pairs")
async def list_pairs(current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(ChannelGroupPair)
        .where(ChannelGroupPair.user_id == current_user.id)
        .order_by(ChannelGroupPair.created_at.desc())
    )
    pairs = result.scalars().all()
    return [
        {
            "id": p.id,
            "bot_id": p.bot_id,
            "channel_id": p.channel_id,
            "channel_name": p.channel_name,
            "channel_link": p.channel_link,
            "group_id": p.group_id,
            "group_name": p.group_name,
            "group_link": p.group_link,
            "enabled": p.enabled,
            "has_bot": p.bot_id is not None,
            "created_at": p.created_at.isoformat(),
        }
        for p in pairs
    ]


@router.post("/pairs", status_code=201)
async def create_pair(body: PairCreate, current_user: CurrentUser, session: DBSession, _: RateLimit):
    # Verify bot belongs to user
    bot_result = await session.execute(
        select(Bot).where(Bot.id == body.bot_id, Bot.user_id == current_user.id)
    )
    if not bot_result.scalar_one_or_none():
        raise HTTPException(404, "Bot not found")

    pair = ChannelGroupPair(
        user_id=current_user.id,
        bot_id=body.bot_id,
        channel_id=body.channel_id,
        channel_name=body.channel_name,
        channel_link=body.channel_link,
        group_id=body.group_id,
        group_name=body.group_name,
        group_link=body.group_link,
    )
    session.add(pair)
    await session.commit()
    await session.refresh(pair)
    return {"id": pair.id}


class PairUpdate(BaseModel):
    bot_id: int | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    channel_link: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    group_link: str | None = None


@router.patch("/pairs/{pair_id}")
async def update_pair(
    pair_id: int, body: PairUpdate, current_user: CurrentUser, session: DBSession, _: RateLimit
):
    result = await session.execute(
        select(ChannelGroupPair).where(
            ChannelGroupPair.id == pair_id,
            ChannelGroupPair.user_id == current_user.id,
        )
    )
    pair = result.scalar_one_or_none()
    if not pair:
        raise HTTPException(404, "Pair not found")

    if body.bot_id is not None:
        bot_result = await session.execute(
            select(Bot).where(Bot.id == body.bot_id, Bot.user_id == current_user.id)
        )
        if not bot_result.scalar_one_or_none():
            raise HTTPException(404, "Bot not found")
        pair.bot_id = body.bot_id

    if body.channel_id is not None:
        pair.channel_id = body.channel_id
    if body.channel_name is not None:
        pair.channel_name = body.channel_name
    if body.channel_link is not None:
        pair.channel_link = body.channel_link
    if body.group_id is not None:
        pair.group_id = body.group_id
    if body.group_name is not None:
        pair.group_name = body.group_name
    if body.group_link is not None:
        pair.group_link = body.group_link

    await session.commit()
    return {"ok": True}


@router.patch("/pairs/{pair_id}/toggle")
async def toggle_pair(pair_id: int, current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(ChannelGroupPair).where(
            ChannelGroupPair.id == pair_id,
            ChannelGroupPair.user_id == current_user.id,
        )
    )
    pair = result.scalar_one_or_none()
    if not pair:
        raise HTTPException(404, "Pair not found")
    if pair.bot_id is None and not pair.enabled:
        raise HTTPException(400, "Cannot enable a pair with no bot assigned")
    pair.enabled = not pair.enabled
    await session.commit()
    return {"enabled": pair.enabled}


@router.delete("/pairs/{pair_id}", status_code=204)
async def delete_pair(pair_id: int, current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(ChannelGroupPair).where(
            ChannelGroupPair.id == pair_id,
            ChannelGroupPair.user_id == current_user.id,
        )
    )
    pair = result.scalar_one_or_none()
    if not pair:
        raise HTTPException(404, "Pair not found")
    await session.delete(pair)
    await session.commit()


# ── Welcome configs (standalone verification) ─────────────────────────────────

class WelcomeConfigCreate(BaseModel):
    bot_id: int
    group_id: str
    group_name: str
    group_link: str = ""


class WelcomeConfigUpdate(BaseModel):
    group_link: str | None = None
    verification_enabled: bool | None = None
    verification_timeout_min: int | None = None
    verification_message: str | None = None
    verification_button_text: str | None = None
    verification_kick: bool | None = None
    verification_notify_success: bool | None = None
    verification_welcome_dm: str | None = None


@router.get("/welcome/configs")
async def list_welcome_configs(current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(WelcomeConfig)
        .where(WelcomeConfig.user_id == current_user.id)
        .order_by(WelcomeConfig.created_at.desc())
    )
    configs = result.scalars().all()
    return [
        {
            "id": c.id,
            "bot_id": c.bot_id,
            "group_id": c.group_id,
            "group_name": c.group_name,
            "group_link": c.group_link,
            "verification_enabled": c.verification_enabled,
            "verification_timeout_min": c.verification_timeout_min,
            "verification_message": c.verification_message,
            "verification_button_text": c.verification_button_text,
            "verification_kick": c.verification_kick,
            "verification_notify_success": c.verification_notify_success,
            "verification_welcome_dm": c.verification_welcome_dm,
            "created_at": c.created_at.isoformat(),
        }
        for c in configs
    ]


@router.post("/welcome/configs", status_code=201)
async def create_welcome_config(
    body: WelcomeConfigCreate,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    """Create a standalone verification config for a group."""
    bot_result = await session.execute(
        select(Bot).where(Bot.id == body.bot_id, Bot.user_id == current_user.id)
    )
    if not bot_result.scalar_one_or_none():
        raise HTTPException(404, "Bot not found")

    # Check for duplicates
    existing = await session.execute(
        select(WelcomeConfig).where(
            WelcomeConfig.bot_id == body.bot_id,
            WelcomeConfig.group_id == body.group_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "A welcome config for this bot+group already exists")

    # If no group_link provided, fall back to matching ChannelGroupPair
    group_link = body.group_link
    if not group_link:
        pair_result = await session.execute(
            select(ChannelGroupPair).where(
                ChannelGroupPair.group_id == body.group_id,
                ChannelGroupPair.user_id == current_user.id,
                ChannelGroupPair.group_link != "",
            )
        )
        pair = pair_result.scalar_one_or_none()
        if pair and pair.group_link:
            group_link = pair.group_link

    config = WelcomeConfig(
        user_id=current_user.id,
        bot_id=body.bot_id,
        group_id=body.group_id,
        group_name=body.group_name,
        group_link=group_link,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return {"id": config.id}


@router.patch("/welcome/configs/{config_id}")
async def update_welcome_config(
    config_id: int,
    body: WelcomeConfigUpdate,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    result = await session.execute(
        select(WelcomeConfig).where(
            WelcomeConfig.id == config_id,
            WelcomeConfig.user_id == current_user.id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Welcome config not found")

    if body.group_link is not None:
        config.group_link = body.group_link

    if body.verification_enabled is not None:
        config.verification_enabled = body.verification_enabled

    if body.verification_timeout_min is not None:
        if not 1 <= body.verification_timeout_min <= 1440:
            raise HTTPException(400, "Timeout must be between 1 and 1440 minutes")
        config.verification_timeout_min = body.verification_timeout_min

    if body.verification_message is not None:
        config.verification_message = body.verification_message or None

    if body.verification_button_text is not None:
        config.verification_button_text = body.verification_button_text or None

    if body.verification_kick is not None:
        config.verification_kick = body.verification_kick

    if body.verification_notify_success is not None:
        config.verification_notify_success = body.verification_notify_success

    if body.verification_welcome_dm is not None:
        config.verification_welcome_dm = body.verification_welcome_dm or None

    await session.commit()
    return {"ok": True}


@router.delete("/welcome/configs/{config_id}", status_code=204)
async def delete_welcome_config(
    config_id: int,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    result = await session.execute(
        select(WelcomeConfig).where(
            WelcomeConfig.id == config_id,
            WelcomeConfig.user_id == current_user.id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Welcome config not found")
    await session.delete(config)
    await session.commit()


# ── Assistant configs ─────────────────────────────────────────────────────────

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


# ── Logs ──────────────────────────────────────────────────────────────────────

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


# ── Scheduled posts ───────────────────────────────────────────────────────────

class PostCreate(BaseModel):
    pair_id: int
    text: str
    scheduled_at: datetime


@router.get("/posts")
async def list_posts(current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(ScheduledPost)
        .where(ScheduledPost.user_id == current_user.id)
        .order_by(ScheduledPost.scheduled_at.desc())
        .limit(100)
    )
    posts = result.scalars().all()
    return [
        {
            "id": p.id,
            "pair_id": p.pair_id,
            "text": p.text,
            "scheduled_at": p.scheduled_at.isoformat(),
            "status": p.status,
            "error_message": p.error_message,
        }
        for p in posts
    ]


@router.post("/posts", status_code=201)
async def create_post(body: PostCreate, current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(ChannelGroupPair).where(
            ChannelGroupPair.id == body.pair_id,
            ChannelGroupPair.user_id == current_user.id,
            ChannelGroupPair.bot_id.isnot(None),
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(404, "Pair not found or has no bot assigned")

    scheduled_at = body.scheduled_at
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)

    post = ScheduledPost(
        user_id=current_user.id,
        pair_id=body.pair_id,
        text=body.text,
        scheduled_at=scheduled_at,
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)
    return {"id": post.id}


class PostUpdate(BaseModel):
    pair_id: int | None = None
    text: str | None = None
    scheduled_at: datetime | None = None


@router.patch("/posts/{post_id}")
async def update_post(
    post_id: int, body: PostUpdate, current_user: CurrentUser, session: DBSession, _: RateLimit
):
    result = await session.execute(
        select(ScheduledPost).where(
            ScheduledPost.id == post_id,
            ScheduledPost.user_id == current_user.id,
            ScheduledPost.status == PostStatus.pending,
        )
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(404, "Post not found or already sent/cancelled")

    if body.pair_id is not None:
        pair_result = await session.execute(
            select(ChannelGroupPair).where(
                ChannelGroupPair.id == body.pair_id,
                ChannelGroupPair.user_id == current_user.id,
                ChannelGroupPair.bot_id.isnot(None),
            )
        )
        if not pair_result.scalar_one_or_none():
            raise HTTPException(404, "Pair not found or has no bot assigned")
        post.pair_id = body.pair_id

    if body.text is not None:
        post.text = body.text

    if body.scheduled_at is not None:
        scheduled_at = body.scheduled_at
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        post.scheduled_at = scheduled_at

    await session.commit()
    return {"ok": True}


@router.delete("/posts/{post_id}", status_code=204)
async def delete_post(post_id: int, current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(ScheduledPost).where(
            ScheduledPost.id == post_id,
            ScheduledPost.user_id == current_user.id,
            ScheduledPost.status == PostStatus.pending,
        )
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(404, "Post not found or already sent")
    await session.delete(post)
    await session.commit()


# ── Verification settings ─────────────────────────────────────────────────────

class VerificationUpdate(BaseModel):
    verification_enabled: bool | None = None
    verification_timeout_min: int | None = None
    verification_message: str | None = None
    verification_button_text: str | None = None
    verification_kick: bool | None = None
    verification_notify_success: bool | None = None
    verification_welcome_dm: str | None = None


@router.patch("/pairs/{pair_id}/verification")
async def update_verification(
    pair_id: int,
    body: VerificationUpdate,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    result = await session.execute(
        select(ChannelGroupPair).where(
            ChannelGroupPair.id == pair_id,
            ChannelGroupPair.user_id == current_user.id,
        )
    )
    pair = result.scalar_one_or_none()
    if not pair:
        raise HTTPException(404, "Pair not found")

    if body.verification_enabled is not None:
        if body.verification_enabled and pair.bot_id is None:
            raise HTTPException(400, "Cannot enable verification on a pair with no bot assigned")
        pair.verification_enabled = body.verification_enabled

    if body.verification_timeout_min is not None:
        if not 1 <= body.verification_timeout_min <= 1440:
            raise HTTPException(400, "Timeout must be between 1 and 1440 minutes")
        pair.verification_timeout_min = body.verification_timeout_min

    if body.verification_message is not None:
        pair.verification_message = body.verification_message or None

    if body.verification_button_text is not None:
        pair.verification_button_text = body.verification_button_text or None

    if body.verification_kick is not None:
        pair.verification_kick = body.verification_kick

    if body.verification_notify_success is not None:
        pair.verification_notify_success = body.verification_notify_success

    if body.verification_welcome_dm is not None:
        pair.verification_welcome_dm = body.verification_welcome_dm or None

    await session.commit()
    return {"ok": True}


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

    # Load bot token
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
            import logging
            logging.getLogger("web.webhook").exception("Webhook handler error: %s", exc)


# ── Инбокс бота (DM-переписки + AI-ассистент) ─────────────────────────────────
# Группа эндпоинтов /bots/{bot_id}/inbox/* для веб-страницы /bots/{id}/inbox.
# Источник данных — таблица ConversationMessage (история DM с AI),
# дополнительно подтягиваются аватары/иконки из MAX API
# (с кешированием в ConversationMessage.user_avatar для следующих запросов).


async def _require_bot_owner(bot_id: int, user_id: int, session) -> Bot:
    """Проверка ownership бота. 404, если бот чужой/не существует."""
    result = await session.execute(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == user_id)
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found")
    return bot


@router.get("/bots/{bot_id}/inbox/groups")
async def inbox_groups(bot_id: int, current_user: CurrentUser, session: DBSession, _: RateLimit):
    bot = await _require_bot_owner(bot_id, current_user.id, session)

    configs_result = await session.execute(
        select(AssistantConfig).where(AssistantConfig.bot_id == bot_id)
    )
    configs = configs_result.scalars().all()

    # Кол-во пользователей с непрочитанными сообщениями per group
    unread_per_config_result = await session.execute(
        select(
            ConversationMessage.assistant_config_id,
            sqlfunc.count(sqlfunc.distinct(ConversationMessage.max_user_id)).label("unread_users"),
        )
        .outerjoin(
            InboxReadStatus,
            (InboxReadStatus.bot_id == ConversationMessage.bot_id) &
            (InboxReadStatus.max_user_id == ConversationMessage.max_user_id),
        )
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.role == "user",
            or_(
                InboxReadStatus.last_read_at.is_(None),
                ConversationMessage.created_at > InboxReadStatus.last_read_at,
            ),
        )
        .group_by(ConversationMessage.assistant_config_id)
    )
    unread_by_config: dict[int | None, int] = {
        row.assistant_config_id: row.unread_users for row in unread_per_config_result
    }

    # Общее кол-во distinct пользователей с непрочитанными (для кнопки "Все")
    total_unread_result = await session.execute(
        select(sqlfunc.count(sqlfunc.distinct(ConversationMessage.max_user_id)))
        .outerjoin(
            InboxReadStatus,
            (InboxReadStatus.bot_id == ConversationMessage.bot_id) &
            (InboxReadStatus.max_user_id == ConversationMessage.max_user_id),
        )
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.role == "user",
            or_(
                InboxReadStatus.last_read_at.is_(None),
                ConversationMessage.created_at > InboxReadStatus.last_read_at,
            ),
        )
    )
    total_unread = total_unread_result.scalar() or 0

    # Подтягиваем иконки групп через MAX API
    chat_icons: dict[str, str | None] = {}
    try:
        import asyncio
        token = decrypt_token(bot.encrypted_token)
        async with MaxClient(token=token) as client:
            async def _fetch_icon(group_id: str) -> tuple[str, str | None]:
                try:
                    data = await client.get_chat(group_id)
                    _ic = data.get("icon")
                    icon = (
                        (_ic.get("url") if isinstance(_ic, dict) else _ic)
                        or data.get("avatar_url")
                        or data.get("photo_url")
                        or None
                    )
                    return group_id, icon
                except MaxAPIError:
                    return group_id, None
            results = await asyncio.gather(*[_fetch_icon(cfg.group_id) for cfg in configs])
            chat_icons = dict(results)
    except Exception:
        pass

    groups = [{"id": "all", "name": "Все", "count": total_unread, "icon": None}]
    for cfg in configs:
        groups.append({
            "id": cfg.id,
            "name": cfg.group_name or f"Группа {cfg.group_id}",
            "count": unread_by_config.get(cfg.id, 0),
            "icon": chat_icons.get(cfg.group_id),
        })
    return groups


@router.get("/bots/{bot_id}/inbox/users")
async def inbox_users(
    bot_id: int,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
    group_id: str = "all",
):
    bot = await _require_bot_owner(bot_id, current_user.id, session)

    q = select(
        ConversationMessage.max_user_id,
        sqlfunc.max(ConversationMessage.created_at).label("last_at"),
    ).where(ConversationMessage.bot_id == bot_id)

    if group_id != "all":
        try:
            config_id = int(group_id)
        except ValueError:
            raise HTTPException(400, "invalid group_id")
        q = q.where(ConversationMessage.assistant_config_id == config_id)

    q = q.group_by(ConversationMessage.max_user_id).order_by(sqlfunc.max(ConversationMessage.created_at).desc())

    rows = (await session.execute(q)).all()
    if not rows:
        return []

    user_ids = [r.max_user_id for r in rows]

    # Загружаем всю переписку выбранных пользователей одним запросом —
    # отсюда же берём last_msg, аватары и first_user_msg, чтобы не делать
    # три отдельных round-trip'а в БД (это и есть устранение N+1).
    # Сортировка DESC: первый встреченный msg.max_user_id и есть последний.
    last_msg_result = await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.max_user_id.in_(user_ids),
        )
        .order_by(ConversationMessage.created_at.desc())
    )
    all_msgs = last_msg_result.scalars().all()
    last_msg: dict[str, ConversationMessage] = {}
    for m in all_msgs:
        if m.max_user_id not in last_msg:
            last_msg[m.max_user_id] = m

    # user names from verification_requests (take the most recent per user)
    names_result = await session.execute(
        select(VerificationRequest.max_user_id, VerificationRequest.user_name)
        .where(VerificationRequest.max_user_id.in_(user_ids))
        .order_by(VerificationRequest.created_at.desc())
    )
    names: dict[str, str] = {}
    for uid, uname in names_result.all():
        if uid not in names and uname:
            names[uid] = uname

    # берём аватар из сохранённых сообщений; если нет — идём в MAX API
    import logging as _logging
    _avatar_log = _logging.getLogger("web.api.inbox")

    stored_avatars: dict[str, str | None] = {}
    last_user_msg: dict[str, ConversationMessage] = {}
    for msg in all_msgs:  # DESC: первый встреченный per user — самый свежий
        uid = msg.max_user_id
        if uid not in stored_avatars:
            # При первой встрече инициализируем; user_avatar берём из самого свежего сообщения
            stored_avatars[uid] = msg.user_avatar or None
        elif msg.user_avatar and not stored_avatars[uid]:
            # Более старое сообщение содержит аватар — подхватываем
            stored_avatars[uid] = msg.user_avatar
        if msg.role == "user" and uid not in last_user_msg:
            last_user_msg[uid] = msg

    missing = [uid for uid in user_ids if not stored_avatars.get(uid)]
    if missing:
        try:
            import asyncio
            token = decrypt_token(bot.encrypted_token)

            def _url_from(data: dict, *fields: str) -> str | None:
                """Извлекает URL из dict по нескольким возможным именам полей.
                Поле может быть строкой-URL или dict{"url": ...}."""
                for f in fields:
                    v = data.get(f)
                    if isinstance(v, dict):
                        u = v.get("url")
                        if u:
                            return u
                    elif isinstance(v, str) and v:
                        return v
                return None

            async with MaxClient(token=token) as client:
                async def _fetch_avatar(uid: str) -> tuple[str, str | None]:
                    # 1. /users/{uid}
                    try:
                        data = await client.get_user(uid)
                        url = _url_from(data, "avatar_url", "photo_url", "photo", "avatar")
                        if url:
                            return uid, url
                    except MaxAPIError:
                        pass
                    # 2. DM-чат из последнего сообщения пользователя
                    chat_ids: list[str] = []
                    fmsg = last_user_msg.get(uid)
                    if fmsg and fmsg.chat_id and fmsg.chat_id != uid:
                        chat_ids.append(fmsg.chat_id)
                    # 3. В MAX DM chat_id зачастую == user_id — пробуем напрямую
                    chat_ids.append(uid)
                    for cid in chat_ids:
                        try:
                            data = await client.get_chat(cid)
                            # Стандартные поля иконки чата
                            url = _url_from(data, "icon", "avatar_url", "photo_url", "photo", "avatar")
                            if url:
                                return uid, url
                            # MAX для диалогов (type=dialog) кладёт инфо о
                            # собеседнике в dialog_with_user — там есть photo/avatar_url.
                            dwu = data.get("dialog_with_user") or data.get("owner")
                            if isinstance(dwu, dict):
                                url = _url_from(dwu, "avatar_url", "photo_url", "photo", "avatar", "full_avatar_url")
                                if url:
                                    return uid, url
                            # Логируем ключи для диагностики (один раз на пользователя)
                            _avatar_log.warning(
                                "Avatar fields not found for uid=%s cid=%s, keys=%s",
                                uid, cid, list(data.keys()),
                            )
                        except MaxAPIError:
                            pass
                    return uid, None

                fetched = dict(await asyncio.gather(*[_fetch_avatar(uid) for uid in missing]))

            # Кешируем в БД в самое свежее сообщение пользователя, где avatar ещё не стоит
            for uid, url in fetched.items():
                if url:
                    stored_avatars[uid] = url
                    msg_to_update = last_user_msg.get(uid)
                    if msg_to_update and not msg_to_update.user_avatar:
                        msg_to_update.user_avatar = url
            await session.commit()
        except Exception as exc:
            _avatar_log.warning("Avatar fetch failed for bot %s: %s", bot_id, exc)

    # Кол-во непрочитанных сообщений per user
    unread_result = await session.execute(
        select(
            ConversationMessage.max_user_id,
            sqlfunc.count(ConversationMessage.id).label("unread_count"),
        )
        .outerjoin(
            InboxReadStatus,
            (InboxReadStatus.bot_id == ConversationMessage.bot_id) &
            (InboxReadStatus.max_user_id == ConversationMessage.max_user_id),
        )
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.max_user_id.in_(user_ids),
            ConversationMessage.role == "user",
            or_(
                InboxReadStatus.last_read_at.is_(None),
                ConversationMessage.created_at > InboxReadStatus.last_read_at,
            ),
        )
        .group_by(ConversationMessage.max_user_id)
    )
    unread_counts: dict[str, int] = {row.max_user_id: row.unread_count for row in unread_result}

    _ATT_LABELS = {
        "image": "📷 Фото", "video": "🎥 Видео",
        "audio": "🎤 Голосовое", "file": "📎 Файл",
        "share": "🔗 Ссылка",
    }

    users = []
    for r in rows:
        uid = r.max_user_id
        m = last_msg.get(uid)
        last_text = (m.content or "").strip() if m else ""
        if not last_text and m:
            # Если нет текста — показываем тип первого вложения
            try:
                atts = json.loads(m.attachments_json or "[]")
                if atts:
                    last_text = _ATT_LABELS.get(atts[0].get("type", ""), "📎 Вложение")
            except Exception:
                pass
        users.append({
            "user_id": uid,
            "name": names.get(uid) or f"User {uid}",
            "avatar": stored_avatars.get(uid),
            "unread_count": unread_counts.get(uid, 0),
            "last_message": last_text[:200],
            "last_role": m.role if m else "",
            "last_at": r.last_at.isoformat() if r.last_at else None,
        })
    return users


@router.get("/bots/{bot_id}/inbox/messages")
async def inbox_messages(
    bot_id: int,
    user_id: str,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
    group_id: str = "all",
):
    await _require_bot_owner(bot_id, current_user.id, session)

    q = select(ConversationMessage).where(
        ConversationMessage.bot_id == bot_id,
        ConversationMessage.max_user_id == user_id,
    )
    if group_id != "all":
        try:
            config_id = int(group_id)
        except ValueError:
            raise HTTPException(400, "invalid group_id")
        q = q.where(ConversationMessage.assistant_config_id == config_id)

    q = q.order_by(ConversationMessage.created_at.asc())
    result = await session.execute(q)
    msgs = result.scalars().all()

    # Помечаем как прочитанные
    now = datetime.now(timezone.utc)
    stmt = pg_insert(InboxReadStatus).values(
        bot_id=bot_id,
        max_user_id=user_id,
        last_read_at=now,
    ).on_conflict_do_update(
        constraint="uq_inbox_read_bot_user",
        set_={"last_read_at": now},
    )
    await session.execute(stmt)
    await session.commit()

    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "attachments": json.loads(m.attachments_json or "[]"),
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


@router.get("/bots/{bot_id}/inbox/profile/{user_id}")
async def inbox_profile(
    bot_id: int,
    user_id: str,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    """Профиль пользователя в инбоксе.

    Возвращает агрегированную информацию: базовый профиль, статистику,
    группы (через UserBotContext), ссылки из текста и вложения по типам.
    Используется правым профильным панелью в inbox.html.
    """
    import re

    await _require_bot_owner(bot_id, current_user.id, session)

    # Все сообщения пользователя (ASC — нужны для first/last)
    msgs_result = await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.max_user_id == user_id,
        )
        .order_by(ConversationMessage.created_at.asc())
    )
    msgs = msgs_result.scalars().all()
    if not msgs:
        raise HTTPException(404, "No messages found for this user")

    first_msg = msgs[0]
    last_msg_obj = msgs[-1]

    # Аватар — берём из самого свежего сообщения с непустым user_avatar
    avatar = None
    for m in reversed(msgs):
        if m.user_avatar:
            avatar = m.user_avatar
            break

    # Имя — из VerificationRequest (самый свежий)
    name_result = await session.execute(
        select(VerificationRequest.user_name)
        .where(VerificationRequest.max_user_id == user_id)
        .order_by(VerificationRequest.created_at.desc())
        .limit(1)
    )
    name = name_result.scalar() or f"User {user_id}"

    # Статистика сообщений
    user_msg_count = sum(1 for m in msgs if m.role == "user")
    bot_msg_count = sum(1 for m in msgs if m.role == "assistant")

    # Группы пользователя (через UserBotContext → AssistantConfig)
    contexts_result = await session.execute(
        select(UserBotContext)
        .where(
            UserBotContext.bot_id == bot_id,
            UserBotContext.max_user_id == user_id,
        )
        .options(selectinload(UserBotContext.assistant_config))
    )
    groups = []
    for ctx in contexts_result.scalars().all():
        cfg = ctx.assistant_config
        groups.append({
            "group_id": ctx.group_id,
            "group_name": cfg.group_name if cfg else ctx.group_id,
        })

    # Ссылки — regex-поиск по тексту сообщений (последние 100)
    _url_re = re.compile(r"https?://[^\s<>\"'{}|\\^`\[\]]+")
    links: list[dict] = []
    for m in msgs:
        if m.content:
            for found_url in _url_re.findall(m.content):
                links.append({
                    "url": found_url,
                    "date": m.created_at.isoformat(),
                    "role": m.role,
                })

    # Вложения по типу (из attachments_json)
    media: list[dict] = []
    files: list[dict] = []
    voices: list[dict] = []
    for m in msgs:
        try:
            atts = json.loads(m.attachments_json or "[]")
        except Exception:
            atts = []
        for att in atts:
            entry = {**att, "date": m.created_at.isoformat(), "role": m.role}
            t = att.get("type", "")
            if t in ("image", "video"):
                media.append(entry)
            elif t == "audio":
                voices.append(entry)
            elif t == "file":
                files.append(entry)

    return {
        "user_id": user_id,
        "name": name,
        "avatar": avatar,
        "first_contact": first_msg.created_at.isoformat(),
        "last_active": last_msg_obj.created_at.isoformat(),
        "user_msg_count": user_msg_count,
        "bot_msg_count": bot_msg_count,
        "groups": groups,
        "links": links[-100:],
        "media": media[-100:],
        "files": files[-100:],
        "voices": voices[-100:],
    }


class InboxSendRequest(BaseModel):
    user_id: str
    text: str = ""
    # Вложения в «storage»-формате: [{type, token, filename?, size?}]
    # Бэкенд конвертирует в MAX API-формат при отправке.
    attachments: list[dict] | None = None
    assistant_config_id: int | None = None


@router.post("/bots/{bot_id}/inbox/send")
async def inbox_send(
    bot_id: int,
    body: InboxSendRequest,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    if not body.text and not body.attachments:
        raise HTTPException(400, "Either text or attachments must be provided")

    bot = await _require_bot_owner(bot_id, current_user.id, session)

    # Ищем реальный chat_id из истории сообщений пользователя
    last_msg_result = await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.max_user_id == body.user_id,
            ConversationMessage.chat_id.isnot(None),
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(1)
    )
    last_msg = last_msg_result.scalar_one_or_none()
    chat_id = last_msg.chat_id if last_msg else body.user_id

    try:
        token = decrypt_token(bot.encrypted_token)
    except ValueError:
        raise HTTPException(500, "Token decryption failed")

    # Конвертируем storage-формат → MAX API-формат для отправки
    max_atts: list[dict] | None = None
    if body.attachments:
        max_atts = [
            {"type": att["type"], "payload": {"token": att["token"]}}
            for att in body.attachments
            if att.get("token")
        ] or None

    async with MaxClient(token=token) as client:
        try:
            max_resp = await client.send_message(
                chat_id=chat_id,
                text=body.text or "",
                attachments=max_atts,
                format="markdown",
            )
        except MaxAPIError as e:
            raise HTTPException(502, f"Max API error: {e}")

    # Пытаемся извлечь реальные URL изображений из ответа MAX API.
    # MAX возвращает Message-объект; вложения могут быть в разных форматах:
    #   payload.photo/thumbnail → {"url": "..."}
    #   payload.photos          → {"<size>": {"url": "..."}, ...}
    atts_to_store: list[dict] = list(body.attachments or [])
    if atts_to_store and isinstance(max_resp, dict):
        resp_body = (
            max_resp.get("message", {}).get("body", {})
            or max_resp.get("body", {})
        )
        resp_atts = resp_body.get("attachments", []) if isinstance(resp_body, dict) else []
        import logging as _sl; _sl.getLogger("web.api.inbox.send").info(
            "inbox_send MAX response atts bot=%s resp_atts=%r", bot_id, resp_atts
        )
        for i, stored_att in enumerate(atts_to_store):
            # Ищем соответствующее вложение в ответе по индексу (порядок совпадает)
            resp_att = resp_atts[i] if i < len(resp_atts) else {}
            payload = resp_att.get("payload", {}) if isinstance(resp_att, dict) else {}
            att_type = stored_att.get("type", "")
            new_att = dict(stored_att)

            # Извлекаем прямую ссылку на файл из ответа MAX
            # Вариант 0: payload.url — основной формат ответа MAX на POST /messages
            direct_url: str | None = None
            if isinstance(payload.get("url"), str) and payload["url"]:
                direct_url = payload["url"]
            # Вариант 1: payload.photo / payload.thumbnail (изображения в некоторых форматах)
            if not direct_url:
                for key in ("photo", "thumbnail"):
                    thumb = payload.get(key)
                    if isinstance(thumb, dict) and thumb.get("url"):
                        direct_url = thumb["url"]
                        break
                    elif isinstance(thumb, str) and thumb:
                        direct_url = thumb
                        break
            # Вариант 2: payload.photos dict {"<size>": {"url": ...}}
            if not direct_url:
                photos_dict = payload.get("photos")
                if isinstance(photos_dict, dict):
                    for pv in photos_dict.values():
                        if isinstance(pv, dict) and pv.get("url"):
                            direct_url = pv["url"]
                            break

            if direct_url:
                new_att["url"] = direct_url
                # Для изображений preview_url = та же ссылка (используется как src для <img>)
                if att_type == "image":
                    new_att["preview_url"] = direct_url
            # Blob URL не переживёт перезагрузку — убираем, чтобы не хранить мусор
            if new_att.get("preview_url", "").startswith("blob:"):
                del new_att["preview_url"]
            atts_to_store[i] = new_att

    msg = ConversationMessage(
        bot_id=bot_id,
        max_user_id=body.user_id,
        chat_id=chat_id,
        assistant_config_id=body.assistant_config_id,
        role="assistant",
        content=body.text or "",
        attachments_json=json.dumps(atts_to_store, ensure_ascii=False),
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return {"ok": True, "id": msg.id, "created_at": msg.created_at.isoformat()}


@router.post("/bots/{bot_id}/inbox/upload")
async def inbox_upload(
    bot_id: int,
    file: UploadFile,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    """Загружает файл в MAX API и возвращает токен вложения.

    Клиент сначала вызывает этот эндпоинт (получает token),
    потом передаёт token в /inbox/send в поле attachments.
    Поддерживаемые типы: изображения, видео, аудио, произвольные файлы.
    Максимальный размер: 20 МБ.
    """
    MAX_SIZE = 20 * 1024 * 1024  # 20 МБ

    bot = await _require_bot_owner(bot_id, current_user.id, session)

    try:
        token = decrypt_token(bot.encrypted_token)
    except ValueError:
        raise HTTPException(500, "Token decryption failed")

    ct = file.content_type or "application/octet-stream"
    filename = file.filename or "file"

    # MAX API принимает type=image|video|audio|file (не "photo")
    if ct.startswith("image/"):
        att_type, store_type = "image", "image"
    elif ct.startswith("video/"):
        att_type, store_type = "video", "video"
    elif ct.startswith("audio/"):
        att_type, store_type = "audio", "audio"
    else:
        att_type, store_type = "file", "file"

    file_bytes = await file.read()
    if len(file_bytes) > MAX_SIZE:
        raise HTTPException(413, "Файл слишком большой (максимум 20 МБ)")

    import logging as _upload_log
    _ul = _upload_log.getLogger("web.api.inbox.upload")

    async with MaxClient(token=token) as client:
        try:
            result = await client.upload_attachment(file_bytes, filename, ct, att_type)
        except MaxAPIError as e:
            _ul.error(
                "MAX upload failed bot=%s att_type=%s filename=%s status=%s body=%r",
                bot_id, att_type, filename, e.status, e.body,
            )
            raise HTTPException(502, f"MAX API upload error (status {e.status}): {e.body}")

    # MAX возвращает разные структуры в зависимости от типа:
    #   файлы/аудио/видео → {"token": "..."}
    #   картинки          → {"photos": {"<photo_id>": {"token": "..."}, ...}}
    #                    или {"photos": ["<token>", ...]}  (старый формат)
    _ul.info("MAX upload raw result bot=%s att_type=%s result=%r", bot_id, att_type, result)
    photos_val = result.get("photos")
    photos_token = ""
    if isinstance(photos_val, dict):
        # {"<photo_id>": {"token": "..."}} — берём первый элемент, затем его token
        first_photo = next(iter(photos_val.values()), None)
        if isinstance(first_photo, dict):
            photos_token = first_photo.get("token") or first_photo.get("file_id") or ""
        elif isinstance(first_photo, str):
            photos_token = first_photo
    elif isinstance(photos_val, list) and photos_val:
        # ["<token>", ...] — строка или dict с token
        item = photos_val[0]
        if isinstance(item, str):
            photos_token = item
        elif isinstance(item, dict):
            photos_token = item.get("token") or item.get("file_id") or ""
    upload_token = (
        result.get("token")
        or result.get("file_id")
        or photos_token
        or ""
    )
    if not upload_token:
        raise HTTPException(502, f"MAX API не вернул token: {result}")

    # storage-формат — то, что хранится в attachments_json и передаётся в /send
    store_att: dict = {"type": store_type, "token": upload_token, "size": len(file_bytes)}
    if store_type == "file":
        store_att["filename"] = filename

    return {
        "ok": True,
        "attachment": store_att,
        "type": store_type,
        "filename": filename,
        "size": len(file_bytes),
    }


@router.get("/bots/{bot_id}/inbox/proxy")
async def inbox_proxy_download(
    bot_id: int,
    url: str,
    filename: str | None = None,
    current_user: CurrentUser,
    session: DBSession,
):
    """Прокси-скачивание файла с MAX CDN с правильным Content-Disposition.

    CDN MAX не возвращает имя файла в заголовках, а атрибут ``download``
    в HTML работает только для same-origin URL. Этот эндпоинт скачивает
    файл с CDN и отдаёт клиенту с нужным ``Content-Disposition``.

    SSRF-защита: разрешены только домены ``*.oneme.ru`` и ``*.max.ru``.
    """
    from urllib.parse import urlparse, quote as _quote

    _host = urlparse(url).hostname or ""
    if not (_host.endswith(".oneme.ru") or _host.endswith(".max.ru")):
        raise HTTPException(400, "URL не разрешён")

    await _require_bot_owner(bot_id, current_user.id, session)

    async def _stream():
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as _c:
            async with _c.stream("GET", url) as _resp:
                async for chunk in _resp.aiter_bytes(65536):
                    yield chunk

    resp_headers: dict[str, str] = {}
    if filename:
        # RFC 5987: поддержка UTF-8 имён файлов во всех браузерах
        safe_ascii = filename.encode("ascii", errors="replace").decode()
        encoded = _quote(filename, safe="")
        resp_headers["Content-Disposition"] = (
            f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded}'
        )
    else:
        resp_headers["Content-Disposition"] = "attachment"

    return StreamingResponse(
        _stream(),
        media_type="application/octet-stream",
        headers=resp_headers,
    )
