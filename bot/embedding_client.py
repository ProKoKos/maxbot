"""Клиент для получения векторных эмбеддингов через OpenAI-совместимый API."""
import httpx


async def get_embedding(text: str, model: str, api_url: str, api_key: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
