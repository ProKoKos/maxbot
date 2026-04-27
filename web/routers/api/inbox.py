"""Инбокс бота (DM-переписки + AI-ассистент): /bots/{id}/inbox/*.

Источник данных — таблица ConversationMessage (история DM с AI),
дополнительно подтягиваются аватары/иконки из MAX API
(с кешированием в ConversationMessage.user_avatar для следующих запросов).
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func as sqlfunc, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from bot.client import MaxAPIError, MaxClient
from bot.crypto import decrypt_token
from db.models import (
    AssistantConfig,
    Bot,
    ConversationMessage,
    InboxReadStatus,
    UserBotContext,
    UserChannelMembership,
    VerificationRequest,
)
from web.deps import CurrentUser, DBSession, RateLimit

router = APIRouter()
_log = logging.getLogger("web.api.inbox")


async def _require_bot_owner(bot_id: int, user_id: int, session) -> Bot:
    """Проверка ownership бота. 404, если бот чужой/не существует."""
    result = await session.execute(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == user_id)
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found")
    return bot


@router.get("/bots/{bot_id}/inbox/groups")
async def inbox_groups(bot_id: int, current_user: CurrentUser, session: DBSession, _: RateLimit):
    bot = await _require_bot_owner(bot_id, current_user.id, session)

    configs_result = await session.execute(
        select(AssistantConfig).where(AssistantConfig.bot_id == bot_id)
    )
    configs = configs_result.scalars().all()

    # Кол-во пользователей с непрочитанными сообщениями per group
    unread_per_config_result = await session.execute(
        select(
            ConversationMessage.assistant_config_id,
            sqlfunc.count(sqlfunc.distinct(ConversationMessage.max_user_id)).label("unread_users"),
        )
        .outerjoin(
            InboxReadStatus,
            (InboxReadStatus.bot_id == ConversationMessage.bot_id) &
            (InboxReadStatus.max_user_id == ConversationMessage.max_user_id),
        )
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.role == "user",
            or_(
                InboxReadStatus.last_read_at.is_(None),
                ConversationMessage.created_at > InboxReadStatus.last_read_at,
            ),
        )
        .group_by(ConversationMessage.assistant_config_id)
    )
    unread_by_config: dict[int | None, int] = {
        row.assistant_config_id: row.unread_users for row in unread_per_config_result
    }

    # Общее кол-во distinct пользователей с непрочитанными (для кнопки "Все")
    total_unread_result = await session.execute(
        select(sqlfunc.count(sqlfunc.distinct(ConversationMessage.max_user_id)))
        .outerjoin(
            InboxReadStatus,
            (InboxReadStatus.bot_id == ConversationMessage.bot_id) &
            (InboxReadStatus.max_user_id == ConversationMessage.max_user_id),
        )
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.role == "user",
            or_(
                InboxReadStatus.last_read_at.is_(None),
                ConversationMessage.created_at > InboxReadStatus.last_read_at,
            ),
        )
    )
    total_unread = total_unread_result.scalar() or 0

    # Подтягиваем иконки групп через MAX API
    chat_icons: dict[str, str | None] = {}
    try:
        token = decrypt_token(bot.encrypted_token)
        async with MaxClient(token=token) as client:
            async def _fetch_icon(group_id: str) -> tuple[str, str | None]:
                try:
                    data = await client.get_chat(group_id)
                    _ic = data.get("icon")
                    icon = (
                        (_ic.get("url") if isinstance(_ic, dict) else _ic)
                        or data.get("avatar_url")
                        or data.get("photo_url")
                        or None
                    )
                    return group_id, icon
                except MaxAPIError:
                    return group_id, None
            results = await asyncio.gather(*[_fetch_icon(cfg.group_id) for cfg in configs])
            chat_icons = dict(results)
    except Exception:
        pass

    groups = [{"id": "all", "name": "Все", "count": total_unread, "icon": None}]
    for cfg in configs:
        groups.append({
            "id": cfg.id,
            "name": cfg.group_name or f"Группа {cfg.group_id}",
            "count": unread_by_config.get(cfg.id, 0),
            "icon": chat_icons.get(cfg.group_id),
        })
    return groups


@router.get("/bots/{bot_id}/inbox/users")
async def inbox_users(
    bot_id: int,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
    group_id: str = "all",
):
    bot = await _require_bot_owner(bot_id, current_user.id, session)

    q = select(
        ConversationMessage.max_user_id,
        sqlfunc.max(ConversationMessage.created_at).label("last_at"),
    ).where(ConversationMessage.bot_id == bot_id)

    if group_id != "all":
        try:
            config_id = int(group_id)
        except ValueError:
            raise HTTPException(400, "invalid group_id")
        q = q.where(ConversationMessage.assistant_config_id == config_id)

    q = q.group_by(ConversationMessage.max_user_id).order_by(sqlfunc.max(ConversationMessage.created_at).desc())

    rows = (await session.execute(q)).all()
    if not rows:
        return []

    user_ids = [r.max_user_id for r in rows]

    # Загружаем всю переписку выбранных пользователей одним запросом —
    # отсюда же берём last_msg, аватары и first_user_msg, чтобы не делать
    # три отдельных round-trip'а в БД (это и есть устранение N+1).
    # Сортировка DESC: первый встреченный msg.max_user_id и есть последний.
    last_msg_result = await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.max_user_id.in_(user_ids),
        )
        .order_by(ConversationMessage.created_at.desc())
    )
    all_msgs = last_msg_result.scalars().all()
    last_msg: dict[str, ConversationMessage] = {}
    for m in all_msgs:
        if m.max_user_id not in last_msg:
            last_msg[m.max_user_id] = m

    # user names from verification_requests (take the most recent per user)
    names_result = await session.execute(
        select(VerificationRequest.max_user_id, VerificationRequest.user_name)
        .where(VerificationRequest.max_user_id.in_(user_ids))
        .order_by(VerificationRequest.created_at.desc())
    )
    names: dict[str, str] = {}
    for uid, uname in names_result.all():
        if uid not in names and uname:
            names[uid] = uname

    # берём аватар из сохранённых сообщений; если нет — идём в MAX API
    stored_avatars: dict[str, str | None] = {}
    last_user_msg: dict[str, ConversationMessage] = {}
    for msg in all_msgs:  # DESC: первый встреченный per user — самый свежий
        uid = msg.max_user_id
        if uid not in stored_avatars:
            stored_avatars[uid] = msg.user_avatar or None
        elif msg.user_avatar and not stored_avatars[uid]:
            stored_avatars[uid] = msg.user_avatar
        if msg.role == "user" and uid not in last_user_msg:
            last_user_msg[uid] = msg

    missing = [uid for uid in user_ids if not stored_avatars.get(uid)]
    if missing:
        try:
            token = decrypt_token(bot.encrypted_token)

            def _url_from(data: dict, *fields: str) -> str | None:
                """Извлекает URL из dict по нескольким возможным именам полей."""
                for f in fields:
                    v = data.get(f)
                    if isinstance(v, dict):
                        u = v.get("url")
                        if u:
                            return u
                    elif isinstance(v, str) and v:
                        return v
                return None

            async with MaxClient(token=token) as client:
                async def _fetch_avatar(uid: str) -> tuple[str, str | None]:
                    # 1. /users/{uid}
                    try:
                        data = await client.get_user(uid)
                        url = _url_from(data, "avatar_url", "photo_url", "photo", "avatar")
                        if url:
                            return uid, url
                    except MaxAPIError:
                        pass
                    # 2. DM-чат из последнего сообщения пользователя
                    chat_ids: list[str] = []
                    fmsg = last_user_msg.get(uid)
                    if fmsg and fmsg.chat_id and fmsg.chat_id != uid:
                        chat_ids.append(fmsg.chat_id)
                    # 3. В MAX DM chat_id зачастую == user_id — пробуем напрямую
                    chat_ids.append(uid)
                    for cid in chat_ids:
                        try:
                            data = await client.get_chat(cid)
                            url = _url_from(data, "icon", "avatar_url", "photo_url", "photo", "avatar")
                            if url:
                                return uid, url
                            # MAX для диалогов кладёт инфо о собеседнике в dialog_with_user
                            dwu = data.get("dialog_with_user") or data.get("owner")
                            if isinstance(dwu, dict):
                                url = _url_from(dwu, "avatar_url", "photo_url", "photo", "avatar", "full_avatar_url")
                                if url:
                                    return uid, url
                            _log.warning(
                                "Avatar fields not found for uid=%s cid=%s, keys=%s",
                                uid, cid, list(data.keys()),
                            )
                        except MaxAPIError:
                            pass
                    return uid, None

                fetched = dict(await asyncio.gather(*[_fetch_avatar(uid) for uid in missing]))

            # Кешируем в БД в самое свежее сообщение пользователя
            for uid, url in fetched.items():
                if url:
                    stored_avatars[uid] = url
                    msg_to_update = last_user_msg.get(uid)
                    if msg_to_update and not msg_to_update.user_avatar:
                        msg_to_update.user_avatar = url
            await session.commit()
        except Exception as exc:
            _log.warning("Avatar fetch failed for bot %s: %s", bot_id, exc)

    # Кол-во непрочитанных сообщений per user
    unread_result = await session.execute(
        select(
            ConversationMessage.max_user_id,
            sqlfunc.count(ConversationMessage.id).label("unread_count"),
        )
        .outerjoin(
            InboxReadStatus,
            (InboxReadStatus.bot_id == ConversationMessage.bot_id) &
            (InboxReadStatus.max_user_id == ConversationMessage.max_user_id),
        )
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.max_user_id.in_(user_ids),
            ConversationMessage.role == "user",
            or_(
                InboxReadStatus.last_read_at.is_(None),
                ConversationMessage.created_at > InboxReadStatus.last_read_at,
            ),
        )
        .group_by(ConversationMessage.max_user_id)
    )
    unread_counts: dict[str, int] = {row.max_user_id: row.unread_count for row in unread_result}

    _ATT_LABELS = {
        "image": "📷 Фото", "video": "🎥 Видео",
        "audio": "🎤 Голосовое", "file": "📎 Файл",
        "share": "🔗 Ссылка",
    }

    users = []
    for r in rows:
        uid = r.max_user_id
        m = last_msg.get(uid)
        last_text = (m.content or "").strip() if m else ""
        if not last_text and m:
            try:
                atts = json.loads(m.attachments_json or "[]")
                if atts:
                    last_text = _ATT_LABELS.get(atts[0].get("type", ""), "📎 Вложение")
            except Exception:
                pass
        users.append({
            "user_id": uid,
            "name": names.get(uid) or f"User {uid}",
            "avatar": stored_avatars.get(uid),
            "unread_count": unread_counts.get(uid, 0),
            "last_message": last_text[:200],
            "last_role": m.role if m else "",
            "last_at": r.last_at.isoformat() if r.last_at else None,
        })
    return users


@router.get("/bots/{bot_id}/inbox/messages")
async def inbox_messages(
    bot_id: int,
    user_id: str,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
    group_id: str = "all",
):
    await _require_bot_owner(bot_id, current_user.id, session)

    q = select(ConversationMessage).where(
        ConversationMessage.bot_id == bot_id,
        ConversationMessage.max_user_id == user_id,
    )
    if group_id != "all":
        try:
            config_id = int(group_id)
        except ValueError:
            raise HTTPException(400, "invalid group_id")
        q = q.where(ConversationMessage.assistant_config_id == config_id)

    q = q.order_by(ConversationMessage.created_at.asc())
    result = await session.execute(q)
    msgs = result.scalars().all()

    # Помечаем как прочитанные
    now = datetime.now(timezone.utc)
    stmt = pg_insert(InboxReadStatus).values(
        bot_id=bot_id,
        max_user_id=user_id,
        last_read_at=now,
    ).on_conflict_do_update(
        constraint="uq_inbox_read_bot_user",
        set_={"last_read_at": now},
    )
    await session.execute(stmt)
    await session.commit()

    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "attachments": json.loads(m.attachments_json or "[]"),
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


@router.get("/bots/{bot_id}/inbox/profile/{user_id}")
async def inbox_profile(
    bot_id: int,
    user_id: str,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    """Профиль пользователя в инбоксе.

    Возвращает агрегированную информацию: базовый профиль, статистику,
    группы (через UserBotContext), ссылки из текста и вложения по типам.
    Используется правым профильным панелью в inbox.html.
    """
    await _require_bot_owner(bot_id, current_user.id, session)

    # Все сообщения пользователя (ASC — нужны для first/last)
    msgs_result = await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.max_user_id == user_id,
        )
        .order_by(ConversationMessage.created_at.asc())
    )
    msgs = msgs_result.scalars().all()
    if not msgs:
        raise HTTPException(404, "No messages found for this user")

    first_msg = msgs[0]
    last_msg_obj = msgs[-1]

    # Аватар — берём из самого свежего сообщения с непустым user_avatar
    avatar = None
    for m in reversed(msgs):
        if m.user_avatar:
            avatar = m.user_avatar
            break

    # Имя — из VerificationRequest (самый свежий)
    name_result = await session.execute(
        select(VerificationRequest.user_name)
        .where(VerificationRequest.max_user_id == user_id)
        .order_by(VerificationRequest.created_at.desc())
        .limit(1)
    )
    name = name_result.scalar() or f"User {user_id}"

    # Статистика сообщений
    user_msg_count = sum(1 for m in msgs if m.role == "user")
    bot_msg_count = sum(1 for m in msgs if m.role == "assistant")

    # Группы пользователя (через UserBotContext → AssistantConfig)
    contexts_result = await session.execute(
        select(UserBotContext)
        .where(
            UserBotContext.bot_id == bot_id,
            UserBotContext.max_user_id == user_id,
        )
        .options(selectinload(UserBotContext.assistant_config))
    )
    groups = []
    for ctx in contexts_result.scalars().all():
        cfg = ctx.assistant_config
        groups.append({
            "group_id": ctx.group_id,
            "group_name": cfg.group_name if cfg else ctx.group_id,
        })

    # Каналы пользователя (через UserChannelMembership)
    channels_result = await session.execute(
        select(UserChannelMembership)
        .where(
            UserChannelMembership.bot_id == bot_id,
            UserChannelMembership.max_user_id == user_id,
        )
        .order_by(UserChannelMembership.joined_at.asc())
    )
    channels = [
        {
            "channel_id": m.channel_id,
            "channel_title": m.channel_title or m.channel_id,
        }
        for m in channels_result.scalars().all()
    ]

    # Ссылки — regex-поиск по тексту сообщений
    _url_re = re.compile(r"https?://[^\s<>\"'{}|\\^`\[\]]+")
    links: list[dict] = []
    for m in msgs:
        if m.content:
            for found_url in _url_re.findall(m.content):
                links.append({
                    "url": found_url,
                    "date": m.created_at.isoformat(),
                    "role": m.role,
                })

    # Вложения по типу (из attachments_json)
    media: list[dict] = []
    files: list[dict] = []
    voices: list[dict] = []
    for m in msgs:
        try:
            atts = json.loads(m.attachments_json or "[]")
        except Exception:
            atts = []
        for att in atts:
            entry = {**att, "date": m.created_at.isoformat(), "role": m.role}
            t = att.get("type", "")
            if t in ("image", "video"):
                media.append(entry)
            elif t == "audio":
                voices.append(entry)
            elif t == "file":
                files.append(entry)

    return {
        "user_id": user_id,
        "name": name,
        "avatar": avatar,
        "first_contact": first_msg.created_at.isoformat(),
        "last_active": last_msg_obj.created_at.isoformat(),
        "user_msg_count": user_msg_count,
        "bot_msg_count": bot_msg_count,
        "groups": groups,
        "channels": channels,
        "links": links[-100:],
        "media": media[-100:],
        "files": files[-100:],
        "voices": voices[-100:],
    }


class InboxSendRequest(BaseModel):
    user_id: str
    text: str = ""
    # Вложения в «storage»-формате: [{type, token, filename?, size?}]
    # Бэкенд конвертирует в MAX API-формат при отправке.
    attachments: list[dict] | None = None
    assistant_config_id: int | None = None


@router.post("/bots/{bot_id}/inbox/send")
async def inbox_send(
    bot_id: int,
    body: InboxSendRequest,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    if not body.text and not body.attachments:
        raise HTTPException(400, "Either text or attachments must be provided")

    bot = await _require_bot_owner(bot_id, current_user.id, session)

    # Ищем реальный chat_id из истории сообщений пользователя
    last_msg_result = await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.bot_id == bot_id,
            ConversationMessage.max_user_id == body.user_id,
            ConversationMessage.chat_id.isnot(None),
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(1)
    )
    last_msg = last_msg_result.scalar_one_or_none()
    chat_id = last_msg.chat_id if last_msg else body.user_id

    try:
        token = decrypt_token(bot.encrypted_token)
    except ValueError:
        raise HTTPException(500, "Token decryption failed")

    # Конвертируем storage-формат → MAX API-формат для отправки
    max_atts: list[dict] | None = None
    if body.attachments:
        max_atts = [
            {"type": att["type"], "payload": {"token": att["token"]}}
            for att in body.attachments
            if att.get("token")
        ] or None

    async with MaxClient(token=token) as client:
        try:
            max_resp = await client.send_message(
                chat_id=chat_id,
                text=body.text or "",
                attachments=max_atts,
                format="markdown",
            )
        except MaxAPIError as e:
            raise HTTPException(502, f"Max API error: {e}")

    # Пытаемся извлечь реальные URL изображений из ответа MAX API.
    # MAX возвращает Message-объект; вложения могут быть в разных форматах:
    #   payload.photo/thumbnail → {"url": "..."}
    #   payload.photos          → {"<size>": {"url": "..."}, ...}
    atts_to_store: list[dict] = list(body.attachments or [])
    if atts_to_store and isinstance(max_resp, dict):
        resp_body = (
            max_resp.get("message", {}).get("body", {})
            or max_resp.get("body", {})
        )
        resp_atts = resp_body.get("attachments", []) if isinstance(resp_body, dict) else []
        logging.getLogger("web.api.inbox.send").info(
            "inbox_send MAX response atts bot=%s resp_atts=%r", bot_id, resp_atts
        )
        for i, stored_att in enumerate(atts_to_store):
            resp_att = resp_atts[i] if i < len(resp_atts) else {}
            payload = resp_att.get("payload", {}) if isinstance(resp_att, dict) else {}
            att_type = stored_att.get("type", "")
            new_att = dict(stored_att)

            # Извлекаем прямую ссылку на файл из ответа MAX
            direct_url: str | None = None
            if isinstance(payload.get("url"), str) and payload["url"]:
                direct_url = payload["url"]
            if not direct_url:
                for key in ("photo", "thumbnail"):
                    thumb = payload.get(key)
                    if isinstance(thumb, dict) and thumb.get("url"):
                        direct_url = thumb["url"]
                        break
                    elif isinstance(thumb, str) and thumb:
                        direct_url = thumb
                        break
            if not direct_url:
                photos_dict = payload.get("photos")
                if isinstance(photos_dict, dict):
                    for pv in photos_dict.values():
                        if isinstance(pv, dict) and pv.get("url"):
                            direct_url = pv["url"]
                            break

            if direct_url:
                new_att["url"] = direct_url
                if att_type == "image":
                    new_att["preview_url"] = direct_url
            if new_att.get("preview_url", "").startswith("blob:"):
                del new_att["preview_url"]
            atts_to_store[i] = new_att

    msg = ConversationMessage(
        bot_id=bot_id,
        max_user_id=body.user_id,
        chat_id=chat_id,
        assistant_config_id=body.assistant_config_id,
        role="assistant",
        content=body.text or "",
        attachments_json=json.dumps(atts_to_store, ensure_ascii=False),
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return {"ok": True, "id": msg.id, "created_at": msg.created_at.isoformat()}


@router.post("/bots/{bot_id}/inbox/upload")
async def inbox_upload(
    bot_id: int,
    file: UploadFile,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    """Загружает файл в MAX API и возвращает токен вложения.

    Клиент сначала вызывает этот эндпоинт (получает token),
    потом передаёт token в /inbox/send в поле attachments.
    Поддерживаемые типы: изображения, видео, аудио, произвольные файлы.
    Максимальный размер: 20 МБ.
    """
    MAX_SIZE = 20 * 1024 * 1024  # 20 МБ
    _ul = logging.getLogger("web.api.inbox.upload")

    bot = await _require_bot_owner(bot_id, current_user.id, session)

    try:
        token = decrypt_token(bot.encrypted_token)
    except ValueError:
        raise HTTPException(500, "Token decryption failed")

    ct = file.content_type or "application/octet-stream"
    filename = file.filename or "file"

    # MAX API принимает type=image|video|audio|file (не "photo")
    if ct.startswith("image/"):
        att_type, store_type = "image", "image"
    elif ct.startswith("video/"):
        att_type, store_type = "video", "video"
    elif ct.startswith("audio/"):
        att_type, store_type = "audio", "audio"
    else:
        att_type, store_type = "file", "file"

    file_bytes = await file.read()
    if len(file_bytes) > MAX_SIZE:
        raise HTTPException(413, "Файл слишком большой (максимум 20 МБ)")

    async with MaxClient(token=token) as client:
        try:
            result = await client.upload_attachment(file_bytes, filename, ct, att_type)
        except MaxAPIError as e:
            _ul.error(
                "MAX upload failed bot=%s att_type=%s filename=%s status=%s body=%r",
                bot_id, att_type, filename, e.status, e.body,
            )
            raise HTTPException(502, f"MAX API upload error (status {e.status}): {e.body}")

    # MAX возвращает разные структуры в зависимости от типа:
    #   файлы/аудио/видео → {"token": "..."}
    #   картинки          → {"photos": {"<size>": {"token": "..."}, ...}}
    _ul.info("MAX upload raw result bot=%s att_type=%s result=%r", bot_id, att_type, result)
    photos_val = result.get("photos")
    photos_token = ""
    if isinstance(photos_val, dict):
        first_photo = next(iter(photos_val.values()), None)
        if isinstance(first_photo, dict):
            photos_token = first_photo.get("token") or first_photo.get("file_id") or ""
        elif isinstance(first_photo, str):
            photos_token = first_photo
    elif isinstance(photos_val, list) and photos_val:
        item = photos_val[0]
        if isinstance(item, str):
            photos_token = item
        elif isinstance(item, dict):
            photos_token = item.get("token") or item.get("file_id") or ""
    upload_token = (
        result.get("token")
        or result.get("file_id")
        or photos_token
        or ""
    )
    if not upload_token:
        raise HTTPException(502, f"MAX API не вернул token: {result}")

    store_att: dict = {"type": store_type, "token": upload_token, "size": len(file_bytes)}
    if store_type == "file":
        store_att["filename"] = filename

    return {
        "ok": True,
        "attachment": store_att,
        "type": store_type,
        "filename": filename,
        "size": len(file_bytes),
    }


@router.get("/bots/{bot_id}/inbox/proxy")
async def inbox_proxy_download(
    bot_id: int,
    current_user: CurrentUser,
    session: DBSession,
    url: str,
    filename: str | None = None,
):
    """Прокси-скачивание файла с MAX CDN с правильным Content-Disposition.

    CDN MAX не возвращает имя файла в заголовках, а атрибут ``download``
    в HTML работает только для same-origin URL. Этот эндпоинт скачивает
    файл с CDN и отдаёт клиенту с нужным ``Content-Disposition``.

    SSRF-защита: разрешены только домены ``*.oneme.ru`` и ``*.max.ru``.
    """
    _host = urlparse(url).hostname or ""
    if not (_host.endswith(".oneme.ru") or _host.endswith(".max.ru")):
        raise HTTPException(400, "URL не разрешён")

    await _require_bot_owner(bot_id, current_user.id, session)

    async def _stream():
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as _c:
            async with _c.stream("GET", url) as _resp:
                async for chunk in _resp.aiter_bytes(65536):
                    yield chunk

    resp_headers: dict[str, str] = {}
    if filename:
        # RFC 5987: поддержка UTF-8 имён файлов во всех браузерах
        safe_ascii = filename.encode("ascii", errors="replace").decode()
        encoded = quote(filename, safe="")
        resp_headers["Content-Disposition"] = (
            f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded}'
        )
    else:
        resp_headers["Content-Disposition"] = "attachment"

    return StreamingResponse(
        _stream(),
        media_type="application/octet-stream",
        headers=resp_headers,
    )
