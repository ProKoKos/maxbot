"""
Event handlers for Max Bot API updates.

Flow for new channel post:
  1. Look up the ChannelGroupPair for this channel + bot.
  2. Duplicate post to the discussion group.
  3. Send a standalone button message to the channel (preserves original post untouched).
  4. Save PostLink: channel_post_id → group_message_id.
  5. Write EventLog entry.
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.buttons import comment_button, discussion_header
from bot.client import MaxAPIError, MaxClient
from db.models import ChannelGroupPair, EventLog, LogLevel, PostLink

logger = logging.getLogger(__name__)


async def handle_update(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """Dispatch a single update dict to the appropriate handler."""
    update_type = update.get("update_type") or update.get("type", "")

    if update_type == "message_created":
        await _handle_message_created(update, session, client, bot_id=bot_id)
    elif update_type == "bot_started":
        logger.info("bot_started event from user %s", update.get("user", {}).get("user_id"))
    else:
        logger.debug("Unhandled update type: %s", update_type)


async def _handle_message_created(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    message = update.get("message", {})
    chat = message.get("recipient", {})
    chat_type = chat.get("chat_type", "")

    if chat_type != "channel":
        return

    chat_id = str(chat.get("chat_id", ""))
    message_body = message.get("body", {})
    message_id = str(message_body.get("mid", ""))
    text = message_body.get("text", "")

    if not chat_id or not message_id:
        return

    # Find an active pair for this channel operated by this specific bot
    query = select(ChannelGroupPair).where(
        ChannelGroupPair.channel_id == chat_id,
        ChannelGroupPair.enabled == True,  # noqa: E712
    )
    if bot_id is not None:
        query = query.where(ChannelGroupPair.bot_id == bot_id)

    result = await session.execute(query)
    pair = result.scalar_one_or_none()
    if not pair:
        return

    # Avoid duplicate processing
    existing = await session.execute(
        select(PostLink).where(PostLink.channel_post_id == message_id)
    )
    if existing.scalar_one_or_none():
        logger.debug("Post %s already processed, skipping", message_id)
        return

    logger.info(
        "New channel post %s in %s (bot=%s, pair=%d) → group %s",
        message_id, chat_id, bot_id, pair.id, pair.group_id,
    )

    # ── Step 1: Duplicate post to discussion group ────────────────────────────
    header = discussion_header(pair.channel_name or chat_id, message_id)
    group_text = header + (text or "")
    # Only forward media attachments — share tokens are context-specific and
    # invalid in a different chat. URL in text will auto-generate a fresh preview.
    forwardable_attachments = _extract_media_attachments(message_body)

    try:
        group_resp = await client.send_message(
            chat_id=pair.group_id,
            text=group_text,
            attachments=forwardable_attachments or None,
        )
    except MaxAPIError as exc:
        logger.error("Failed to duplicate post to group: %s", exc)
        await _log(session, pair.user_id, bot_id, LogLevel.error,
                   f"Duplicate failed for post {message_id}: {exc}")
        await session.commit()
        return

    group_message_id = str(group_resp.get("message", {}).get("mid", ""))

    # ── Step 2: Post button as standalone message in the channel ─────────────
    # We intentionally do NOT edit the original post (editing removes link previews)
    # and do NOT use reply (reply shows an ugly quote of the original).
    # A plain message with just the inline keyboard is the cleanest approach.
    button = comment_button(pair.group_link, group_message_id)
    try:
        await client.send_message(
            chat_id=chat_id,
            text="\u200b",   # zero-width space — satisfies required text field
            attachments=[button],
            notify=False,
        )
    except MaxAPIError as exc:
        logger.warning("Failed to send button to channel: %s", exc)
        await _log(session, pair.user_id, bot_id, LogLevel.warning,
                   f"Could not send button for post {message_id}: {exc}")

    # ── Step 3: Persist link ──────────────────────────────────────────────────
    session.add(PostLink(
        pair_id=pair.id,
        channel_post_id=message_id,
        group_message_id=group_message_id,
    ))
    await _log(
        session, pair.user_id, bot_id, LogLevel.info,
        f"Post {message_id} duplicated → group message {group_message_id} (pair {pair.id})"
    )
    await session.commit()



def _extract_media_attachments(body: dict) -> list[dict]:
    """
    Extract attachments that can be forwarded to another chat (by token).
    Captures: image, video, audio, file.
    NOTE: "share" (link preview) tokens are context-specific and cannot be
    re-sent to a different chat — so they are intentionally excluded here.
    The URL in the text will auto-generate a fresh preview in the group.
    """
    attachments = []
    for att in body.get("attachments", []):
        att_type = att.get("type", "")
        payload = att.get("payload", {})
        token = payload.get("token")
        if att_type in ("image", "video", "audio", "file") and token:
            attachments.append({"type": att_type, "payload": {"token": token}})
    return attachments




async def _log(
    session: AsyncSession,
    user_id: int | None,
    bot_id: int | None,
    level: LogLevel,
    message: str,
) -> None:
    session.add(EventLog(user_id=user_id, bot_id=bot_id, level=level, message=message))
