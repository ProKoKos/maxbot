"""
Event handlers for Max Bot API updates.

Flow for new channel post:
  1. Look up the ChannelGroupPair for this channel + bot.
  2. Duplicate post to the discussion group.
  3. Edit the original channel post to add the inline button.
  4. Save PostLink: channel_post_id → group_message_id.
  5. Write EventLog entry.

Flow for verification (captcha-gate):
  user_added / chat_member_added:
    1. Look up WelcomeConfig by group_id + bot_id (new); fall back to ChannelGroupPair.
    2. Skip if the joining user IS the bot itself.
    3. Generate a secret token; save VerificationRequest with deadline.
    4. Send verification message with deep-link button to group.

  bot_started (payload="verify_<token>"):
    1. Look up VerificationRequest by token.
    2. Check deadline not passed and status == pending.
    3. Mark status=verified.
    4. Delete group message to keep the chat clean.
    5. If verification_welcome_dm configured: send DM with "return to group" button.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import bot.ollama_client as ollama_client
from bot.buttons import comment_button, discussion_header
from bot.client import MaxAPIError, MaxClient
from db.models import (
    AssistantConfig,
    Bot,
    ChannelGroupPair,
    ConversationMessage,
    EventLog,
    LogLevel,
    PostLink,
    UserBotContext,
    VerificationRequest,
    VerificationStatus,
    WelcomeConfig,
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

    if chat_type == "dialog":
        await _handle_dm_message(update, session, client, bot_id)
        return

    if chat_type != "channel":
        # Non-channel message — delete it if the sender is still pending verification
        await _delete_if_unverified(update, session, client, bot_id)
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
    Fired when someone joins a group. If a WelcomeConfig (or pair verification)
    is enabled for this group, send a captcha-gate message with a deep-link button.
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
        logger.debug("user_added: missing chat_id or user_id")
        return

    # ── 1. Try WelcomeConfig first (standalone, new) ─────────────────────────
    wc: WelcomeConfig | None = None
    pair: ChannelGroupPair | None = None

    if bot_id is not None:
        wc_result = await session.execute(
            select(WelcomeConfig)
            .where(
                WelcomeConfig.group_id == chat_id,
                WelcomeConfig.bot_id == bot_id,
                WelcomeConfig.verification_enabled == True,  # noqa: E712
            )
            .options(selectinload(WelcomeConfig.bot))
        )
        wc = wc_result.scalar_one_or_none()

    # ── 2. Fall back to pair-based verification ───────────────────────────────
    if wc is None:
        pair_query = select(ChannelGroupPair).where(
            ChannelGroupPair.group_id == chat_id,
            ChannelGroupPair.enabled == True,  # noqa: E712
            ChannelGroupPair.verification_enabled == True,  # noqa: E712
        )
        if bot_id is not None:
            pair_query = pair_query.where(ChannelGroupPair.bot_id == bot_id)
        pair_result = await session.execute(
            pair_query.options(selectinload(ChannelGroupPair.bot))
        )
        pair = pair_result.scalar_one_or_none()

    config = wc or pair
    if not config:
        return

    bot: Bot | None = config.bot
    user_id = config.user_id

    # Skip if this IS the bot itself joining the chat
    if bot and str(bot.max_user_id) == max_user_id:
        logger.debug("Bot itself joined group %s — skipping verification", chat_id)
        return

    # Skip if there's already a pending verification for this user in this group
    if wc is not None:
        existing_q = select(VerificationRequest).where(
            VerificationRequest.welcome_config_id == wc.id,
            VerificationRequest.max_user_id == max_user_id,
            VerificationRequest.status == VerificationStatus.pending,
        )
    else:
        existing_q = select(VerificationRequest).where(
            VerificationRequest.pair_id == pair.id,
            VerificationRequest.max_user_id == max_user_id,
            VerificationRequest.status == VerificationStatus.pending,
        )
    if (await session.execute(existing_q)).scalar_one_or_none():
        logger.debug("Verification already pending for user %s in group %s", max_user_id, chat_id)
        return

    logger.info(
        "New member %s (%s) in group %s — verification required (%s=%d)",
        user_name, max_user_id, chat_id,
        "welcome_config" if wc else "pair",
        config.id,
    )

    # Build verification token and deadline
    token = secrets.token_hex(24)  # 48-char hex string
    deadline = datetime.now(timezone.utc) + timedelta(minutes=config.verification_timeout_min)

    # Get bot username for deep-link
    bot_username = (bot.max_username if bot else None) or ""

    # Build message text from template
    msg_template = config.verification_message or _DEFAULT_VERIFY_MSG
    msg_text = (
        msg_template
        .replace("{имя}", user_name)
        .replace("{группа}", config.group_name or chat_id)
        .replace("{минут}", str(config.verification_timeout_min))
    )

    # Build deep-link button
    btn_text = config.verification_button_text or _DEFAULT_VERIFY_BTN
    deep_link_url = f"https://max.ru/{bot_username}?start=verify_{token}" if bot_username else ""
    if not deep_link_url:
        logger.warning(
            "Bot has no username — cannot generate verification deep-link for config %d", config.id
        )
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
        await _log(session, user_id, bot_id, LogLevel.error,
                   f"Verification msg failed for {user_name} in group {chat_id}: {exc}")
        await session.commit()
        return

    # Save verification request (link to WelcomeConfig or pair)
    vr = VerificationRequest(
        welcome_config_id=wc.id if wc is not None else None,
        pair_id=pair.id if pair is not None else None,
        max_user_id=max_user_id,
        user_name=user_name,
        token=token,
        group_message_id=group_message_id,
        deadline=deadline,
        status=VerificationStatus.pending,
    )
    session.add(vr)
    await _log(
        session, user_id, bot_id, LogLevel.info,
        f"Verification started for {user_name} ({max_user_id}) in group {chat_id} "
        f"(config={config.id}), deadline {deadline.isoformat()}"
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

    # Look up request — eagerly load both config types
    result = await session.execute(
        select(VerificationRequest)
        .where(VerificationRequest.token == token)
        .options(
            selectinload(VerificationRequest.welcome_config).selectinload(WelcomeConfig.bot),
            selectinload(VerificationRequest.pair).selectinload(ChannelGroupPair.bot),
        )
    )
    vr = result.scalar_one_or_none()

    if not vr:
        logger.warning("Verification token not found: %s", token[:8])
        await _send_dm_safe(client, chat_id_for_dm,
                            "⚠️ Ссылка верификации не найдена или устарела.")
        return

    # Resolve config (WelcomeConfig takes priority, fall back to pair)
    if vr.welcome_config_id and vr.welcome_config:
        config = vr.welcome_config
        user_id = config.user_id
    elif vr.pair_id and vr.pair:
        config = vr.pair
        user_id = config.user_id
    else:
        await _send_dm_safe(client, chat_id_for_dm,
                            "⚠️ Конфигурация верификации удалена. Обратитесь к администратору.")
        return

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
        await _log(session, user_id, bot_id, LogLevel.warning,
                   f"Verification expired for {vr.user_name} ({vr.max_user_id})")
        await session.commit()
        await _send_dm_safe(
            client, chat_id_for_dm,
            f"⏰ К сожалению, время на верификацию истекло. "
            f"Обратитесь к администраторам группы *{config.group_name}*."
        )
        return

    # ✅ Mark as verified
    vr.status = VerificationStatus.verified
    await _log(
        session, user_id, bot_id, LogLevel.info,
        f"User {vr.user_name} ({vr.max_user_id}) verified for group {config.group_id} "
        f"(config {config.id})"
    )
    await _save_user_bot_context(session, bot_id, vr.max_user_id, config)
    await session.commit()

    # Delete group verification message to keep the chat clean
    if vr.group_message_id:
        try:
            await client.delete_message(message_id=vr.group_message_id)
        except MaxAPIError as exc:
            logger.warning(
                "Could not delete group verification message %s: %s", vr.group_message_id, exc
            )

    # Send welcome DM in bot chat
    if config.verification_welcome_dm:
        welcome_text = (
            config.verification_welcome_dm
            .replace("{имя}", vr.user_name)
            .replace("{группа}", config.group_name or config.group_id)
        )
    else:
        welcome_text = (
            f"✅ Верификация пройдена! Добро пожаловать в *{config.group_name or 'группу'}*."
        )

    # Attach "Return to group" button if the group has a public link
    return_button = None
    if config.group_link:
        return_button = {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [[{
                    "type": "link",
                    "text": "💬 Вернуться в группу",
                    "url": config.group_link,
                }]]
            },
        }

    await _send_dm_safe(client, chat_id_for_dm, welcome_text,
                        attachments=[return_button] if return_button else None)


# ── AI assistant DM handler ───────────────────────────────────────────────────

async def _handle_dm_message(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """Handle an incoming DM and reply using the AI assistant if configured."""
    message = update.get("message", {})
    sender = message.get("sender", {})
    max_user_id = str(sender.get("user_id", ""))
    chat = message.get("recipient", {})
    chat_id = str(chat.get("chat_id", "") or max_user_id)
    message_body = message.get("body", {})
    text = (message_body.get("text") or "").strip()

    if not max_user_id or not text or bot_id is None:
        return

    contexts_result = await session.execute(
        select(UserBotContext)
        .where(UserBotContext.bot_id == bot_id, UserBotContext.max_user_id == max_user_id)
        .options(selectinload(UserBotContext.assistant_config))
    )
    contexts = contexts_result.scalars().all()
    if not contexts:
        return

    assistant_config: AssistantConfig | None = None
    for ctx in contexts:
        if ctx.assistant_config and ctx.assistant_config.is_enabled:
            assistant_config = ctx.assistant_config
            break
    if not assistant_config:
        return

    history_result = await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.max_user_id == max_user_id,
        )
        .order_by(ConversationMessage.created_at)
    )
    history = history_result.scalars().all()

    messages: list[dict] = []
    if assistant_config.system_prompt:
        messages.append({"role": "system", "content": assistant_config.system_prompt})
    for msg in history:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": text})

    session.add(ConversationMessage(
        bot_id=bot_id,
        max_user_id=max_user_id,
        assistant_config_id=assistant_config.id,
        role="user",
        content=text,
    ))
    await session.commit()

    try:
        reply = await ollama_client.chat(
            model=assistant_config.model_name,
            messages=messages,
        )
    except Exception as exc:
        logger.error("Ollama error for user %s: %s", max_user_id, exc)
        await _send_dm_safe(client, chat_id, "⚠️ Ошибка AI-ассистента. Попробуйте позже.")
        return

    session.add(ConversationMessage(
        bot_id=bot_id,
        max_user_id=max_user_id,
        assistant_config_id=assistant_config.id,
        role="assistant",
        content=reply,
    ))
    await session.commit()
    await _send_dm_safe(client, chat_id, reply)


async def _save_user_bot_context(
    session: AsyncSession,
    bot_id: int | None,
    max_user_id: str,
    config: WelcomeConfig | ChannelGroupPair,
) -> None:
    """Create a UserBotContext linking this user to the AssistantConfig for the verified group."""
    if bot_id is None:
        return

    result = await session.execute(
        select(AssistantConfig).where(
            AssistantConfig.bot_id == bot_id,
            AssistantConfig.group_id == config.group_id,
        )
    )
    assistant_config = result.scalar_one_or_none()
    if not assistant_config:
        return

    existing = await session.execute(
        select(UserBotContext).where(
            UserBotContext.bot_id == bot_id,
            UserBotContext.max_user_id == max_user_id,
            UserBotContext.assistant_config_id == assistant_config.id,
        )
    )
    if existing.scalar_one_or_none():
        return

    session.add(UserBotContext(
        bot_id=bot_id,
        max_user_id=max_user_id,
        assistant_config_id=assistant_config.id,
    ))


# ── Delete messages from unverified members ───────────────────────────────────

async def _delete_if_unverified(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """
    If the message author has a pending VerificationRequest in this group,
    silently delete the message to enforce read-only until verification passes.
    """
    message = update.get("message", {})
    chat = message.get("recipient", {})
    chat_id = str(chat.get("chat_id", ""))
    sender = message.get("sender", {})
    sender_id = str(sender.get("user_id", ""))
    message_body = message.get("body", {})
    message_id = str(message_body.get("mid", ""))

    if not chat_id or not sender_id or not message_id:
        return

    # Build subqueries filtered by group_id (and optionally bot_id)
    wc_q = select(WelcomeConfig.id).where(WelcomeConfig.group_id == chat_id)
    pair_q = select(ChannelGroupPair.id).where(ChannelGroupPair.group_id == chat_id)
    if bot_id is not None:
        wc_q = wc_q.where(WelcomeConfig.bot_id == bot_id)
        pair_q = pair_q.where(ChannelGroupPair.bot_id == bot_id)

    result = await session.execute(
        select(VerificationRequest).where(
            VerificationRequest.max_user_id == sender_id,
            VerificationRequest.status == VerificationStatus.pending,
            or_(
                VerificationRequest.welcome_config_id.in_(wc_q),
                VerificationRequest.pair_id.in_(pair_q),
            ),
        )
    )
    vr = result.scalar_one_or_none()
    if not vr:
        return

    try:
        await client.delete_message(message_id=message_id)
        logger.info(
            "Deleted message %s from unverified user %s in group %s",
            message_id, sender_id, chat_id,
        )
    except MaxAPIError as exc:
        logger.warning("Could not delete unverified message %s: %s", message_id, exc)


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _send_dm_safe(
    client: MaxClient,
    chat_id: str,
    text: str,
    attachments: list[dict] | None = None,
) -> None:
    """Send a DM; swallow errors (user may have blocked the bot)."""
    if not chat_id:
        return
    try:
        await client.send_message(chat_id=chat_id, text=text, attachments=attachments)
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
