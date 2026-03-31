"""
FastAPI web application entry point.
Serves both the Jinja2 HTML UI and the JSON REST API.
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
    # Ensure tables exist (alembic runs separately, this is a safety net)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed admin user if not exists
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


# ── Exception handlers ────────────────────────────────────────────────────────

@app.exception_handler(302)
async def redirect_handler(request: Request, exc):
    return RedirectResponse(url=exc.headers["location"], status_code=302)
