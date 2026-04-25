"""
JWT-аутентификация и работа с паролями.

Используется одновременно API (``Authorization: Bearer ...``) и UI
(cookie ``access_token``, выставляется при /login). Зависимости:

  • :func:`get_current_user` — для JSON-эндпоинтов, бросает 401;
  • :func:`get_current_user_ui` — для HTML-страниц, бросает 302 → /login.

Пароли хешируются bcrypt'ом через passlib (deprecated="auto" — passlib
сам пометит старые схемы при необходимости миграции).
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


# ── Пароли (bcrypt) ───────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Bcrypt-хеш пароля для хранения в users.hashed_password."""
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Сравнение plain-пароля с bcrypt-хешем (constant-time)."""
    return pwd_context.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Подписывает payload JWT'ом с настроенным секретом и алгоритмом.

    В data обязательно должен быть ключ ``sub`` (subject) — мы используем
    туда email пользователя. Срок жизни — из настроек, если не задан.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Декодирует JWT, проверяя подпись и срок. Бросает JWTError на ошибках."""
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])


# ── FastAPI-зависимости ───────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> User:
    """Извлекает текущего пользователя из заголовка или cookie.

    Приоритет: ``Authorization: Bearer <token>`` (для API-клиентов),
    затем cookie ``access_token`` (для UI-форм). Пустой/невалидный токен
    или удалённый/неактивный юзер → 401.
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
    """То же, что :func:`get_current_user`, но при ошибке шлёт 302 → /login.

    Для HTML-страниц 401-JSON выглядел бы странно — пользователь должен
    увидеть форму логина, а не голый JSON-ответ.
    """
    from fastapi.responses import RedirectResponse

    try:
        return await get_current_user(request, session)
    except HTTPException:
        # 302 с заголовком Location обработается в exception_handler
        # в web/main.py и превратится в нормальный редирект.
        response = RedirectResponse(url="/login", status_code=302)
        raise HTTPException(status_code=302, headers={"location": "/login"})
