"""Пары канал↔группа: CRUD-эндпоинты /pairs/*."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from db.models import Bot, ChannelGroupPair
from web.deps import CurrentUser, DBSession, RateLimit

router = APIRouter()


class PairCreate(BaseModel):
    bot_id: int
    channel_id: str
    channel_name: str
    channel_link: str = ""
    group_id: str
    group_name: str
    group_link: str


class PairUpdate(BaseModel):
    bot_id: int | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    channel_link: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    group_link: str | None = None


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
