"""
Event handlers for Max Bot API updates.

Flow for new channel post:
  1. Look up the ChannelGroupPair for this channel + bot.
  2. Duplicate post to the discussion group.
  3. Edit the original channel post to add the inline button.
  4. Save PostLink: channel_post_id → group_message_id.
  5. Write EventLog entry.

Flow for verification (captcha-gate):
  chat_member_added:
    1. Look up pair by group_id; skip if verification not enabled.
    2. Skip if the joining user IS the bot itself.
    3. Generate a secret token; save VerificationRequest with deadline.
    4. Send verification message with deep-link button to group.

  bot_started (payload="verify_<token>"):
    1. Look up VerificationRequest by token.
    2. Check deadline not passed and status == pending.
    3. Mark status=verified.
    4. Edit group message to show success.
    5. If verification_welcome_dm configured: send DM.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.buttons import comment_button, discussion_header
from bot.client import MaxAPIError, MaxClient
from db.models import (
    Bot,
    ChannelGroupPair,
    EventLog,
    LogLevel,
    PostLink,
    VerificationRequest,
    VerificationStatus,
)

logger = logging.getLogger(__name__)

# ── Default verification message template ─────────────────────────────────────

_DEFAULT_VERIFY_MSG = (
    "👋 Привет, **{имя}**!\n\n"
    "Добро пожаловать в *{группа}*. Чтобы получить доступ к чату, подтвердите, "
    "что вы не бот — нажмите кнопку ниже.\n\n"
    "⏰ Время на верификацию: **{минут} мин.**"
)
_DEFAULT_VERIFY_BTN = "✅ Я не бот"

_DEFAULT_KICK_MSG = (
    "❌ **{имя}** не прошёл(а) проверку и был(а) исключён(а) из группы."
)
_DEFAULT_SUCCESS_MSG = (
    "✅ **{имя}** прошёл(а) проверку и получил(а) доступ к чату."
)


# ── Main dispatcher ────────────────────────────────────────────────────────────

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
        await _handle_bot_started(update, session, client, bot_id=bot_id)
    elif update_type in ("user_added", "chat_member_added"):
        await _handle_member_added(update, session, client, bot_id=bot_id)
    else:
        logger.debug("Unhandled update type: %r", update_type)


# ── Channel post duplication ───────────────────────────────────────────────────

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

    # Step 1: Duplicate post to discussion group
    header = discussion_header(pair.channel_name or chat_id, message_id, pair.channel_link or "")
    group_text = header + (text or "")
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

    group_message_id = str(group_resp.get("message", {}).get("body", {}).get("mid", ""))

    # Step 2: Edit original channel post to add discussion button
    button = comment_button(pair.group_link, group_message_id)
    share_url = _extract_share_url(message_body)

    edited = False
    if share_url:
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

    # Step 3: Persist link
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


# ── Verification: new member joined ───────────────────────────────────────────

async def _handle_member_added(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """
    Fired when someone joins a group. If verification is enabled for this group,
    send a captcha-gate message with a deep-link button.
    """
    # Extract chat_id — Max may use "chat_id" at top level or inside "chat" dict
    chat_id = str(
        update.get("chat_id")
        or update.get("chat", {}).get("chat_id", "")
    )
    user_info = update.get("user", {})
    max_user_id = str(user_info.get("user_id", ""))
    user_name = user_info.get("name") or user_info.get("username") or "Участник"

    if not chat_id or not max_user_id:
        logger.debug("chat_member_added: missing chat_id or user_id")
        return

    # Look up the pair for this group
    query = select(ChannelGroupPair).where(
        ChannelGroupPair.group_id == chat_id,
        ChannelGroupPair.enabled == True,  # noqa: E712
        ChannelGroupPair.verification_enabled == True,  # noqa: E712
    )
    if bot_id is not None:
        query = query.where(ChannelGroupPair.bot_id == bot_id)

    result = await session.execute(query.options(selectinload(ChannelGroupPair.bot)))
    pair = result.scalar_one_or_none()
    if not pair:
        return

    # Skip if this IS the bot itself joining the chat
    bot: Bot | None = pair.bot
    if bot and str(bot.max_user_id) == max_user_id:
        logger.debug("Bot itself joined group %s — skipping verification", chat_id)
        return

    # Skip if there's already a pending verification for this user in this group
    existing = await session.execute(
        select(VerificationRequest).where(
            VerificationRequest.pair_id == pair.id,
            VerificationRequest.max_user_id == max_user_id,
            VerificationRequest.status == VerificationStatus.pending,
        )
    )
    if existing.scalar_one_or_none():
        logger.debug("Verification already pending for user %s in group %s", max_user_id, chat_id)
        return

    logger.info(
        "New member %s (%s) in group %s — verification required (pair=%d)",
        user_name, max_user_id, chat_id, pair.id,
    )

    # Build verification token and deadline
    token = secrets.token_hex(24)  # 48-char hex string
    deadline = datetime.now(timezone.utc) + timedelta(minutes=pair.verification_timeout_min)

    # Get bot username for deep-link
    bot_username = (bot.max_username if bot else None) or ""

    # Build message text from template
    msg_template = pair.verification_message or _DEFAULT_VERIFY_MSG
    msg_text = (
        msg_template
        .replace("{имя}", user_name)
        .replace("{группа}", pair.group_name or chat_id)
        .replace("{минут}", str(pair.verification_timeout_min))
    )

    # Build deep-link button
    btn_text = pair.verification_button_text or _DEFAULT_VERIFY_BTN
    deep_link_url = f"https://max.ru/{bot_username}?start=verify_{token}" if bot_username else ""
    if not deep_link_url:
        logger.warning("Bot has no username — cannot generate verification deep-link for pair %d", pair.id)
        return

    verify_button = {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [[{
                "type": "link",
                "text": btn_text,
                "url": deep_link_url,
            }]]
        },
    }

    # Send verification message to group
    group_message_id: str | None = None
    try:
        resp = await client.send_message(
            chat_id=chat_id,
            text=msg_text,
            attachments=[verify_button],
        )
        group_message_id = (
            str(resp.get("message", {}).get("body", {}).get("mid", "")) or None
        )
    except MaxAPIError as exc:
        logger.error("Failed to send verification message to group %s: %s", chat_id, exc)
        await _log(session, pair.user_id, bot_id, LogLevel.error,
                   f"Verification msg failed for {user_name} in group {chat_id}: {exc}")
        await session.commit()
        return

    # Save verification request
    vr = VerificationRequest(
        pair_id=pair.id,
        max_user_id=max_user_id,
        user_name=user_name,
        token=token,
        group_message_id=group_message_id,
        deadline=deadline,
        status=VerificationStatus.pending,
    )
    session.add(vr)
    await _log(
        session, pair.user_id, bot_id, LogLevel.info,
        f"Verification started for {user_name} ({max_user_id}) in group {chat_id} "
        f"(pair {pair.id}), deadline {deadline.isoformat()}"
    )
    await session.commit()


# ── Verification: user clicked /start in bot ──────────────────────────────────

async def _handle_bot_started(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """
    Fired when a user opens the bot / uses a deep-link.
    If payload starts with "verify_", process the captcha-gate response.
    """
    user_info = update.get("user", {})
    max_user_id = str(user_info.get("user_id", ""))
    user_name = user_info.get("name") or user_info.get("username") or "Участник"

    # chat_id for sending DM reply: in Max, the user's private chat_id
    # can be either update.chat_id or the user_id itself
    chat_id_for_dm = str(
        update.get("chat_id") or max_user_id
    )

    # Payload comes from ?start=PAYLOAD in the deep-link
    payload = str(update.get("payload") or update.get("start_payload") or "").strip()

    if not payload.startswith("verify_"):
        logger.info("bot_started from user %s (no verification payload)", max_user_id)
        return

    token = payload[len("verify_"):]
    if not token:
        return

    logger.info("Verification response: user %s, token %s", max_user_id, token[:8] + "...")

    # Look up request
    result = await session.execute(
        select(VerificationRequest)
        .where(VerificationRequest.token == token)
        .options(selectinload(VerificationRequest.pair).selectinload(ChannelGroupPair.bot))
    )
    vr = result.scalar_one_or_none()

    if not vr:
        logger.warning("Verification token not found: %s", token[:8])
        await _send_dm_safe(client, chat_id_for_dm,
                            "⚠️ Ссылка верификации не найдена или устарела.")
        return

    pair = vr.pair
    now = datetime.now(timezone.utc)

    # Check already processed
    if vr.status != VerificationStatus.pending:
        msg = {
            VerificationStatus.verified: "✅ Вы уже прошли верификацию!",
            VerificationStatus.kicked: "❌ Вы были исключены из группы за истечение времени.",
            VerificationStatus.expired: "⏰ Время верификации истекло.",
        }.get(vr.status, "⚠️ Запрос верификации уже обработан.")
        await _send_dm_safe(client, chat_id_for_dm, msg)
        return

    # Check deadline
    if now > vr.deadline:
        vr.status = VerificationStatus.expired
        await _log(session, pair.user_id, bot_id, LogLevel.warning,
                   f"Verification expired for {vr.user_name} ({vr.max_user_id})")
        await session.commit()
        await _send_dm_safe(
            client, chat_id_for_dm,
            f"⏰ К сожалению, время на верификацию истекло. "
            f"Обратитесь к администраторам группы *{pair.group_name}*."
        )
        return

    # ✅ Mark as verified
    vr.status = VerificationStatus.verified
    await _log(
        session, pair.user_id, bot_id, LogLevel.info,
        f"User {vr.user_name} ({vr.max_user_id}) verified for group {pair.group_id} (pair {pair.id})"
    )
    await session.commit()

    # Delete group verification message to keep the chat clean
    if vr.group_message_id:
        try:
            await client.delete_message(message_id=vr.group_message_id)
        except MaxAPIError as exc:
            logger.warning("Could not delete group verification message %s: %s", vr.group_message_id, exc)

    # Send welcome DM in bot chat
    if pair.verification_welcome_dm:
        welcome_text = (
            pair.verification_welcome_dm
            .replace("{имя}", vr.user_name)
            .replace("{группа}", pair.group_name or pair.group_id)
        )
        await _send_dm_safe(client, chat_id_for_dm, welcome_text)
    else:
        # Always confirm success in DM
        group_link_part = (
            f" Вернитесь в группу: {pair.group_link}" if pair.group_link else ""
        )
        await _send_dm_safe(
            client, chat_id_for_dm,
            f"✅ Верификация пройдена! Добро пожаловать в *{pair.group_name or 'группу'}*.{group_link_part}"
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _send_dm_safe(client: MaxClient, chat_id: str, text: str) -> None:
    """Send a DM; swallow errors (user may have blocked the bot)."""
    if not chat_id:
        return
    try:
        await client.send_message(chat_id=chat_id, text=text)
    except MaxAPIError as exc:
        logger.warning("Could not send DM to %s: %s", chat_id, exc)


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
