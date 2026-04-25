import httpx
from shared.config import get_settings

_DEFAULT_URL = "https://openrouter.ai/api/v1"


async def chat(
    model: str,
    messages: list[dict],
    timeout: int = 60,
    api_url: str = "",
    api_key: str = "",
) -> str:
    """Send a chat request to an OpenAI-compatible API and return the assistant's reply."""
    base_url = (api_url or _DEFAULT_URL).rstrip("/")
    key = api_key or get_settings().openrouter_api_key
    if not key:
        raise RuntimeError("API key is not configured")

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if "openrouter.ai" in base_url:
        headers["HTTP-Referer"] = "https://maxbot.teplobyte.ru"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={"model": model, "messages": messages},
            timeout=timeout,
        )
        if resp.is_error:
            raise RuntimeError(f"API {resp.status_code}: {resp.text}")
        return resp.json()["choices"][0]["message"]["content"]
