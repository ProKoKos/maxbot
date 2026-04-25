"""
Низкоуровневый асинхронный клиент MAX Bot API.

Документация API: https://dev.max.ru/docs-api

Используется как контекст-менеджер: ``async with MaxClient(token) as c: ...`` —
это гарантирует закрытие httpx.AsyncClient и освобождение TCP-соединений
из пула. На каждое long-polling-соединение поднимается свой клиент,
поэтому лимиты ниже — per-host (то есть на один токен/бота).
"""
import logging
from typing import Any

import httpx

from shared.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# MAX API имеет лимит 30 rps на токен. Лимиты httpx указаны per-host
# (один host = api.max.ru), но т.к. на каждого бота свой клиент,
# фактически 5 keep-alive хватает с запасом для одного бота.
_limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)


class MaxAPIError(Exception):
    """Ошибка обращения к MAX Bot API.

    Атрибуты:
        status — HTTP-код ответа (или код из JSON-обёртки MAX);
        body — сырое тело ответа, полезно для диагностики
            (например, ``{"code": "rate_limit_exceeded", ...}``).
    """
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Max API {status}: {body}")


class MaxClient:
    """Тонкая обёртка над MAX Bot API на базе httpx."""

    def __init__(self, token: str | None = None):
        # Fallback на settings.max_bot_token оставлен для обратной
        # совместимости со старой single-bot конфигурацией; в нынешней
        # multi-bot архитектуре токен всегда передаётся явно.
        self.token = token or settings.max_bot_token
        self._base = settings.max_api_base
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "MaxClient":
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": self.token},
            # 30 сек > MAX long-poll timeout (25 сек) — гарантирует, что
            # сначала отработает таймаут MAX, а не httpx.
            timeout=30.0,
            limits=_limits,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ── Внутренняя логика ────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Унифицированный запрос: разворачивает обёртку MAX и поднимает MaxAPIError."""
        assert self._client, "Use MaxClient as an async context manager"
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise MaxAPIError(resp.status_code, resp.text)
        data = resp.json()
        # MAX иногда возвращает 200 OK, но с code != "ok" в теле —
        # это тоже ошибка, и её нужно поднять как исключение.
        if isinstance(data, dict) and data.get("code") and data["code"] != "ok":
            raise MaxAPIError(resp.status_code, str(data))
        return data

    # ── Информация о боте ────────────────────────────────────────────────────

    async def get_me(self) -> dict:
        """Возвращает профиль бота: user_id, username, name. Используется
        как «health check» токена при первом подключении."""
        return await self._request("GET", "/me")

    # ── Long-polling апдейтов ────────────────────────────────────────────────

    async def get_updates(self, marker: int | None = None, timeout: int = 25) -> dict:
        """Long-poll: ждёт новых событий до timeout секунд.

        ``marker`` — непрозрачный курсор, возвращённый предыдущим вызовом;
        начинать с None (или 0). MAX гарантирует, что один и тот же
        update не вернётся, если мы передаём актуальный marker.
        Максимальный timeout по докам — 60 сек.
        """
        params: dict[str, Any] = {"timeout": timeout}
        if marker:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params)

    # ── Webhook (альтернатива polling) ───────────────────────────────────────

    async def subscribe_webhook(self, url: str, secret: str | None = None) -> dict:
        payload: dict[str, Any] = {"url": url}
        if secret:
            payload["secret"] = secret
        return await self._request("POST", "/subscriptions", json=payload)

    async def unsubscribe_webhook(self) -> dict:
        return await self._request("DELETE", "/subscriptions")

    # ── Сообщения ────────────────────────────────────────────────────────────

    async def send_message(
        self,
        chat_id: str,
        text: str,
        attachments: list[dict] | None = None,
        notify: bool = True,
        format: str = "markdown",
    ) -> dict:
        payload: dict[str, Any] = {
            "text": text,
            "notify": notify,
            "format": format,
        }
        if attachments:
            payload["attachments"] = attachments
        return await self._request(
            "POST", "/messages", params={"chat_id": chat_id}, json=payload
        )

    async def edit_message(
        self,
        message_id: str,
        text: str,
        attachments: list[dict] | None = None,
    ) -> dict:
        """Редактирует сообщение/пост в канале. Бот должен быть админом."""
        payload: dict[str, Any] = {"text": text}
        if attachments:
            payload["attachments"] = attachments
        return await self._request("PUT", "/messages", params={"message_id": message_id}, json=payload)

    async def delete_message(self, message_id: str) -> None:
        """Удаляет сообщение. Бот должен быть админом в чате."""
        # try/except здесь по-сути no-op (мы просто перевыкидываем) —
        # оставлено как точка для будущего instrumentation/метрик.
        try:
            await self._request("DELETE", "/messages", params={"message_id": message_id})
        except MaxAPIError as exc:
            raise exc

    async def reply_to_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        attachments: list[dict] | None = None,
    ) -> dict:
        """Отправляет reply на сообщение. Используется как fallback,
        когда edit_message запрещён (например, истёк лимит редактирования)."""
        payload: dict[str, Any] = {
            "text": text or "💬",
            "link": {"type": "reply", "mid": message_id},
        }
        if attachments:
            payload["attachments"] = attachments
        return await self._request(
            "POST", "/messages", params={"chat_id": chat_id}, json=payload
        )

    # ── Чаты ─────────────────────────────────────────────────────────────────

    async def get_chat(self, chat_id: str) -> dict:
        """Информация о конкретном чате (название, иконка, тип, ссылка)."""
        return await self._request("GET", f"/chats/{chat_id}")

    async def get_chats(self) -> dict:
        """Список всех чатов, в которых состоит бот. Используется в UI
        для построения выпадающих списков каналов и групп."""
        return await self._request("GET", "/chats")

    # ── Пользователи ─────────────────────────────────────────────────────────

    async def get_user(self, user_id: str) -> dict:
        """Профиль пользователя по user_id (для аватарок в инбоксе)."""
        return await self._request("GET", f"/users/{user_id}")

    # ── Управление участниками ──────────────────────────────────────────────

    async def kick_member(self, chat_id: str, user_id: str) -> dict:
        """Исключает участника из группы. Бот должен быть админом."""
        return await self._request(
            "DELETE", f"/chats/{chat_id}/members",
            params={"user_id": user_id},
        )

    async def send_action(self, chat_id: str, action: str = "typing_on") -> None:
        """Отправляет chat-action (например, индикатор «печатает»).

        Ошибки глотаются: индикатор — украшение, не критично, если
        MAX его не показал. Не должен блокировать отправку самого ответа.
        """
        try:
            await self._request("POST", "/chats/actions", params={"chat_id": chat_id}, json={"action": action})
        except MaxAPIError:
            pass
