"""
JWT authentication utilities.
- create_access_token / verify_token for API
- get_current_user dependency for FastAPI routes
- password hashing with bcrypt
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Cookie, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from db.session import get_async_session
from shared.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Passwords ─────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])


# ── FastAPI dependencies ───────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> User:
    """
    Extracts user from:
    1. Authorization: Bearer <token> header  (API clients)
    2. access_token cookie                   (browser UI)
    """
    token: str | None = None

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    if not token:
        token = request.cookies.get("access_token")

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not token:
        raise credentials_exception

    try:
        payload = decode_token(token)
        email: str = payload.get("sub", "")
        if not email:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise credentials_exception

    return user


async def get_current_user_ui(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> User:
    """Same as get_current_user but redirects to login page instead of 401."""
    from fastapi.responses import RedirectResponse

    try:
        return await get_current_user(request, session)
    except HTTPException:
        # Store intended URL in cookie so login can redirect back
        response = RedirectResponse(url="/login", status_code=302)
        raise HTTPException(status_code=302, headers={"location": "/login"})
