"""Боты и Dashboard: CRUD-эндпоинты /bots/*, /status, /bots/{id}/chats, /bots/{id}/groups."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from bot.client import MaxAPIError, MaxClient
from bot.crypto import decrypt_token, encrypt_token
from db.models import Bot, ChannelGroupPair
from web.deps import CurrentUser, DBSession, RateLimit

router = APIRouter()


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
    полезные данные. Поэтому пары «откалываются»: bot_id=NULL и enabled=False —
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
