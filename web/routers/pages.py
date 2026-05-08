"""
HTML-страницы (Jinja2).

Все страницы, кроме ``/login``, требуют cookie-авторизации. Если cookie
отсутствует или JWT битый — :func:`_require_user` бросает HTTPException(302)
и пользователя редиректит на ``/login`` (см. handler в web/main.py).

Placeholder-страницы (coming soon) вынесены в отдельный модуль
``web.routers.pages_coming_soon`` — там же хранятся их тексты и
маршруты регистрируются динамически.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crypto import decrypt_token
from db.models import AssistantConfig, Bot, ChannelGroupPair, EventLog, PostStatus, ScheduledPost, User, WelcomeConfig
from db.session import get_async_session
from shared.config import get_settings
from web.auth import create_access_token, verify_password
from web.deps import DBSession

router = APIRouter()
templates = Jinja2Templates(directory="/app/web/templates")
settings = get_settings()


def _get_user_from_cookie(request: Request) -> str | None:
    """Email из JWT-cookie, либо None если cookie нет / JWT битый."""
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
    """Достаёт пользователя по cookie, иначе бросает 302 → /login.

    Используется как ручная замена Depends (чтобы не плодить кучу
    идентичных параметров в каждой странице). 302 ловится глобальным
    exception_handler'ом и превращается в RedirectResponse.
    """
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
    """Обработка отправки формы логина.

    На неверные креденшелы возвращаем 401 с тем же шаблоном (PRG не делаем —
    форма короткая, redirect-после-POST не критичен).

    ⚠️ CSRF-токен здесь не проверяется — для текущего масштаба считаем
    риск приемлемым (samesite=lax cookie + ручной POST формы).
    """
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


# ── Главная (дашборд) ─────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: DBSession):
    """Главная страница: пары + список ботов + последние 5 событий.

    bot_map нужна шаблону, чтобы быстро показать имя бота для каждой
    пары без N+1 в Jinja (вместо ленивых ORM-обращений).
    """
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


# ── Инбокс конкретного бота ───────────────────────────────────────────────────
# Сама страница только рендерит шаблон — данные подгружает фронтенд через
# /api/bots/{bot_id}/inbox/* (см. web/routers/api.py).

@router.get("/bots/{bot_id}/inbox", response_class=HTMLResponse)
async def bot_inbox_page(bot_id: int, request: Request, session: DBSession):
    user = await _require_user(request, session)

    result = await session.execute(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == user.id)
    )
    bot = result.scalar_one_or_none()
    if not bot:
        raise HTTPException(404, "Bot not found")

    return templates.TemplateResponse(
        "inbox.html",
        {"request": request, "user": user, "bot": bot},
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
            "active_page": "discussions",
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


# ── Хелпер для placeholder-страниц ────────────────────────────────────────────
# Используется в web.routers.pages_coming_soon — маршруты coming-soon
# регистрируются там динамически из списка COMING_SOON_PAGES.

def _coming_soon(request: Request, user, active_page: str, title: str, icon: str,
                 description: str, features: list[dict]):
    """Рендерит шаблон ``coming_soon.html`` с переданными метаданными фичи."""
    return templates.TemplateResponse(
        "coming_soon.html",
        {
            "request": request,
            "user": user,
            "active_page": active_page,
            "title": title,
            "icon": icon,
            "description": description,
            "features": features,
        },
    )


# ── Раздел «Аудитория» (verification, AI и т.д.) ─────────────────────────────

# Дефолтные шаблоны для placeholder'ов в формах настройки верификации.
# Намеренно дублируются (без плейсхолдеров **bold**) с константами в
# bot/constants.py — здесь мы показываем их пользователю как пример,
# а не как реальный шаблон. Сохранение пустого поля в БД означает «использовать
# DEFAULT_VERIFY_MSG из bot/constants.py» (см. handlers).
_DEFAULT_VERIFY_MSG = (
    "👋 Привет, {имя}!\n\n"
    "Добро пожаловать в {группа}. Чтобы получить доступ к чату, подтвердите, "
    "что вы не бот — нажмите кнопку ниже.\n\n"
    "⏰ Время на верификацию: {минут} мин."
)
_DEFAULT_VERIFY_BTN = "✅ Я не бот"
_DEFAULT_WELCOME_DM = "✅ Верификация пройдена! Добро пожаловать в {группа}.\n\nНажмите кнопку ниже, чтобы вернуться в чат."


@router.get("/welcome", response_class=HTMLResponse)
async def welcome_page(request: Request, session: DBSession):
    """Страница управления WelcomeConfig'ами (standalone-верификация).

    Конфиги сериализуются на сервере в configs_data, чтобы фронт мог
    встроить их JSON'ом в шаблон без дополнительного fetch'а — это
    ускоряет первую отрисовку.
    """
    user = await _require_user(request, session)

    configs_result = await session.execute(
        select(WelcomeConfig)
        .where(WelcomeConfig.user_id == user.id)
        .order_by(WelcomeConfig.created_at.desc())
    )
    configs = configs_result.scalars().all()

    bots_result = await session.execute(
        select(Bot).where(Bot.user_id == user.id)
    )
    bots = bots_result.scalars().all()
    bot_map = {b.id: b for b in bots}

    # Serialize configs to dicts for JSON embedding in the template
    configs_data = [
        {
            "id": c.id,
            "group_name": c.group_name or "",
            "group_id": c.group_id,
            "group_link": c.group_link or "",
            "bot_id": c.bot_id,
            "bot_name": (
                bot_map[c.bot_id].name or ""
                if c.bot_id and c.bot_id in bot_map
                else ""
            ),
            "bot_username": (
                f"@{bot_map[c.bot_id].max_username}"
                if c.bot_id and c.bot_id in bot_map and bot_map[c.bot_id].max_username
                else ""
            ),
            "verification_enabled": c.verification_enabled,
            "verification_timeout_min": c.verification_timeout_min,
            "verification_message": c.verification_message or "",
            "verification_button_text": c.verification_button_text or "",
            "verification_kick": c.verification_kick,
            "verification_notify_success": c.verification_notify_success,
            "verification_welcome_dm": c.verification_welcome_dm or "",
        }
        for c in configs
    ]

    bots_list = [
        {
            "id": b.id,
            "name": (
                f"{b.name} (@{b.max_username})" if b.name and b.max_username
                else b.name or f"@{b.max_username}"
            ),
        }
        for b in bots
        if b.is_active
    ]

    return templates.TemplateResponse(
        "welcome.html",
        {
            "request": request,
            "user": user,
            "configs_data": configs_data,
            "bots_list": bots_list,
            "active_page": "welcome",
            "default_verify_msg": _DEFAULT_VERIFY_MSG,
            "default_verify_btn": _DEFAULT_VERIFY_BTN,
            "default_welcome_dm": _DEFAULT_WELCOME_DM,
        },
    )


@router.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request, session: DBSession):
    """Страница управления AssistantConfig'ами (AI-ассистент DM).

    api_key расшифровывается на сервере, чтобы фронт сразу показал его
    (для редактирования). Передача plain-ключа клиенту — известный
    компромисс, оправданный UX'ом «Покажи мой ключ».
    """
    user = await _require_user(request, session)

    configs_result = await session.execute(
        select(AssistantConfig)
        .where(AssistantConfig.user_id == user.id)
        .order_by(AssistantConfig.created_at.desc())
    )
    configs = configs_result.scalars().all()

    bots_result = await session.execute(
        select(Bot).where(Bot.user_id == user.id)
    )
    bots = bots_result.scalars().all()
    bot_map = {b.id: b for b in bots}

    configs_data = [
        {
            "id": c.id,
            "group_name": c.group_name or "",
            "group_id": c.group_id,
            "bot_id": c.bot_id,
            "bot_name": (
                bot_map[c.bot_id].name or ""
                if c.bot_id and c.bot_id in bot_map
                else ""
            ),
            "bot_username": (
                f"@{bot_map[c.bot_id].max_username}"
                if c.bot_id and c.bot_id in bot_map and bot_map[c.bot_id].max_username
                else ""
            ),
            "is_enabled": c.is_enabled,
            "system_prompt": c.system_prompt or "",
            "model_name": c.model_name,
            "api_url": c.api_url or "",
            "api_key": decrypt_token(c.api_key) if c.api_key else "",
            "embedding_model": c.embedding_model or "",
            "embedding_api_url": c.embedding_api_url or "",
            "embedding_api_key": decrypt_token(c.embedding_api_key) if c.embedding_api_key else "",
            "retrieval_top_k": c.retrieval_top_k,
            "retrieval_threshold": c.retrieval_threshold,
        }
        for c in configs
    ]

    bots_list = [
        {
            "id": b.id,
            "name": (
                f"{b.name} (@{b.max_username})" if b.name and b.max_username
                else b.name or f"@{b.max_username}"
            ),
        }
        for b in bots
        if b.is_active
    ]

    return templates.TemplateResponse(
        "assistant.html",
        {
            "request": request,
            "user": user,
            "configs_data": configs_data,
            "bots_list": bots_list,
            "active_page": "assistant",
        },
    )
