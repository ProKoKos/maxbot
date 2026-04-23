"""
BotSupervisor — dynamic multi-bot polling manager.

Every REFRESH_INTERVAL seconds it queries the DB for active bots and:
  - Starts a new polling task for bots that appeared.
  - Cancels the task for bots that were deactivated or deleted.
  - Restarts tasks that crashed (with exponential backoff).

Each bot runs an independent asyncio Task with its own MaxClient and
PollingMarker, so they don't share state or affect each other.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update

from bot.client import MaxAPIError, MaxClient
from bot.crypto import decrypt_token
from bot.handlers import handle_update
from db.models import Bot, PollingMarker
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

REFRESH_INTERVAL = 30   # seconds between DB polls for new/removed bots
POLL_TIMEOUT = 25       # Max API long-poll hang duration
RETRY_BASE = 5          # base seconds for backoff on error
RETRY_MAX = 300         # cap at 5 minutes


@dataclass
class ManagedBot:
    bot_id: int
    user_id: int
    token: str                        # decrypted plain token
    task: asyncio.Task | None = None
    retry_count: int = 0
    last_error: str = ""


class BotSupervisor:
    """Manages a pool of per-bot polling tasks, synced with the DB."""

    def __init__(self) -> None:
        self._bots: dict[int, ManagedBot] = {}   # bot_id → ManagedBot

    # ── Public API ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main supervisor loop. Runs forever."""
        logger.info("Supervisor started — refresh interval: %ds", REFRESH_INTERVAL)
        while True:
            try:
                await self._refresh()
            except Exception as exc:
                logger.exception("Supervisor refresh error: %s", exc)
            await asyncio.sleep(REFRESH_INTERVAL)

    # ── Internals ──────────────────────────────────────────────────────────────

    async def _refresh(self) -> None:
        """Sync running tasks with active bots in DB."""
        active_bots = await self._fetch_active_bots()
        active_ids = {b["id"] for b in active_bots}
        running_ids = set(self._bots.keys())

        # ── Start new bots ────────────────────────────────────────────────────
        for bot_row in active_bots:
            bot_id = bot_row["id"]
            if bot_id not in running_ids:
                await self._start_bot(bot_row)

        # ── Stop removed/deactivated bots ─────────────────────────────────────
        for bot_id in running_ids - active_ids:
            await self._stop_bot(bot_id)

        # ── Restart crashed tasks ─────────────────────────────────────────────
        for bot_id, managed in list(self._bots.items()):
            if managed.task and managed.task.done():
                exc = managed.task.exception() if not managed.task.cancelled() else None
                if exc:
                    managed.last_error = str(exc)
                    managed.retry_count += 1
                    delay = min(RETRY_BASE * (2 ** (managed.retry_count - 1)), RETRY_MAX)
                    logger.warning(
                        "Bot %d task crashed (attempt %d), restarting in %ds: %s",
                        bot_id, managed.retry_count, delay, exc,
                    )
                    await asyncio.sleep(delay)
                # Re-fetch token (might have changed)
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

    # ── Per-bot polling loop ───────────────────────────────────────────────────

    async def _poll_loop(self, managed: ManagedBot) -> None:
        """
        Long-polling loop for a single bot.
        Runs until cancelled (supervisor stops it) or raises (supervisor restarts it).
        """
        bot_id = managed.bot_id
        logger.info("Bot %d: polling loop started", bot_id)

        async with MaxClient(token=managed.token) as client:
            # Verify token on first connect
            try:
                me = await client.get_me()
                logger.info(
                    "Bot %d authenticated: %s (max_id=%s)",
                    bot_id, me.get("name"), me.get("user_id"),
                )
                # Update cached Max identity in DB
                await self._update_bot_identity(bot_id, me)
                managed.retry_count = 0  # reset on successful auth
            except MaxAPIError as exc:
                logger.error("Bot %d: auth failed — %s. Task will not retry bad tokens.", bot_id, exc)
                # Mark bot as inactive so supervisor stops retrying
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
                    raise  # propagate cancellation
                except MaxAPIError as exc:
                    logger.error("Bot %d: get_updates error — %s", bot_id, exc)
                    await asyncio.sleep(RETRY_BASE)
                    continue

                updates = data.get("updates", [])
                new_marker = data.get("marker")

                # TEMP DEBUG: always log the response shape
                logger.info(
                    "Bot %d /updates response keys=%s updates_count=%d marker=%s",
                    bot_id, list(data.keys()), len(updates), new_marker,
                )

                if updates:
                    for upd in updates:
                        # TEMP DEBUG: log every raw update so we can identify event types
                        logger.info(
                            "Bot %d RAW update: type=%r keys=%s full=%s",
                            bot_id,
                            upd.get("update_type") or upd.get("type"),
                            list(upd.keys()),
                            upd,
                        )
                    async with AsyncSessionLocal() as session:
                        for upd in updates:
                            try:
                                await handle_update(upd, session, client, bot_id=bot_id)
                            except Exception as exc:
                                logger.exception(
                                    "Bot %d: error handling update %s: %s", bot_id, upd, exc
                                )

                if new_marker and new_marker != marker:
                    await self._save_marker(bot_id, new_marker)

    # ── DB helpers ─────────────────────────────────────────────────────────────

    async def _fetch_active_bots(self) -> list[dict]:
        """Return list of {id, user_id, name, token (decrypted)} for all active bots."""
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
        """Disable a bot in DB so the supervisor won't keep retrying it."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Bot).where(Bot.id == bot_id))
            bot = result.scalar_one_or_none()
            if bot:
                bot.is_active = False
                await session.commit()
        # Remove from local registry
        self._bots.pop(bot_id, None)
        logger.warning("Bot %d deactivated in DB: %s", bot_id, reason)
