"""
Общие FastAPI-зависимости (DI).

Здесь же — простой in-memory rate-limiter. Он намеренно «дёшев»: лимиты
сбрасываются при перезапуске процесса и не разделяются между worker'ами.
Для production-нагрузки нужен Redis-backed лимитер (sliding window /
token bucket), но для текущего масштаба этого достаточно.
"""
import time
from collections import defaultdict
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_async_session
from shared.config import get_settings
from web.auth import get_current_user

settings = get_settings()

# IP → список таймштампов запросов в окне 60 секунд.
# Не очищается явно — старые элементы фильтруются на каждом запросе.
# При большом числе уникальных IP может расти; в production — Redis.
_request_counts: dict[str, list[float]] = defaultdict(list)


def rate_limit(request: Request) -> None:
    """Лимит запросов на IP: ``RATE_LIMIT_PER_MINUTE`` в скользящем окне 60 сек.

    Внимание: при работе за реверс-прокси (nginx/Caddy) ``request.client.host``
    может вернуть IP прокси, а не клиента. Если это критично — нужно
    читать ``X-Forwarded-For`` (но валидировать TrustedHostMiddleware,
    чтобы не было spoofing'а).
    """
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = 60.0
    limit = settings.rate_limit_per_minute

    # Срезаем всё, что старше окна, и добавляем текущую метку.
    _request_counts[ip] = [t for t in _request_counts[ip] if now - t < window]
    _request_counts[ip].append(now)

    if len(_request_counts[ip]) > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: {limit} requests/minute",
        )


# Алиасы типов для использования в сигнатурах роутов — короче и читабельнее,
# чем полное ``session: AsyncSession = Depends(get_async_session)``.
DBSession = Annotated[AsyncSession, Depends(get_async_session)]
CurrentUser = Annotated[object, Depends(get_current_user)]
RateLimit = Annotated[None, Depends(rate_limit)]
