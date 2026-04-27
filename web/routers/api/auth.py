"""Auth эндпоинты: POST /login, POST /logout."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from db.models import User
from shared.config import get_settings
from web.auth import create_access_token, verify_password
from web.deps import DBSession, RateLimit

router = APIRouter()
settings = get_settings()


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, session: DBSession, _: RateLimit):
    """Логин по email+password. Возвращает JWT в JSON и одновременно ставит httpOnly-cookie.

    Cookie позволяет UI работать без явной передачи Bearer; JSON-токен
    нужен внешним API-клиентам. samesite=lax предотвращает простейший
    CSRF (но не заменяет полноценную CSRF-защиту форм, которой пока нет).
    """
    result = await session.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user.email})
    resp = JSONResponse({"access_token": token, "token_type": "bearer"})
    resp.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
    )
    return resp


@router.post("/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("access_token")
    return resp
