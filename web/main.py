"""
Точка входа web-сервиса (контейнер web в docker-compose).

Один FastAPI-процесс отдаёт два слоя:
  • HTML-страницы на Jinja2 — роутер ``web.routers.pages``;
  • JSON REST API — роутер ``web.routers.api``.

Оба слоя авторизуются по одному и тому же JWT (cookie ``access_token``).
При первом запуске сидится администратор из ``ADMIN_EMAIL`` /
``ADMIN_PASSWORD`` в .env. Если БД ещё пустая (alembic не успел или
не применил миграции), таблицы создаются «в догон» через ``create_all``.
"""
import logging
import sys

sys.path.insert(0, "/app")

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from db.models import Base, Subscription, User
from db.session import AsyncSessionLocal, async_engine
from shared.config import get_settings
from web.auth import hash_password
from web.routers import api, pages

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("web.main")
settings = get_settings()

app = FastAPI(
    title="MaxBot Dashboard",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url=None,
)

# ── Static files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="/app/web/static"), name="static")

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(pages.router)
app.include_router(api.router)


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    """Подстраховка схемы и создание первого админа."""
    # Подстраховка: alembic запускается отдельным сервисом ``migrate``.
    # Если он не отработал, ``create_all`` хотя бы поднимет недостающие
    # таблицы — лучше работа с дефолтной схемой, чем 500 на каждом запросе.
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Сидим первого админа, если в БД ещё нет пользователя с таким email.
    # Без этого на свежей установке некем было бы войти.
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(User).where(User.email == settings.admin_email)
        )
        if not result.scalar_one_or_none():
            admin = User(
                email=settings.admin_email,
                hashed_password=hash_password(settings.admin_password),
            )
            session.add(admin)
            await session.flush()
            sub = Subscription(user_id=admin.id, plan="free")
            session.add(sub)
            await session.commit()
            logger.info("Admin user created: %s", settings.admin_email)


# ── Обработчики исключений ────────────────────────────────────────────────────

@app.exception_handler(302)
async def redirect_handler(request: Request, exc):
    """Превращает HTTPException(status_code=302) в полноценный редирект.

    Используется в зависимостях вроде get_current_user_ui, которым нужно
    отправить юзера на /login без ручного return RedirectResponse.
    """
    return RedirectResponse(url=exc.headers["location"], status_code=302)
