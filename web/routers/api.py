"""
REST API routes (JSON).
Used by the UI via fetch() and by external integrations.
All routes (except /login and /webhook) require JWT.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from bot.client import MaxAPIError, MaxClient
from bot.crypto import decrypt_token, encrypt_token
from bot.handlers import handle_update
from db.models import (
    Bot,
    ChannelGroupPair,
    EventLog,
    LogLevel,
    PostStatus,
    ScheduledPost,
    Subscription,
    User,
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
    # Validate token against Max API before saving
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
    result = await session.execute(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == current_user.id)
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found")

    # Detach all pairs: set bot_id=NULL, disable them
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


# ── Pairs ─────────────────────────────────────────────────────────────────────

class PairCreate(BaseModel):
    bot_id: int
    channel_id: str
    channel_name: str
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
        group_id=body.group_id,
        group_name=body.group_name,
        group_link=body.group_link,
    )
    session.add(pair)
    await session.commit()
    await session.refresh(pair)
    return {"id": pair.id}


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


# ── Webhook endpoint (for BOT_MODE=webhook) ───────────────────────────────────

@router.post("/webhook/{bot_id}")
async def webhook(bot_id: int, request: Request, session: DBSession):
    """
    Receives POST from Max servers for a specific bot.
    URL pattern: /api/webhook/{bot_id}
    Validates X-Max-Bot-Api-Secret header.
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

    return {"ok": True}
