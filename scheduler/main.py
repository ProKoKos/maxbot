"""
Сервис планировщика (отдельный контейнер docker-compose).

APScheduler в контейнере scheduler выполняет две независимые джобы:

  publish_due_posts — каждые 30 сек публикует отложенные ScheduledPost'ы,
      время которых наступило. Делает это через MaxClient бота, к которому
      привязана пара поста.

  kick_expired_verifications — каждые 60 сек ищет VerificationRequest'ы
      с status=pending и истёкшим deadline. Если у конфига включён
      verification_kick — выгоняет пользователя из группы; в любом случае
      удаляет сообщение-приветствие из чата и помечает запрос как
      kicked / expired.

ВАЖНО: на текущий момент НЕ реализован distributed lock. Если запустить
два экземпляра scheduler одновременно (горизонтальное масштабирование),
оба возьмутся за одни и те же записи и могут продублировать публикации.
В моноинстансовом режиме всё корректно за счёт ``max_instances=1``
и ``coalesce=True`` у каждой джобы.
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
    WelcomeConfig,
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
    """Подстраховка от случая «миграции не применились»: создаёт
    недостающие таблицы. Основная схема — через alembic в сервисе migrate."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def publish_due_posts() -> None:
    """Публикует пачку готовых к отправке постов (до 20 за тик)."""
    now = datetime.now(timezone.utc)

    # Берём первые 20 «созревших» постов. Лимит выбран намеренно:
    # каждая публикация делает до 2 round-trip'ов к MAX API, а одна
    # итерация джобы должна укладываться в её период (30 сек).
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.status == PostStatus.pending,
                ScheduledPost.scheduled_at <= now,
            )
            .options(
                # selectinload вместо joinedload — отдельный SELECT, но без
                # cartesian-произведения; для маленького limit'а это быстрее.
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
    """Публикует один пост в своей собственной сессии.

    Каждая публикация выполняется в отдельной сессии, чтобы:
      - падение одной не откатывало успешные;
      - можно было точечно коммитить статус и ошибку для каждого поста.
    """
    async with AsyncSessionLocal() as session:
        # Перечитываем post в новой сессии (объект из publish_due_posts'а
        # «detached» — нужно прицепить к session.commit'ам).
        result = await session.execute(
            select(ScheduledPost)
            .where(ScheduledPost.id == post.id)
            .options(selectinload(ScheduledPost.pair).selectinload(ChannelGroupPair.bot))
        )
        post = result.scalar_one()
        pair: ChannelGroupPair | None = post.pair
        bot: Bot | None = pair.bot if pair else None

        # Гард: пара/бот могли быть удалены или отключены, пока пост ждал.
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
                # Шаг 1: публикуем в канале (с inline-кнопкой комментариев).
                resp = await client.send_message(
                    chat_id=pair.channel_id,
                    text=post.text,
                    attachments=attachments,
                )
                channel_msg_id = str(resp.get("message", {}).get("body", {}).get("mid", ""))

                # Шаг 2: дублируем тот же пост в группу обсуждений
                # с шапкой-ссылкой на канал. Это инициализирует тред,
                # к которому ведёт inline-кнопка из канала.
                group_text = discussion_header(pair.channel_name, channel_msg_id, pair.channel_link or "") + post.text
                group_resp = await client.send_message(
                    chat_id=pair.group_id,
                    text=group_text,
                )
                group_msg_id = str(group_resp.get("message", {}).get("body", {}).get("mid", ""))

                # Шаг 3: фиксируем связь, чтобы при появлении update'а
                # message_created handlers'ы не продублировали пост ещё раз.
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


# Дефолтный текст уведомления о кике. Сейчас не отправляется в группу
# (см. CLAUDE.md — MAX API не поддерживает mute, мы просто кикаем),
# оставлен на случай будущей реализации «sticky» уведомления.
_DEFAULT_KICK_MSG = "❌ **{имя}** не прошёл(а) проверку вовремя и был(а) исключён(а) из группы."


async def kick_expired_verifications() -> None:
    """Раз в минуту обходит просроченные verification-запросы.

    На каждый запрос: kick'ает (если включено), удаляет приветствие
    из группы и помечает status=kicked / expired.
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
                selectinload(VerificationRequest.welcome_config).selectinload(WelcomeConfig.bot),
                selectinload(VerificationRequest.pair).selectinload(ChannelGroupPair.bot),
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
    """Обрабатывает один просроченный запрос в собственной сессии."""
    async with AsyncSessionLocal() as session:
        # Перечитываем с обеими ветками конфига (welcome_config / pair) —
        # какая именно «жива», узнаем по filled FK.
        result = await session.execute(
            select(VerificationRequest)
            .where(VerificationRequest.id == vr.id)
            .options(
                selectinload(VerificationRequest.welcome_config).selectinload(WelcomeConfig.bot),
                selectinload(VerificationRequest.pair).selectinload(ChannelGroupPair.bot),
            )
        )
        vr = result.scalar_one_or_none()
        if not vr or vr.status != VerificationStatus.pending:
            # Гонка с handlers._handle_bot_started: пользователь успел
            # верифицироваться между SELECT'ом publish_due_posts и нашим
            # SELECT'ом. Просто выходим.
            return

        # Resolve config (WelcomeConfig takes priority, fall back to pair)
        if vr.welcome_config_id and vr.welcome_config:
            config = vr.welcome_config
        elif vr.pair_id and vr.pair:
            config = vr.pair
        else:
            vr.status = VerificationStatus.expired
            await session.commit()
            return

        bot: Bot | None = config.bot
        user_id = config.user_id

        # Check if config is still active
        is_pair = isinstance(config, ChannelGroupPair)
        if is_pair and not config.enabled:
            vr.status = VerificationStatus.expired
            await session.commit()
            return

        if not bot or not bot.is_active:
            vr.status = VerificationStatus.expired
            session.add(EventLog(
                user_id=user_id, bot_id=None, level=LogLevel.warning,
                message=f"Cannot kick {vr.user_name}: bot is inactive (config {config.id})",
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
            # 1. Кикаем пользователя, если verification_kick включён.
            # Иначе просто помечаем запрос как kicked-без-кика (юзер
            # сможет позже снова попробовать).
            if config.verification_kick:
                try:
                    await client.kick_member(chat_id=config.group_id, user_id=vr.max_user_id)
                    logger.info(
                        "Kicked %s (%s) from group %s (verification timeout)",
                        vr.user_name, vr.max_user_id, config.group_id,
                    )
                except MaxAPIError as exc:
                    logger.warning(
                        "Could not kick %s from group %s: %s",
                        vr.max_user_id, config.group_id, exc,
                    )

            # 2. Удаляем «приветственное» сообщение из группы — чтобы чат
            # не превращался в стену устаревших captcha-приглашений.
            if vr.group_message_id:
                try:
                    await client.delete_message(message_id=vr.group_message_id)
                except MaxAPIError as exc:
                    logger.warning(
                        "Could not delete verification message %s: %s",
                        vr.group_message_id, exc,
                    )

        session.add(EventLog(
            user_id=user_id,
            bot_id=bot.id,
            level=LogLevel.info,
            message=(
                f"Verification expired: {vr.user_name} ({vr.max_user_id}) "
                f"kicked from group {config.group_id} (config {config.id})"
            ),
        ))
        await session.commit()


async def main() -> None:
    await _ensure_schema()

    # Все джобы — UTC, чтобы не зависеть от TZ контейнера.
    # max_instances=1 защищает от перекрытия (если предыдущий запуск
    # не успел завершиться). coalesce=True склеивает пропущенные
    # тики после долгого зависания в один запуск.
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

    # Главный цикл просто спит — APScheduler работает в собственных
    # фоновых задачах. sleep(3600) минимизирует CPU usage idle-процесса.
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
