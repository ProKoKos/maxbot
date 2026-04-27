"""Отложенные посты: CRUD-эндпоинты /posts/*."""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from db.models import ChannelGroupPair, PostStatus, ScheduledPost
from web.deps import CurrentUser, DBSession, RateLimit

router = APIRouter()


class PostCreate(BaseModel):
    pair_id: int
    text: str
    scheduled_at: datetime


class PostUpdate(BaseModel):
    pair_id: int | None = None
    text: str | None = None
    scheduled_at: datetime | None = None


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
