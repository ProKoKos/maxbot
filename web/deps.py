"""
Shared FastAPI dependencies.
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

# ── Simple in-process rate limiter ────────────────────────────────────────────
# For production, replace with Redis-backed sliding window.

_request_counts: dict[str, list[float]] = defaultdict(list)


def rate_limit(request: Request) -> None:
    """
    Middleware-style dependency: limits each IP to RATE_LIMIT_PER_MINUTE.
    Uses an in-memory sliding window — resets on process restart.
    """
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = 60.0
    limit = settings.rate_limit_per_minute

    # Evict entries older than 1 minute
    _request_counts[ip] = [t for t in _request_counts[ip] if now - t < window]
    _request_counts[ip].append(now)

    if len(_request_counts[ip]) > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: {limit} requests/minute",
        )


# Type aliases for use in route signatures
DBSession = Annotated[AsyncSession, Depends(get_async_session)]
CurrentUser = Annotated[object, Depends(get_current_user)]
RateLimit = Annotated[None, Depends(rate_limit)]
