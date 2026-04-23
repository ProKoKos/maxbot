"""
Event handlers for Max Bot API updates.

Flow for new channel post:
  1. Look up the ChannelGroupPair for this channel + bot.
  2. Duplicate post to the discussion group.
  3. Edit the original channel post to add the inline button.
     - First try: edit with [share_url_att + button] to preserve link preview.
     - Fallback: edit with just [button] (preview disappears but button is clean).
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
    header = discussion_header(pair.channel_name or chat_id, message_id, pair.channel_link or "")
    group_text = header + (text or "")
    # Only forward media attachments (image/video/audio/file).
    # share tokens are context-specific — invalid in a different chat.
    # URL in text will auto-generate a fresh preview in the group.
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

    # ── Step 2: Edit original channel post to add button ─────────────────────
    # Strategy A: edit with [share_by_url + button] — preserves link preview.
    # Strategy B: edit with [button] only — clean but preview disappears.
    button = comment_button(pair.group_link, group_message_id)

    # Extract share URL from original attachment (if any)
    share_url = _extract_share_url(message_body)

    edited = False
    if share_url:
        # Try to rebuild the share preview using the URL (not the token)
        share_att = {"type": "share", "payload": {"url": share_url}}
        edited = await _try_edit(client, message_id, text, [share_att, button])
        if not edited:
            logger.info("Edit with share_url failed for %s, retrying with button only", message_id)

    if not edited:
        edited = await _try_edit(client, message_id, text, [button])

    if not edited:
        logger.warning("Could not edit post %s — button not added", message_id)
        await _log(session, pair.user_id, bot_id, LogLevel.warning,
                   f"Could not add button to post {message_id}")

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


async def _try_edit(
    client: MaxClient,
    message_id: str,
    text: str,
    attachments: list[dict],
) -> bool:
    """Edit a message with given attachments. Returns True on success."""
    try:
        await client.edit_message(message_id=message_id, text=text or "", attachments=attachments)
        logger.info("Edited post %s with %d attachment(s)", message_id, len(attachments))
        return True
    except MaxAPIError as exc:
        logger.warning("Edit %s failed (status=%s): %s", message_id, exc.status, exc.body)
        return False


def _extract_media_attachments(body: dict) -> list[dict]:
    """
    Extract attachments forwardable to another chat (image/video/audio/file by token).
    Excludes 'share' — those tokens are context-specific.
    """
    attachments = []
    for att in body.get("attachments", []):
        att_type = att.get("type", "")
        payload = att.get("payload", {})
        token = payload.get("token")
        if att_type in ("image", "video", "audio", "file") and token:
            attachments.append({"type": att_type, "payload": {"token": token}})
    return attachments


def _extract_share_url(body: dict) -> str | None:
    """Return the URL from the first 'share' attachment, if present."""
    for att in body.get("attachments", []):
        if att.get("type") == "share":
            url = att.get("payload", {}).get("url")
            if url:
                return url
    return None


async def _log(
    session: AsyncSession,
    user_id: int | None,
    bot_id: int | None,
    level: LogLevel,
    message: str,
) -> None:
    session.add(EventLog(user_id=user_id, bot_id=bot_id, level=level, message=message))
