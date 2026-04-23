"""
Scheduler service — publishes ScheduledPosts at their scheduled_at time.

Every 30 seconds queries for pending posts whose scheduled_at <= now.
Uses the token of the bot assigned to the post's pair.
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.buttons import comment_button, discussion_header
from bot.client import MaxAPIError, MaxClient
from bot.crypto import decrypt_token
from db.models import (
    Base, Bot, ChannelGroupPair, EventLog, LogLevel, PostLink,
    PostStatus, ScheduledPost, VerificationRequest, VerificationStatus,
)
from db.session import AsyncSessionLocal, async_engine
from shared.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler.main")
settings = get_settings()


async def _ensure_schema() -> None:
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def publish_due_posts() -> None:
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.status == PostStatus.pending,
                ScheduledPost.scheduled_at <= now,
            )
            .options(
                selectinload(ScheduledPost.pair).selectinload(ChannelGroupPair.bot)
            )
            .order_by(ScheduledPost.scheduled_at)
            .limit(20)
        )
        posts = result.scalars().all()

    if not posts:
        return

    logger.info("Publishing %d scheduled post(s)", len(posts))

    for post in posts:
        await _publish_one(post)


async def _publish_one(post: ScheduledPost) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ScheduledPost)
            .where(ScheduledPost.id == post.id)
            .options(selectinload(ScheduledPost.pair).selectinload(ChannelGroupPair.bot))
        )
        post = result.scalar_one()
        pair: ChannelGroupPair | None = post.pair
        bot: Bot | None = pair.bot if pair else None

        # Guard: pair must exist, have a bot, and be enabled
        if not pair or not pair.enabled or not pair.bot_id:
            post.status = PostStatus.failed
            post.error_message = "Pair has no assigned bot or is disabled"
            await session.commit()
            return

        if not bot or not bot.is_active:
            post.status = PostStatus.failed
            post.error_message = f"Bot (id={pair.bot_id}) is inactive or deleted"
            await session.commit()
            return

        try:
            token = decrypt_token(bot.encrypted_token)
        except ValueError as exc:
            post.status = PostStatus.failed
            post.error_message = f"Token decryption failed: {exc}"
            await session.commit()
            return

        attachments = json.loads(post.attachments_json or "[]")
        button = comment_button(pair.group_link)
        attachments.append(button)

        async with MaxClient(token=token) as client:
            try:
                # Publish to channel
                resp = await client.send_message(
                    chat_id=pair.channel_id,
                    text=post.text,
                    attachments=attachments,
                )
                channel_msg_id = str(resp.get("message", {}).get("body", {}).get("mid", ""))

                # Duplicate to discussion group
                group_text = discussion_header(pair.channel_name, channel_msg_id, pair.channel_link or "") + post.text
                group_resp = await client.send_message(
                    chat_id=pair.group_id,
                    text=group_text,
                )
                group_msg_id = str(group_resp.get("message", {}).get("body", {}).get("mid", ""))

                session.add(PostLink(
                    pair_id=pair.id,
                    channel_post_id=channel_msg_id,
                    group_message_id=group_msg_id,
                ))
                post.status = PostStatus.sent
                session.add(EventLog(
                    user_id=post.user_id,
                    bot_id=bot.id,
                    level=LogLevel.info,
                    message=(
                        f"Scheduled post {post.id} published via bot '{bot.name}' "
                        f"to channel {pair.channel_id} (msg {channel_msg_id})"
                    ),
                ))
                logger.info("Scheduled post %d published (channel msg %s)", post.id, channel_msg_id)

            except MaxAPIError as exc:
                post.status = PostStatus.failed
                post.error_message = str(exc)
                session.add(EventLog(
                    user_id=post.user_id,
                    bot_id=bot.id,
                    level=LogLevel.error,
                    message=f"Scheduled post {post.id} failed via bot '{bot.name}': {exc}",
                ))
                logger.error("Scheduled post %d failed: %s", post.id, exc)

        await session.commit()


_DEFAULT_KICK_MSG = "❌ **{имя}** не прошёл(а) проверку вовремя и был(а) исключён(а) из группы."


async def kick_expired_verifications() -> None:
    """
    Every 60 s: find pending verification requests past their deadline,
    kick the user from the group, and mark the request as 'kicked'.
    """
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(VerificationRequest)
            .where(
                VerificationRequest.status == VerificationStatus.pending,
                VerificationRequest.deadline <= now,
            )
            .options(
                selectinload(VerificationRequest.pair).selectinload(ChannelGroupPair.bot)
            )
            .limit(50)
        )
        expired: list[VerificationRequest] = result.scalars().all()

    if not expired:
        return

    logger.info("Processing %d expired verification request(s)", len(expired))

    for vr in expired:
        await _process_expired(vr)


async def _process_expired(vr: VerificationRequest) -> None:
    async with AsyncSessionLocal() as session:
        # Reload with relationships
        result = await session.execute(
            select(VerificationRequest)
            .where(VerificationRequest.id == vr.id)
            .options(
                selectinload(VerificationRequest.pair).selectinload(ChannelGroupPair.bot)
            )
        )
        vr = result.scalar_one_or_none()
        if not vr or vr.status != VerificationStatus.pending:
            return  # race condition — already handled

        pair: ChannelGroupPair = vr.pair
        bot: Bot | None = pair.bot if pair else None

        if not pair or not pair.enabled:
            vr.status = VerificationStatus.expired
            await session.commit()
            return

        if not bot or not bot.is_active:
            vr.status = VerificationStatus.expired
            session.add(EventLog(
                user_id=pair.user_id, bot_id=None, level=LogLevel.warning,
                message=f"Cannot kick {vr.user_name}: bot is inactive (pair {pair.id})",
            ))
            await session.commit()
            return

        try:
            token = decrypt_token(bot.encrypted_token)
        except ValueError:
            vr.status = VerificationStatus.expired
            await session.commit()
            return

        vr.status = VerificationStatus.kicked

        async with MaxClient(token=token) as client:
            # 1. Kick the user if configured
            if pair.verification_kick:
                try:
                    await client.kick_member(chat_id=pair.group_id, user_id=vr.max_user_id)
                    logger.info(
                        "Kicked %s (%s) from group %s (verification timeout)",
                        vr.user_name, vr.max_user_id, pair.group_id,
                    )
                except MaxAPIError as exc:
                    logger.warning(
                        "Could not kick %s from group %s: %s",
                        vr.max_user_id, pair.group_id, exc,
                    )

            # 2. Delete group verification message to keep the chat clean
            if vr.group_message_id:
                try:
                    await client.delete_message(message_id=vr.group_message_id)
                except MaxAPIError as exc:
                    logger.warning(
                        "Could not delete verification message %s: %s",
                        vr.group_message_id, exc,
                    )

        session.add(EventLog(
            user_id=pair.user_id,
            bot_id=bot.id,
            level=LogLevel.info,
            message=(
                f"Verification expired: {vr.user_name} ({vr.max_user_id}) "
                f"kicked from group {pair.group_id} (pair {pair.id})"
            ),
        ))
        await session.commit()


async def main() -> None:
    await _ensure_schema()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        publish_due_posts,
        trigger="interval",
        seconds=30,
        id="publish_scheduled",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        kick_expired_verifications,
        trigger="interval",
        seconds=60,
        id="kick_expired_verifications",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Scheduler started — posts every 30 s, verification kicks every 60 s")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
