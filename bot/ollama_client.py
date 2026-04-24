import httpx
from shared.config import get_settings


async def chat(model: str, messages: list[dict], timeout: int = 60) -> str:
    """Send a chat request to OpenRouter and return the assistant's reply."""
    api_key = get_settings().openrouter_api_key
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://maxbot.teplobyte.ru",
            },
            json={"model": model, "messages": messages},
            timeout=timeout,
        )
        if resp.is_error:
            raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text}")
        return resp.json()["choices"][0]["message"]["content"]
