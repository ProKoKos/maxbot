"""
Клиент OpenAI-совместимого Chat Completions API.

Подходит для всех провайдеров, реализующих OpenAI-протокол
(``/chat/completions``):

  • OpenRouter (по умолчанию, см. ``_DEFAULT_URL``);
  • локальный Ollama c openai-совместимым прокси;
  • официальный OpenAI;
  • Together, Groq, Anthropic-через-прокси и т.д.

Запросы делаются на каждый ответ AI отдельным httpx.AsyncClient'ом —
для bot-сценария частота низкая, поэтому переиспользование клиента
не даёт ощутимого выигрыша, зато упрощает работу с разными api_url
для разных AssistantConfig'ов.
"""
import httpx

# OpenRouter — наш дефолтный провайдер: единый endpoint к десяткам моделей
# и удобная биллинг-модель. Перекрывается через AssistantConfig.api_url.
_DEFAULT_URL = "https://openrouter.ai/api/v1"


async def chat(
    model: str,
    messages: list[dict],
    timeout: int = 60,
    api_url: str = "",
    api_key: str = "",
) -> str:
    """Отправляет chat-запрос и возвращает текст ответа ассистента.

    :param messages: список ``{"role": "...", "content": "..."}`` в формате
        OpenAI Chat Completions.
    :param timeout: таймаут запроса в секундах. 60 сек — компромисс
        между «дать модели подумать» и «не подвиснуть навсегда»;
        OpenRouter обычно укладывается в 10–20 сек.
    :returns: содержимое поля ``choices[0].message.content``.
    :raises RuntimeError: при отсутствии api_key или ошибке HTTP/API.
    """
    if not api_key:
        raise RuntimeError("API ключ не настроен для этого ассистента")

    base_url = (api_url or _DEFAULT_URL).rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # OpenRouter требует HTTP-Referer, иначе помечает запросы как анонимные
    # и применяет более жёсткие лимиты. Для остальных провайдеров заголовок
    # просто игнорируется.
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
            # Не глотаем тело — оно содержит причину (rate limit, invalid model и т.д.).
            raise RuntimeError(f"API {resp.status_code}: {resp.text}")
        return resp.json()["choices"][0]["message"]["content"]
