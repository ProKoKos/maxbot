"""
Jinja2 HTML page routes.
All pages (except /login) require cookie auth, redirect to /login on failure.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Bot, ChannelGroupPair, EventLog, PostStatus, ScheduledPost, User
from db.session import get_async_session
from shared.config import get_settings
from web.auth import create_access_token, verify_password
from web.deps import DBSession

router = APIRouter()
templates = Jinja2Templates(directory="/app/web/templates")
settings = get_settings()


def _get_user_from_cookie(request: Request) -> str | None:
    from jose import JWTError
    from web.auth import decode_token
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = decode_token(token)
        return payload.get("sub")
    except JWTError:
        return None


async def _require_user(request: Request, session: AsyncSession) -> User:
    email = _get_user_from_cookie(request)
    if not email:
        raise HTTPException(status_code=302, headers={"location": "/login"})
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=302, headers={"location": "/login"})
    return user


# ── Login / Logout ─────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, session: DBSession):
    form = await request.form()
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))

    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный email или пароль"},
            status_code=401,
        )

    token = create_access_token({"sub": user.email})
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        "access_token", token, httponly=True, samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
    )
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: DBSession):
    user = await _require_user(request, session)

    pairs_result = await session.execute(
        select(ChannelGroupPair)
        .where(ChannelGroupPair.user_id == user.id)
        .order_by(ChannelGroupPair.created_at.desc())
    )
    pairs = pairs_result.scalars().all()

    bots_result = await session.execute(
        select(Bot).where(Bot.user_id == user.id)
    )
    bots = bots_result.scalars().all()

    logs_result = await session.execute(
        select(EventLog)
        .where(EventLog.user_id == user.id)
        .order_by(EventLog.created_at.desc())
        .limit(5)
    )
    recent_logs = logs_result.scalars().all()

    # Build bot lookup for pairs display
    bot_map = {b.id: b for b in bots}

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "pairs": pairs,
            "bots": bots,
            "bot_map": bot_map,
            "recent_logs": recent_logs,
            "active_page": "dashboard",
        },
    )


# ── Bots ──────────────────────────────────────────────────────────────────────

@router.get("/bots", response_class=HTMLResponse)
async def bots_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    result = await session.execute(
        select(Bot)
        .where(Bot.user_id == user.id)
        .order_by(Bot.created_at.desc())
    )
    bots = result.scalars().all()

    return templates.TemplateResponse(
        "bots.html",
        {"request": request, "user": user, "bots": bots, "active_page": "bots"},
    )


# ── Pairs management ──────────────────────────────────────────────────────────

@router.get("/pairs", response_class=HTMLResponse)
async def pairs_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    pairs_result = await session.execute(
        select(ChannelGroupPair)
        .where(ChannelGroupPair.user_id == user.id)
        .order_by(ChannelGroupPair.created_at.desc())
    )
    pairs = pairs_result.scalars().all()

    bots_result = await session.execute(
        select(Bot)
        .where(Bot.user_id == user.id, Bot.is_active == True)  # noqa
        .order_by(Bot.name)
    )
    bots = bots_result.scalars().all()
    bot_map = {b.id: b for b in bots}

    return templates.TemplateResponse(
        "pairs.html",
        {
            "request": request,
            "user": user,
            "pairs": pairs,
            "bots": bots,
            "bot_map": bot_map,
            "active_page": "pairs",
        },
    )


# ── Event log ─────────────────────────────────────────────────────────────────

@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    result = await session.execute(
        select(EventLog)
        .where(EventLog.user_id == user.id)
        .order_by(EventLog.created_at.desc())
        .limit(50)
    )
    logs = result.scalars().all()

    return templates.TemplateResponse(
        "logs.html",
        {"request": request, "user": user, "logs": logs, "active_page": "logs"},
    )


# ── Autopost ──────────────────────────────────────────────────────────────────

@router.get("/autopost", response_class=HTMLResponse)
async def autopost_page(request: Request, session: DBSession):
    user = await _require_user(request, session)

    # Only show pairs that have a bot assigned
    pairs_result = await session.execute(
        select(ChannelGroupPair).where(
            ChannelGroupPair.user_id == user.id,
            ChannelGroupPair.enabled == True,  # noqa
            ChannelGroupPair.bot_id.isnot(None),
        )
    )
    pairs = pairs_result.scalars().all()

    posts_result = await session.execute(
        select(ScheduledPost)
        .where(ScheduledPost.user_id == user.id)
        .order_by(ScheduledPost.scheduled_at.desc())
        .limit(50)
    )
    posts = posts_result.scalars().all()

    return templates.TemplateResponse(
        "autopost.html",
        {
            "request": request,
            "user": user,
            "pairs": pairs,
            "posts": posts,
            "PostStatus": PostStatus,
            "active_page": "autopost",
        },
    )
