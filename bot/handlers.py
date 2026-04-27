"""
Обработчики событий MAX Bot API.

Каждое событие из long-polling/webhook попадает сюда через диспетчер
:func:`handle_update`, который по полю ``update_type`` вызывает соответствующий
``_handle_*``. Вся работа выполняется в рамках одной AsyncSession,
которая открывается на пакет апдейтов в supervisor'е.

──────────────────────────────────────────────────────────────────────────────
ПОТОК «новый пост в канале» (``message_created`` в чате типа ``channel``):
  1. Найти активную ChannelGroupPair (channel_id + этот же bot_id).
  2. Проверить дубликат по PostLink.channel_post_id → не повторяем.
  3. Отправить пост-копию в группу (текст + media-вложения).
  4. Отредактировать оригинал в канале — приклеить inline-кнопку «💬 Прокомментировать».
     Если в посте было ``share``-вложение, пробуем сохранить его при edit.
  5. Сохранить связь PostLink (channel_post_id → group_message_id) и EventLog.

ПОТОК «верификация» (captcha-gate):
  ``user_added`` / ``chat_member_added`` — пользователь вошёл в группу:
    1. Сначала пробуем WelcomeConfig (новый, standalone), потом fallback
       на ChannelGroupPair. Приоритет описан в CLAUDE.md.
    2. Пропускаем, если в группу зашёл сам бот (max_user_id совпадает).
    3. Пропускаем, если для этого пользователя уже есть pending-запрос.
    4. Генерируем secret-токен и deadline = now + verification_timeout_min.
    5. Отправляем в группу сообщение с deep-link кнопкой
       ``https://max.ru/<bot>?start=verify_<token>``.
    6. Сохраняем VerificationRequest со ссылкой на конфиг (welcome_config_id
       либо pair_id — ровно одно из двух).

  ``bot_started`` (payload = ``verify_<token>``):
    1. Находим VerificationRequest по token.
    2. Если deadline истёк — помечаем expired, шлём пользователю DM.
    3. Иначе ставим status=verified, удаляем сообщение из группы (чтобы
       не засорять чат), при наличии конфига AssistantConfig — связываем
       пользователя через UserBotContext (для AI-ассистента).
    4. Шлём welcome DM с кнопкой возврата в группу (если задана).

ПОТОК «сообщение в группе от непроверенного пользователя»:
  MAX API не поддерживает mute/restrict — поэтому handler ловит каждое
  сообщение в группе и удаляет его, пока есть открытый VerificationRequest
  (см. :func:`_delete_if_unverified`). Ограничение описано в CLAUDE.md.

ПОТОК «личный диалог с ботом» (chat_type == ``dialog``):
  Если для бота настроен AssistantConfig и пользователь связан с ним
  через UserBotContext, отправляем запрос на OpenAI-совместимое API
  (через :mod:`bot.ollama_client`) и отвечаем в DM. История переписки
  сохраняется в ConversationMessage и используется как контекст.
"""
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import bot.ollama_client as ollama_client
from bot.buttons import comment_button, discussion_header
from bot.client import MaxAPIError, MaxClient
from bot.constants import (
    DEFAULT_VERIFY_BTN,
    DEFAULT_VERIFY_MSG,
    RETURN_TO_GROUP_LABEL,
    VERIFY_PAYLOAD_PREFIX,
)
from bot.crypto import decrypt_token
from db.models import (
    AssistantConfig,
    Bot,
    ChannelGroupPair,
    ConversationMessage,
    EventLog,
    LogLevel,
    PostLink,
    UserBotContext,
    UserChannelMembership,
    UserProfile,
    VerificationRequest,
    VerificationStatus,
    WelcomeConfig,
)

logger = logging.getLogger(__name__)


# ── Главный диспетчер ─────────────────────────────────────────────────────────

async def handle_update(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """Раскидывает один MAX-апдейт по нужному ``_handle_*``.

    ``bot_id`` передаётся явно из supervisor'а — это важно для multi-bot
    режима: один и тот же канал/группа может быть подключён к нескольким
    ботам, и handler должен видеть только «свои» сущности (см. фильтры по
    ``bot_id`` во всех запросах).

    MAX в разных версиях API использует ``update_type`` либо ``type`` —
    поддерживаем оба варианта, чтобы не зависеть от формата.
    """
    update_type = update.get("update_type") or update.get("type", "")

    if update_type == "message_created":
        await _handle_message_created(update, session, client, bot_id=bot_id)
    elif update_type == "bot_started":
        await _handle_bot_started(update, session, client, bot_id=bot_id)
    elif update_type in ("user_added", "chat_member_added"):
        # MAX API использует обе вариации в зависимости от способа вступления.
        await _handle_member_added(update, session, client, bot_id=bot_id)
    elif update_type in ("user_removed", "chat_member_removed"):
        await _handle_member_removed(update, session, client, bot_id=bot_id)
    else:
        logger.debug("Unhandled update type: %r", update_type)


# ── Дублирование постов из канала в группу обсуждений ────────────────────────

async def _handle_message_created(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """Маршрутизирует ``message_created`` по типу чата.

    Один handler обслуживает три сценария — дублирование канала, DM
    с AI-ассистентом и удаление сообщений у непроверенных пользователей.
    Решение, что делать, принимается по ``chat_type``.
    """
    message = update.get("message", {})
    chat = message.get("recipient", {})
    chat_type = chat.get("chat_type", "")

    if chat_type == "dialog":
        # Личный диалог с ботом — отдаём AI-ассистенту.
        await _handle_dm_message(update, session, client, bot_id)
        return

    if chat_type != "channel":
        # Обычная группа: если автор не прошёл верификацию — удаляем сообщение.
        # Это замена mute/restrict, которых нет в MAX API (см. CLAUDE.md).
        await _delete_if_unverified(update, session, client, bot_id)
        return

    chat_id = str(chat.get("chat_id", ""))
    message_body = message.get("body", {})
    message_id = str(message_body.get("mid", ""))
    text = message_body.get("text", "")

    if not chat_id or not message_id:
        return

    # Ищем активную пару для канала, обслуживаемую именно этим ботом.
    # Фильтр по bot_id обязателен в multi-bot режиме: один и тот же канал
    # может быть в нескольких парах (но обычно у разных пользователей).
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

    # Защита от двойной обработки: при долгом polling MAX иногда
    # переотправляет апдейты. PostLink — единственный источник истины
    # о том, что пост уже продублирован.
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

    # Шаг 1. Публикуем пост в группе обсуждений.
    # Заголовок добавляем всегда, даже без channel_link — иначе участники
    # группы не поймут, откуда взялся пост.
    header = discussion_header(pair.channel_name or chat_id, message_id, pair.channel_link or "")
    group_text = header + (text or "")
    # Из вложений берём только media (image/video/audio/file) — share-токены
    # привязаны к контексту канала и не работают в чужом чате.
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

    # Шаг 2. Редактируем оригинал в канале — добавляем кнопку
    # «💬 Прокомментировать», которая ведёт в группу.
    button = comment_button(pair.group_link, group_message_id)
    share_url = _extract_share_url(message_body)

    # Если в посте была share-ссылка (репост превью), сначала пробуем
    # сохранить её при edit. MAX иногда отвергает такой апдейт — fallback
    # без share, чтобы кнопка всё равно появилась.
    edited = False
    if share_url:
        share_att = {"type": "share", "payload": {"url": share_url}}
        edited = await _try_edit(client, message_id, text, [share_att, button])
        if not edited:
            logger.info("Edit with share_url failed for %s, retrying with button only", message_id)

    if not edited:
        edited = await _try_edit(client, message_id, text, [button])

    if not edited:
        # Жёсткой ошибки нет — пост в группе уже опубликован, просто без
        # обратной кнопки. Логируем как warning, чтобы пользователь видел.
        logger.warning("Could not edit post %s — button not added", message_id)
        await _log(session, pair.user_id, bot_id, LogLevel.warning,
                   f"Could not add button to post {message_id}")

    # Шаг 3. Сохраняем связь channel_post_id → group_message_id.
    # Используется для дедупликации (см. выше) и потенциально — для
    # прокидывания комментариев из группы в канал.
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


# ── Верификация: новый участник вошёл в группу ───────────────────────────────

async def _handle_member_added(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """Реагирует на вход нового пользователя в группу.

    Если для группы есть WelcomeConfig или пара с включённой верификацией,
    отправляет в чат сообщение с deep-link кнопкой и создаёт
    VerificationRequest. Иначе — тихо игнорирует событие (бот в группе
    может быть нужен только для другого функционала).
    """
    # MAX в разных версиях кладёт chat_id либо на верхний уровень, либо
    # внутрь объекта "chat". Поддерживаем оба варианта.
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

    # Флаг is_channel: одно событие user_added используется для групп и каналов.
    # Если пользователь подписался на канал — записываем членство и выходим:
    # верификация для каналов не нужна.
    if update.get("is_channel"):
        await _save_channel_membership(session, bot_id, max_user_id, chat_id)
        return

    # 1. Пробуем найти standalone WelcomeConfig — он имеет приоритет над
    # парой по правилу из CLAUDE.md (WelcomeConfig > ChannelGroupPair).
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

    # 2. Standalone-конфига нет — fallback на пару канал↔группа
    # с включённой галочкой verification_enabled.
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

    # Защита от самого себя: при добавлении бота в группу MAX тоже шлёт
    # ``user_added`` — не пытаемся верифицировать собственного бота.
    if bot and str(bot.max_user_id) == max_user_id:
        logger.debug("Bot itself joined group %s — skipping verification", chat_id)
        return

    # Защита от двойной отправки приветствия — MAX иногда дублирует
    # apdate, особенно при сетевых сбоях. Если pending-запрос уже есть,
    # повторное сообщение бы запутало пользователя.
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

    # Генерируем secret-токен (192 бита энтропии) — он попадёт в URL и
    # должен быть непредсказуемым, чтобы исключить подбор по чужим ссылкам.
    token = secrets.token_hex(24)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=config.verification_timeout_min)

    # Имя бота нужно для deep-link. Кешируется в Bot.max_username при
    # первой авторизации в supervisor._update_bot_identity.
    bot_username = (bot.max_username if bot else None) or ""

    # Подставляем плейсхолдеры в шаблон (замены безопасны, т.к. это
    # отображаемый текст, а не SQL/HTML — подстановка идёт в payload MAX).
    msg_template = config.verification_message or DEFAULT_VERIFY_MSG
    msg_text = (
        msg_template
        .replace("{имя}", user_name)
        .replace("{группа}", config.group_name or chat_id)
        .replace("{минут}", str(config.verification_timeout_min))
    )

    # Без max_username бот не сможет дать рабочую deep-ссылку — не имеет
    # смысла отправлять кнопку, ведущую в никуда. Логируем и выходим;
    # пользователь сможет докатить max_username руками в настройках бота.
    btn_text = config.verification_button_text or DEFAULT_VERIFY_BTN
    deep_link_url = (
        f"https://max.ru/{bot_username}?start={VERIFY_PAYLOAD_PREFIX}{token}"
        if bot_username else ""
    )
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

    # Сохраняем запрос. Заполняется ровно один из FK
    # (welcome_config_id или pair_id) — наша модель допускает оба, но
    # верификация всегда привязана к конкретному источнику конфига.
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


# ── Верификация: пользователь нажал кнопку и пришёл в бота ───────────────────

async def _handle_bot_started(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """Обрабатывает первый старт диалога с ботом / deep-link.

    Если payload — наш ``verify_<token>``, проводим верификацию.
    Иначе просто игнорируем (это может быть обычный /start от человека,
    который захотел познакомиться с ботом).

    ВНИМАНИЕ: race condition — если пользователь два раза быстро нажмёт
    кнопку, оба обработчика прочитают status=pending до коммита первого.
    Сейчас второй просто увидит «Вы уже прошли верификацию», что ок;
    но при необходимости здесь можно добавить SELECT … FOR UPDATE.
    """
    user_info = update.get("user", {})
    max_user_id = str(user_info.get("user_id", ""))
    user_name = user_info.get("name") or user_info.get("username") or "Участник"

    # Для отправки DM нужен chat_id личного диалога. В MAX он совпадает
    # с user_id, если apdate пришёл без явного chat_id (что бывает
    # на старте диалога).
    chat_id_for_dm = str(
        update.get("chat_id") or max_user_id
    )

    # Payload — то, что было в ?start=…, MAX называет это поле по-разному
    # ("payload" / "start_payload"). Проверяем оба.
    payload = str(update.get("payload") or update.get("start_payload") or "").strip()

    if not payload.startswith(VERIFY_PAYLOAD_PREFIX):
        logger.info("bot_started from user %s (no verification payload)", max_user_id)
        return

    token = payload[len(VERIFY_PAYLOAD_PREFIX):]
    if not token:
        return

    logger.info("Verification response: user %s, token %s", max_user_id, token[:8] + "...")

    # Подгружаем сразу обе ветки (welcome_config и pair) — заранее,
    # потому что какая именно живёт, мы не знаем; selectinload экономит
    # один-два дополнительных round-trip.
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

    # Резолвим конфиг по тому же приоритету, что и в _handle_member_added:
    # welcome_config (новый стиль) → pair (legacy). Если оба удалены
    # пользователем уже после старта верификации — мягко сообщаем юзеру.
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

    # Если статус уже не pending — кто-то опередил (либо сам пользователь
    # дважды нажал кнопку, либо scheduler успел кикнуть). Просто отвечаем
    # понятным сообщением — без лишних действий.
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

    # Удаляем сообщение из группы, чтобы не засорять чат после верификации.
    # Если удалить не удалось (например, бот разжалован из админов) —
    # это не блокирует основной поток, просто warning.
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

    # Кнопка возврата работает только при наличии публичной ссылки на
    # группу. Без неё MAX не открыл бы группу извне, поэтому не кладём
    # пустую кнопку — лучше отправить просто текст.
    return_button = None
    if config.group_link:
        return_button = {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [[{
                    "type": "link",
                    "text": RETURN_TO_GROUP_LABEL,
                    "url": config.group_link,
                }]]
            },
        }

    await _send_dm_safe(client, chat_id_for_dm, welcome_text,
                        attachments=[return_button] if return_button else None)


# ── Личный диалог: AI-ассистент ──────────────────────────────────────────────

async def _handle_dm_message(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """Обрабатывает входящее DM и при необходимости отвечает AI-моделью.

    Алгоритм:
      1. Находим UserBotContext: пользователь должен быть «связан» с
         AssistantConfig (связь создаётся при успешной верификации).
      2. Пишем входящее сообщение в ConversationMessage до запроса в AI —
         чтобы оно сохранилось, даже если AI упадёт.
      3. Собираем сообщения = system_prompt + история + новое user-сообщение.
      4. Зовём ollama_client.chat(); ответ пишем в БД и шлём в DM.

    Если у бота нет AssistantConfig или модель/ключ не настроены — handler
    тихо не отвечает, чтобы бот не казался «сломанным» там, где AI вообще
    не нужен.
    """
    message = update.get("message", {})
    sender = message.get("sender", {})
    max_user_id = str(sender.get("user_id", ""))
    _photo = sender.get("photo")
    user_avatar = (
        sender.get("avatar_url")
        or sender.get("photo_url")
        or (_photo.get("url") if isinstance(_photo, dict) else _photo)
        or None
    )
    chat = message.get("recipient", {})
    chat_id = str(chat.get("chat_id", "") or max_user_id)
    message_body = message.get("body", {})
    text = (message_body.get("text") or "").strip()
    attachments = _extract_dm_attachments(message_body)

    # Обновляем профиль пользователя при каждом входящем сообщении —
    # так данные всегда актуальны (имя, фамилия, @username, биография, аватар).
    if max_user_id and bot_id is not None:
        await _upsert_user_profile(session, bot_id, max_user_id, sender, client)

    # Пропускаем только если нет ни текста, ни вложений (пустой апдейт)
    if not max_user_id or (not text and not attachments) or bot_id is None:
        return

    contexts_result = await session.execute(
        select(UserBotContext)
        .where(UserBotContext.bot_id == bot_id, UserBotContext.max_user_id == max_user_id)
        .options(selectinload(UserBotContext.assistant_config))
    )
    contexts = contexts_result.scalars().all()
    if not contexts:
        return

    # Пользователь может быть верифицирован в нескольких группах одного
    # бота — берём первый включённый AssistantConfig. Если конфиг был
    # удалён (FK установился в NULL по ondelete=SET NULL), пробуем
    # перепривязать к новому конфигу с тем же group_id — это позволяет
    # пересоздавать ассистента без ручного «обновления» подписки юзера.
    assistant_config: AssistantConfig | None = None
    for ctx in contexts:
        if ctx.assistant_config and ctx.assistant_config.is_enabled:
            assistant_config = ctx.assistant_config
            break
        elif ctx.assistant_config_id is None and ctx.group_id:
            relink = await session.execute(
                select(AssistantConfig).where(
                    AssistantConfig.bot_id == bot_id,
                    AssistantConfig.group_id == ctx.group_id,
                    AssistantConfig.is_enabled.is_(True),
                )
            )
            cfg = relink.scalar_one_or_none()
            if cfg:
                ctx.assistant_config_id = cfg.id
                await session.commit()
                assistant_config = cfg
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

    # Формат OpenAI Chat Completions: system → история → новый user-msg.
    messages: list[dict] = []
    if assistant_config.system_prompt:
        messages.append({"role": "system", "content": assistant_config.system_prompt})
    for msg in history:
        messages.append({"role": msg.role, "content": msg.content})
    if text:
        messages.append({"role": "user", "content": text})

    # Сохраняем входящее сразу, до сетевого запроса в AI: даже если
    # модель упадёт, переписка не «потеряется» и владелец увидит её
    # в инбоксе. Вложения кладём в attachments_json.
    session.add(ConversationMessage(
        bot_id=bot_id,
        max_user_id=max_user_id,
        chat_id=chat_id,
        user_avatar=user_avatar,
        assistant_config_id=assistant_config.id,
        role="user",
        content=text,
        attachments_json=json.dumps(attachments, ensure_ascii=False),
    ))
    await session.commit()

    # Если нет текста — нечего отправлять модели (вложения пока не передаём в AI)
    if not text:
        return

    if not assistant_config.model_name or not assistant_config.api_key:
        return

    try:
        # API-ключ хранится зашифрованным Fernet'ом, как и токены ботов
        # (см. CLAUDE.md). Если ключ битый — пробуем без него; дальше
        # ollama_client.chat() сам выдаст понятную ошибку.
        plain_key = ""
        if assistant_config.api_key:
            try:
                plain_key = decrypt_token(assistant_config.api_key)
            except ValueError:
                pass
        reply = await ollama_client.chat(
            model=assistant_config.model_name,
            messages=messages,
            api_url=assistant_config.api_url or "",
            api_key=plain_key,
        )
    except Exception as exc:
        logger.error("Ollama error for user %s: %s", max_user_id, exc)
        await _send_dm_safe(client, chat_id, "⚠️ Ошибка AI-ассистента. Попробуйте позже.")
        return

    session.add(ConversationMessage(
        bot_id=bot_id,
        max_user_id=max_user_id,
        chat_id=chat_id,
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
    """Связывает прошедшего верификацию пользователя с AssistantConfig.

    UserBotContext — это «членство» юзера в AI-комнате. Создаётся только
    если для (bot_id, group_id) есть AssistantConfig — иначе тихо
    выходим, чтобы не плодить пустые записи.

    Если запись уже была (повторная верификация), просто обновляем
    привязку — не вставляем дубликат, чтобы не нарушить
    UNIQUE(bot_id, max_user_id, group_id).
    """
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
            UserBotContext.group_id == config.group_id,
        )
    )
    ctx = existing.scalar_one_or_none()
    if ctx:
        ctx.assistant_config_id = assistant_config.id
        return

    session.add(UserBotContext(
        bot_id=bot_id,
        max_user_id=max_user_id,
        group_id=config.group_id,
        assistant_config_id=assistant_config.id,
    ))


# ── Членство в каналах ───────────────────────────────────────────────────────

async def _save_channel_membership(
    session: AsyncSession,
    bot_id: int | None,
    max_user_id: str,
    channel_id: str,
) -> None:
    """Сохраняет факт подписки пользователя на канал.

    Название канала берём из ChannelGroupPair — если для этого канала
    настроена пара, имя уже закешировано. Иначе оставляем NULL
    (UI покажет channel_id как fallback).

    Upsert по UNIQUE(bot_id, max_user_id, channel_id): повторный вход
    или дублированный apdate просто обновляет joined_at.
    """
    if bot_id is None or not max_user_id or not channel_id:
        return

    # Пробуем найти название канала в уже известных парах.
    pair_result = await session.execute(
        select(ChannelGroupPair.channel_name).where(
            ChannelGroupPair.bot_id == bot_id,
            ChannelGroupPair.channel_id == channel_id,
        ).limit(1)
    )
    channel_title: str | None = pair_result.scalar_one_or_none() or None

    existing = await session.execute(
        select(UserChannelMembership).where(
            UserChannelMembership.bot_id == bot_id,
            UserChannelMembership.max_user_id == max_user_id,
            UserChannelMembership.channel_id == channel_id,
        )
    )
    membership = existing.scalar_one_or_none()
    if membership:
        # Обновляем время и, если появилось название, сохраняем его.
        membership.joined_at = datetime.now(timezone.utc)
        if channel_title and not membership.channel_title:
            membership.channel_title = channel_title
    else:
        session.add(UserChannelMembership(
            bot_id=bot_id,
            max_user_id=max_user_id,
            channel_id=channel_id,
            channel_title=channel_title,
        ))
    await session.commit()
    logger.info(
        "Channel membership saved: user=%s channel=%s (%s) bot=%s",
        max_user_id, channel_id, channel_title or "?", bot_id,
    )


async def _handle_member_removed(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """Обрабатывает выход участника из чата (``user_removed`` / ``chat_member_removed``).

    Если пользователь вышел из канала (``is_channel=True``) — удаляем
    запись из ``user_channel_memberships``.
    Выход из группы пока не обрабатывается (UserBotContext не трогаем —
    история переписки и привязка к ассистенту должны остаться).
    """
    is_channel = bool(update.get("is_channel"))
    if not is_channel:
        logger.debug("user_removed from group — skipping (no action needed)")
        return

    chat_id = str(
        update.get("chat_id")
        or update.get("chat", {}).get("chat_id", "")
    )
    user_info = update.get("user", {})
    max_user_id = str(user_info.get("user_id", ""))

    if not chat_id or not max_user_id or bot_id is None:
        logger.debug("user_removed: missing chat_id, user_id or bot_id")
        return

    result = await session.execute(
        delete(UserChannelMembership).where(
            UserChannelMembership.bot_id == bot_id,
            UserChannelMembership.max_user_id == max_user_id,
            UserChannelMembership.channel_id == chat_id,
        )
    )
    await session.commit()
    if result.rowcount:
        logger.info(
            "Channel membership removed: user=%s channel=%s bot=%s",
            max_user_id, chat_id, bot_id,
        )
    else:
        logger.debug(
            "user_removed from channel: no membership record found (user=%s channel=%s bot=%s)",
            max_user_id, chat_id, bot_id,
        )


# ── Удаление сообщений у непроверенных участников ────────────────────────────

async def _delete_if_unverified(
    update: dict,
    session: AsyncSession,
    client: MaxClient,
    bot_id: int | None = None,
) -> None:
    """Удаляет сообщение, если автор ещё не прошёл верификацию.

    Замена недостающего mute/restrict в MAX API: если у юзера есть
    pending-VerificationRequest в этой группе, его сообщения не должны
    быть видны. Поиск идёт по обоим типам конфигов одновременно
    (welcome_config_id IN (...) OR pair_id IN (...)) — одним запросом.

    Ошибка delete_message глотается до warning — бот может уже не быть
    админом в группе или сообщение уже удалено пользователем.
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


# ── Вспомогательные функции ──────────────────────────────────────────────────

_PROFILE_REFRESH_INTERVAL = timedelta(hours=1)


async def _upsert_user_profile(
    session: AsyncSession,
    bot_id: int,
    max_user_id: str,
    sender: dict,
    client: MaxClient,
) -> None:
    """Создаёт или обновляет профиль пользователя через GET /users/{user_id}.

    MAX Bot API не отдаёт полный профиль в ``sender`` DM-события —
    там присутствуют только user_id и name. Полные данные (last_name,
    username, description, avatar_url, full_avatar_url) доступны только
    через явный запрос ``GET /users/{user_id}``.

    Кеш: API не вызывается, если профиль уже был обновлён менее часа назад.
    Fallback: если API вернул ошибку — используем поля из ``sender``.

    Не делает commit — вызывается до основного session.commit() хендлера.
    """
    now = datetime.now(timezone.utc)

    existing = await session.execute(
        select(UserProfile).where(
            UserProfile.bot_id == bot_id,
            UserProfile.max_user_id == max_user_id,
        )
    )
    profile = existing.scalar_one_or_none()

    # Определяем, нужно ли идти в API.
    # Если профиль свежий (< 1 часа) — используем данные из sender как было.
    needs_api_call = (
        profile is None
        or profile.last_synced_at is None
        or (now - profile.last_synced_at) >= _PROFILE_REFRESH_INTERVAL
    )

    # Пробуем получить полный профиль через API
    user_data: dict = {}
    if needs_api_call:
        try:
            user_data = await client.get_user(max_user_id)
            logger.debug("Got user profile from API for %s: %s", max_user_id, user_data)
        except Exception as exc:
            logger.warning("Could not fetch user profile for %s: %s", max_user_id, exc)
            # Fallback: данные из sender (могут быть неполными)
            user_data = sender

    # Если API не вызывали — ничего не меняем в профиле кроме полей от sender
    # (только первое имя, оно обычно приходит)
    if not needs_api_call:
        if profile:
            # Обновляем только first_name из sender — другие поля могут отсутствовать
            first_name_raw = sender.get("first_name") or sender.get("name") or None
            if first_name_raw and profile.first_name != first_name_raw:
                profile.first_name = first_name_raw
        return

    # Извлекаем поля из полного ответа API
    first_name: str | None = (
        user_data.get("first_name")
        or user_data.get("name")
        or None
    )
    last_name: str | None = user_data.get("last_name") or None
    username: str | None = user_data.get("username") or None
    description: str | None = user_data.get("description") or None

    # Аватар: MAX отдаёт несколько вариантов поля в зависимости от контекста
    _photo = user_data.get("photo")
    avatar_url: str | None = (
        user_data.get("avatar_url")
        or user_data.get("photo_url")
        or (_photo.get("url") if isinstance(_photo, dict) else _photo)
        or None
    )
    full_avatar_url: str | None = user_data.get("full_avatar_url") or None

    if profile:
        # Обновляем только поля, которые пришли непустыми, —
        # чтобы не затирать старые данные «пустышками» при неполных ответах.
        if first_name is not None:
            profile.first_name = first_name
        if last_name is not None:
            profile.last_name = last_name
        if username is not None:
            profile.username = username
        if description is not None:
            profile.description = description
        if avatar_url is not None:
            profile.avatar_url = avatar_url
        if full_avatar_url is not None:
            profile.full_avatar_url = full_avatar_url
        profile.last_synced_at = now
    else:
        session.add(UserProfile(
            bot_id=bot_id,
            max_user_id=max_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
            description=description,
            avatar_url=avatar_url,
            full_avatar_url=full_avatar_url,
        ))


async def _send_dm_safe(
    client: MaxClient,
    chat_id: str,
    text: str,
    attachments: list[dict] | None = None,
) -> None:
    """Отправляет DM, проглатывая ошибки.

    Пользователь мог заблокировать бота, удалить аккаунт или просто
    нажать кнопку с устаревшего устройства — это не повод падать
    в основном потоке.
    """
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
    """Пытается отредактировать сообщение, возвращает True/False вместо исключения.

    Используется для добавления кнопки «Прокомментировать» к посту в
    канале: если редактирование не прошло (бот не админ, формат, лимит
    редактирования), мы хотим продолжить работу, а не упасть.
    """
    try:
        await client.edit_message(message_id=message_id, text=text or "", attachments=attachments)
        logger.info("Edited post %s with %d attachment(s)", message_id, len(attachments))
        return True
    except MaxAPIError as exc:
        logger.warning("Edit %s failed (status=%s): %s", message_id, exc.status, exc.body)
        return False


def _extract_media_attachments(body: dict) -> list[dict]:
    """Достаёт media-вложения, пригодные для пересылки в другой чат.

    MAX выдаёт три класса вложений:
      - media (image/video/audio/file) — у каждого есть ``token``,
        который можно переотправить в другой чат как есть;
      - share — превью URL/контактов; токен привязан к контексту канала
        и в чужом чате обычно не работает;
      - inline_keyboard — это уже UI поверх сообщения, не пересылаем.
    Берём только первый класс.
    """
    attachments = []
    for att in body.get("attachments", []):
        att_type = att.get("type", "")
        payload = att.get("payload", {})
        token = payload.get("token")
        if att_type in ("image", "video", "audio", "file") and token:
            attachments.append({"type": att_type, "payload": {"token": token}})
    return attachments


def _extract_dm_attachments(body: dict) -> list[dict]:
    """Извлекает вложения DM для сохранения в ConversationMessage.attachments_json.

    В отличие от ``_extract_media_attachments`` (пересылка в группу),
    здесь сохраняем максимум полезных метаданных для отображения в инбоксе:
    тип, токен, URL превью, имя файла, размер.

    ``inline_keyboard`` не сохраняем — это UI-элемент, не контент.
    """
    result = []
    for att in body.get("attachments", []):
        att_type = att.get("type", "")
        if att_type in ("inline_keyboard",):
            continue
        payload = att.get("payload", {})
        item: dict = {"type": att_type}
        # Токен для скачивания через MAX API
        if payload.get("token"):
            item["token"] = payload["token"]
        # Прямой URL (share, sticker)
        if payload.get("url"):
            item["url"] = payload["url"]
        # Мета файла
        if payload.get("filename"):
            item["filename"] = payload["filename"]
        if payload.get("size"):
            item["size"] = payload["size"]
        # Превью для image/video — MAX хранит URL по-разному в зависимости от версии:
        #   payload.url                       → прямая ссылка
        #   payload.photo / payload.thumbnail → {"url": "https://..."} или строка
        #   payload.photos                    → {"<size>": {"url": "..."}, ...}
        if isinstance(payload.get("url"), str) and payload["url"]:
            item["preview_url"] = payload["url"]
        if "preview_url" not in item:
            for thumb_key in ("photo", "thumbnail"):
                thumb = payload.get(thumb_key)
                if isinstance(thumb, dict) and thumb.get("url"):
                    item["preview_url"] = thumb["url"]
                    break
                elif isinstance(thumb, str) and thumb:
                    item["preview_url"] = thumb
                    break
        # Fallback: photos dict {"320": {"url": ...}, "640": {"url": ...}, ...}
        if "preview_url" not in item:
            photos_dict = payload.get("photos")
            if isinstance(photos_dict, dict):
                for photo_variant in photos_dict.values():
                    if isinstance(photo_variant, dict) and photo_variant.get("url"):
                        item["preview_url"] = photo_variant["url"]
                        break
        result.append(item)
    return result


def _extract_share_url(body: dict) -> str | None:
    """Возвращает URL первого share-вложения (если есть)."""
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
    """Кладёт запись в EventLog без отдельного commit'а.

    Не вызывает session.commit() — это делает caller вместе с другими
    изменениями, чтобы лог писался атомарно с бизнес-операцией.
    """
    session.add(EventLog(user_id=user_id, bot_id=bot_id, level=level, message=message))
