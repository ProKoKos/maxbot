import httpx
from shared.config import get_settings


async def chat(model: str, messages: list[dict], timeout: int = 60) -> str:
    """Send a chat request to Groq and return the assistant's reply."""
    api_key = get_settings().groq_api_key
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
