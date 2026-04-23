"""
Low-level async client for Max Bot API.
Docs: https://dev.max.ru/docs-api
"""
import logging
from typing import Any

import httpx

from shared.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Max API enforces 30 rps; httpx limits are per-host.
_limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)


class MaxAPIError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Max API {status}: {body}")


class MaxClient:
    """Thin async wrapper around Max Bot API."""

    def __init__(self, token: str | None = None):
        self.token = token or settings.max_bot_token
        self._base = settings.max_api_base
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "MaxClient":
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": self.token},
            timeout=30.0,
            limits=_limits,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert self._client, "Use MaxClient as an async context manager"
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise MaxAPIError(resp.status_code, resp.text)
        data = resp.json()
        # Max API wraps responses; surface errors from body
        if isinstance(data, dict) and data.get("code") and data["code"] != "ok":
            raise MaxAPIError(resp.status_code, str(data))
        return data

    # ── Bot info ──────────────────────────────────────────────────────────────

    async def get_me(self) -> dict:
        return await self._request("GET", "/me")

    # ── Updates (long-polling) ────────────────────────────────────────────────

    async def get_updates(self, marker: int | None = None, timeout: int = 25) -> dict:
        """
        Long-poll for new events.
        marker — opaque cursor returned by previous call.
        timeout — server-side wait in seconds (max 60).
        """
        params: dict[str, Any] = {"timeout": timeout}
        if marker:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params)

    # ── Webhook ───────────────────────────────────────────────────────────────

    async def subscribe_webhook(self, url: str, secret: str | None = None) -> dict:
        payload: dict[str, Any] = {"url": url}
        if secret:
            payload["secret"] = secret
        return await self._request("POST", "/subscriptions", json=payload)

    async def unsubscribe_webhook(self) -> dict:
        return await self._request("DELETE", "/subscriptions")

    # ── Messages ──────────────────────────────────────────────────────────────

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
        """Edit a channel post (bot must be admin)."""
        payload: dict[str, Any] = {"text": text}
        if attachments:
            payload["attachments"] = attachments
        return await self._request("PUT", "/messages", params={"message_id": message_id}, json=payload)

    async def reply_to_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        attachments: list[dict] | None = None,
    ) -> dict:
        """Reply to a specific message (used as fallback when edit is not allowed)."""
        payload: dict[str, Any] = {
            "text": text or "💬",
            "link": {"type": "reply", "mid": message_id},
        }
        if attachments:
            payload["attachments"] = attachments
        return await self._request(
            "POST", "/messages", params={"chat_id": chat_id}, json=payload
        )

    # ── Chats ─────────────────────────────────────────────────────────────────

    async def get_chat(self, chat_id: str) -> dict:
        return await self._request("GET", f"/chats/{chat_id}")

    async def get_chats(self) -> dict:
        return await self._request("GET", "/chats")

    # ── Members ───────────────────────────────────────────────────────────────

    async def kick_member(self, chat_id: str, user_id: str) -> dict:
        """Remove (kick) a member from a group chat."""
        return await self._request(
            "DELETE", f"/chats/{chat_id}/members",
            params={"user_id": user_id},
        )

    async def send_action(self, chat_id: str, action: str = "typing_on") -> None:
        """Send a chat action (e.g. typing indicator). Errors are swallowed."""
        try:
            await self._request("POST", "/chats/actions", params={"chat_id": chat_id}, json={"action": action})
        except MaxAPIError:
            pass
