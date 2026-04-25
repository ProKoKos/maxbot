"""
BotSupervisor — динамический менеджер polling-задач для нескольких ботов.

Каждые ``REFRESH_INTERVAL`` секунд ходит в БД и сверяет «активные боты»
с текущим набором запущенных asyncio-задач:

  • появился новый бот / включили существующий → создаём asyncio.Task
    с отдельным MaxClient и PollingMarker;
  • бот выключен / удалён → отменяем его задачу через task.cancel();
  • задача упала с исключением → перезапускаем с exponential backoff
    (5 → 10 → 20 → … → 300 сек), сбрасывая счётчик при удачной авторизации.

Изоляция: каждая задача держит отдельный httpx-клиент и работает с
собственным маркером long-polling. Один сбойный бот не повлияет
на остальных.

Если /me возвращает ошибку (например, токен отозвали в MAX), бот
автоматически помечается ``is_active=False`` через
:meth:`_deactivate_bot` — иначе supervisor бесконечно пытался бы его
переподнять.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update

from bot.client import MaxAPIError, MaxClient
from bot.constants import (
    SUPERVISOR_POLL_TIMEOUT as POLL_TIMEOUT,
    SUPERVISOR_REFRESH_INTERVAL as REFRESH_INTERVAL,
    SUPERVISOR_RETRY_BASE as RETRY_BASE,
    SUPERVISOR_RETRY_MAX as RETRY_MAX,
)
from bot.crypto import decrypt_token
from bot.handlers import handle_update
from db.models import Bot, PollingMarker
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


@dataclass
class ManagedBot:
    """Состояние одного бота, обслуживаемого супервайзером."""
    bot_id: int
    user_id: int
    # Расшифрованный (plain) токен — держим в памяти, чтобы не дешифровать
    # на каждой итерации long-polling.
    token: str
    task: asyncio.Task | None = None
    # Счётчик подряд идущих неудач для exponential backoff.
    # Сбрасывается в 0 при удачной авторизации /me.
    retry_count: int = 0
    last_error: str = ""


class BotSupervisor:
    """Пул polling-задач, синхронизируемый с БД."""

    def __init__(self) -> None:
        # bot_id → ManagedBot. Источник истины «что у нас сейчас крутится».
        self._bots: dict[int, ManagedBot] = {}

    # ── Публичный API ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Бесконечный цикл супервайзера. Вызывается из bot/main.py."""
        logger.info("Supervisor started — refresh interval: %ds", REFRESH_INTERVAL)
        while True:
            try:
                await self._refresh()
            except Exception as exc:
                # Ошибка в _refresh не должна валить весь сервис —
                # просто залогируем и пойдём на следующую итерацию.
                logger.exception("Supervisor refresh error: %s", exc)
            await asyncio.sleep(REFRESH_INTERVAL)

    # ── Внутренняя логика ─────────────────────────────────────────────────────

    async def _refresh(self) -> None:
        """Синхронизирует запущенные задачи со списком активных ботов в БД."""
        active_bots = await self._fetch_active_bots()
        active_ids = {b["id"] for b in active_bots}
        running_ids = set(self._bots.keys())

        # 1. Стартуем новые: появились в БД, но ещё не запущены.
        for bot_row in active_bots:
            bot_id = bot_row["id"]
            if bot_id not in running_ids:
                await self._start_bot(bot_row)

        # 2. Гасим ушедшие: задача крутится, но в БД бот уже не активен.
        for bot_id in running_ids - active_ids:
            await self._stop_bot(bot_id)

        # 3. Перезапускаем упавшие: задача завершилась с исключением.
        # Cancellation идёт мимо этой ветки (task.cancelled() == True).
        for bot_id, managed in list(self._bots.items()):
            if managed.task and managed.task.done():
                exc = managed.task.exception() if not managed.task.cancelled() else None
                if exc:
                    managed.last_error = str(exc)
                    managed.retry_count += 1
                    # Exponential backoff: 5, 10, 20, 40 … но не дольше RETRY_MAX.
                    # Спим внутри _refresh, чтобы блокировать только один бот,
                    # а не весь supervisor (это компромисс — простота важнее).
                    delay = min(RETRY_BASE * (2 ** (managed.retry_count - 1)), RETRY_MAX)
                    logger.warning(
                        "Bot %d task crashed (attempt %d), restarting in %ds: %s",
                        bot_id, managed.retry_count, delay, exc,
                    )
                    await asyncio.sleep(delay)
                # Токен мог поменяться (юзер обновил его через UI), пока
                # задача падала и перезапускалась — берём актуальный.
                bot_rows = [b for b in active_bots if b["id"] == bot_id]
                if bot_rows:
                    managed.token = bot_rows[0]["token"]
                managed.task = asyncio.create_task(
                    self._poll_loop(managed),
                    name=f"poll-bot-{bot_id}",
                )

    async def _start_bot(self, bot_row: dict) -> None:
        managed = ManagedBot(
            bot_id=bot_row["id"],
            user_id=bot_row["user_id"],
            token=bot_row["token"],
        )
        managed.task = asyncio.create_task(
            self._poll_loop(managed),
            name=f"poll-bot-{managed.bot_id}",
        )
        self._bots[managed.bot_id] = managed
        logger.info("Started polling task for bot %d (%s)", managed.bot_id, bot_row.get("name"))

    async def _stop_bot(self, bot_id: int) -> None:
        managed = self._bots.pop(bot_id, None)
        if managed and managed.task and not managed.task.done():
            managed.task.cancel()
            try:
                await managed.task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped polling task for bot %d", bot_id)

    # ── Long-polling-цикл одного бота ────────────────────────────────────────

    async def _poll_loop(self, managed: ManagedBot) -> None:
        """Бесконечный long-polling-цикл одного бота.

        Завершается при cancel'е (тогда supervisor его не перезапускает)
        или при необработанном исключении (тогда supervisor увидит
        task.done() с exception и перезапустит с backoff).
        """
        bot_id = managed.bot_id
        logger.info("Bot %d: polling loop started", bot_id)

        async with MaxClient(token=managed.token) as client:
            # Первое действие — /me. Это и проверка валидности токена,
            # и обновление кеша max_user_id/max_username (нужны для
            # deep-link'ов и проверки «бот не верифицирует сам себя»).
            try:
                me = await client.get_me()
                logger.info(
                    "Bot %d authenticated: %s (max_id=%s)",
                    bot_id, me.get("name"), me.get("user_id"),
                )
                await self._update_bot_identity(bot_id, me)
                # Авторизация удалась — сбрасываем backoff, чтобы при
                # следующем падении мы снова начинали с RETRY_BASE.
                managed.retry_count = 0
            except MaxAPIError as exc:
                # Битый/отозванный токен. Бесконечно перезапускать
                # такого бота — пустая трата ресурсов: помечаем
                # is_active=False, юзер увидит ошибку в UI.
                logger.error("Bot %d: auth failed — %s. Task will not retry bad tokens.", bot_id, exc)
                await self._deactivate_bot(bot_id, reason=str(exc))
                return

            while True:
                marker = await self._get_marker(bot_id)
                try:
                    data = await client.get_updates(
                        marker=marker if marker else None,
                        timeout=POLL_TIMEOUT,
                    )
                except asyncio.CancelledError:
                    # Cancel — сигнал «остановись», не обрабатываем как ошибку.
                    raise
                except MaxAPIError as exc:
                    # Сетевые/API-ошибки: ждём RETRY_BASE и продолжаем.
                    # Не выходим из цикла — иначе заработает supervisor backoff
                    # и мы потеряем уже валидную авторизацию /me.
                    logger.error("Bot %d: get_updates error — %s", bot_id, exc)
                    await asyncio.sleep(RETRY_BASE)
                    continue

                updates = data.get("updates", [])
                new_marker = data.get("marker")

                # Один общий session на пакет апдейтов: handlers внутри
                # сами решают, когда коммитить. Если упадёт один update,
                # logger.exception зафиксирует, остальные — продолжатся.
                if updates:
                    async with AsyncSessionLocal() as session:
                        for upd in updates:
                            try:
                                await handle_update(upd, session, client, bot_id=bot_id)
                            except Exception as exc:
                                logger.exception(
                                    "Bot %d: error handling update %s: %s", bot_id, upd, exc
                                )

                # Маркер = курсор в стриме событий MAX. Сохраняем только
                # если изменился — иначе лишние UPDATE.
                if new_marker and new_marker != marker:
                    await self._save_marker(bot_id, new_marker)

    # ── Помощники работы с БД ────────────────────────────────────────────────

    async def _fetch_active_bots(self) -> list[dict]:
        """Возвращает список активных ботов с уже расшифрованными токенами.

        Боты с битым encrypted_token (например, после смены ENCRYPTION_KEY)
        пропускаем — иначе supervisor бы упорно перезапускал безнадёжный
        бот в ошибку. Юзер должен пересохранить токен через UI.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Bot).where(Bot.is_active == True)  # noqa: E712
            )
            bots = result.scalars().all()

        out = []
        for bot in bots:
            try:
                token = decrypt_token(bot.encrypted_token)
            except ValueError as exc:
                logger.error("Bot %d: cannot decrypt token — %s. Skipping.", bot.id, exc)
                continue
            out.append({
                "id": bot.id,
                "user_id": bot.user_id,
                "name": bot.name,
                "token": token,
            })
        return out

    async def _get_marker(self, bot_id: int) -> int:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PollingMarker).where(PollingMarker.bot_id == bot_id)
            )
            row = result.scalar_one_or_none()
            if not row:
                row = PollingMarker(bot_id=bot_id, marker=0)
                session.add(row)
                await session.commit()
            return row.marker

    async def _save_marker(self, bot_id: int, marker: int) -> None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PollingMarker).where(PollingMarker.bot_id == bot_id)
            )
            row = result.scalar_one_or_none()
            if row:
                row.marker = marker
            else:
                session.add(PollingMarker(bot_id=bot_id, marker=marker))
            await session.commit()

    async def _update_bot_identity(self, bot_id: int, me: dict) -> None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Bot).where(Bot.id == bot_id))
            bot = result.scalar_one_or_none()
            if bot:
                bot.max_user_id = str(me.get("user_id", ""))
                bot.max_username = me.get("username") or me.get("name", "")
                await session.commit()

    async def _deactivate_bot(self, bot_id: int, reason: str) -> None:
        """Помечает бота неактивным в БД, чтобы supervisor больше его не запускал."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Bot).where(Bot.id == bot_id))
            bot = result.scalar_one_or_none()
            if bot:
                bot.is_active = False
                await session.commit()
        # Remove from local registry
        self._bots.pop(bot_id, None)
        logger.warning("Bot %d deactivated in DB: %s", bot_id, reason)
