"""
Event handlers for Max Bot API updates.

Flow for new channel post:
  1. Look up the ChannelGroupPair for this channel + bot.
  2. Duplicate post to the discussion group.
  3. Try to edit the original post to add inline button (works only if bot authored it).
  4. If edit fails (403/human-authored), post a reply-with-button instead.
  5. Save PostLink: channel_post_id → group_message_id.
  6. Write EventLog entry.
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
    original_attachments = _extract_media_attachments(message_body)

    try:
        group_resp = await client.send_message(
            chat_id=pair.group_id,
            text=group_text,
            attachments=original_attachments or None,
        )
    except MaxAPIError as exc:
        logger.error("Failed to duplicate post to group: %s", exc)
        await _log(session, pair.user_id, bot_id, LogLevel.error,
                   f"Duplicate failed for post {message_id}: {exc}")
        await session.commit()
        return

    group_message_id = str(group_resp.get("message", {}).get("mid", ""))

    # ── Step 2: Add inline button to original channel post ───────────────────
    button = comment_button(pair.group_link, group_message_id)
    # Pass original attachments so link previews (share) are preserved after edit
    edited = await _try_edit_with_button(
        client, chat_id, message_id, text, button, original_attachments
    )

    if not edited:
        # Fallback: post a reply with the button in the channel
        logger.info("Cannot edit post %s (human-authored). Posting reply-with-button.", message_id)
        try:
            await client.reply_to_message(
                chat_id=chat_id,
                message_id=message_id,
                text="",
                attachments=[button],
            )
        except MaxAPIError as exc:
            logger.warning("Reply-with-button failed: %s", exc)
            await _log(session, pair.user_id, bot_id, LogLevel.warning,
                       f"Could not attach button to post {message_id}: {exc}")

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


async def _try_edit_with_button(
    client: MaxClient,
    chat_id: str,
    message_id: str,
    text: str,
    button: dict,
    original_attachments: list[dict] | None = None,
) -> bool:
    """
    Attempts to edit the original post to attach an inline button.
    Preserves original attachments (link previews, media) by placing them
    before the button in the attachments array.
    Returns True on success, False on permission error.
    """
    attachments = list(original_attachments or []) + [button]
    try:
        await client.edit_message(message_id=message_id, text=text or "", attachments=attachments)
        return True
    except MaxAPIError as exc:
        if exc.status in (403, 400):
            return False
        logger.warning("Unexpected error editing message %s: %s", message_id, exc)
        return False


def _extract_media_attachments(body: dict) -> list[dict]:
    """
    Extract re-sendable attachments from a message body.
    Captures: image, video, audio, file (by token) and share/link previews (by token).
    """
    attachments = []
    for att in body.get("attachments", []):
        att_type = att.get("type", "")
        payload = att.get("payload", {})
        token = payload.get("token")
        if att_type in ("image", "video", "audio", "file", "share") and token:
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
