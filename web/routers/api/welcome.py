"""Welcome configs (standalone верификация): CRUD-эндпоинты /welcome/configs/*."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from db.models import Bot, ChannelGroupPair, WelcomeConfig
from web.deps import CurrentUser, DBSession, RateLimit

router = APIRouter()


class WelcomeConfigCreate(BaseModel):
    bot_id: int
    group_id: str
    group_name: str
    group_link: str = ""


class WelcomeConfigUpdate(BaseModel):
    group_link: str | None = None
    verification_enabled: bool | None = None
    verification_timeout_min: int | None = None
    verification_message: str | None = None
    verification_button_text: str | None = None
    verification_kick: bool | None = None
    verification_notify_success: bool | None = None
    verification_welcome_dm: str | None = None


@router.get("/welcome/configs")
async def list_welcome_configs(current_user: CurrentUser, session: DBSession, _: RateLimit):
    result = await session.execute(
        select(WelcomeConfig)
        .where(WelcomeConfig.user_id == current_user.id)
        .order_by(WelcomeConfig.created_at.desc())
    )
    configs = result.scalars().all()
    return [
        {
            "id": c.id,
            "bot_id": c.bot_id,
            "group_id": c.group_id,
            "group_name": c.group_name,
            "group_link": c.group_link,
            "verification_enabled": c.verification_enabled,
            "verification_timeout_min": c.verification_timeout_min,
            "verification_message": c.verification_message,
            "verification_button_text": c.verification_button_text,
            "verification_kick": c.verification_kick,
            "verification_notify_success": c.verification_notify_success,
            "verification_welcome_dm": c.verification_welcome_dm,
            "created_at": c.created_at.isoformat(),
        }
        for c in configs
    ]


@router.post("/welcome/configs", status_code=201)
async def create_welcome_config(
    body: WelcomeConfigCreate,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    """Create a standalone verification config for a group."""
    bot_result = await session.execute(
        select(Bot).where(Bot.id == body.bot_id, Bot.user_id == current_user.id)
    )
    if not bot_result.scalar_one_or_none():
        raise HTTPException(404, "Bot not found")

    # Check for duplicates
    existing = await session.execute(
        select(WelcomeConfig).where(
            WelcomeConfig.bot_id == body.bot_id,
            WelcomeConfig.group_id == body.group_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "A welcome config for this bot+group already exists")

    # If no group_link provided, fall back to matching ChannelGroupPair
    group_link = body.group_link
    if not group_link:
        pair_result = await session.execute(
            select(ChannelGroupPair).where(
                ChannelGroupPair.group_id == body.group_id,
                ChannelGroupPair.user_id == current_user.id,
                ChannelGroupPair.group_link != "",
            )
        )
        pair = pair_result.scalar_one_or_none()
        if pair and pair.group_link:
            group_link = pair.group_link

    config = WelcomeConfig(
        user_id=current_user.id,
        bot_id=body.bot_id,
        group_id=body.group_id,
        group_name=body.group_name,
        group_link=group_link,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return {"id": config.id}


@router.patch("/welcome/configs/{config_id}")
async def update_welcome_config(
    config_id: int,
    body: WelcomeConfigUpdate,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    result = await session.execute(
        select(WelcomeConfig).where(
            WelcomeConfig.id == config_id,
            WelcomeConfig.user_id == current_user.id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Welcome config not found")

    if body.group_link is not None:
        config.group_link = body.group_link

    if body.verification_enabled is not None:
        config.verification_enabled = body.verification_enabled

    if body.verification_timeout_min is not None:
        if not 1 <= body.verification_timeout_min <= 1440:
            raise HTTPException(400, "Timeout must be between 1 and 1440 minutes")
        config.verification_timeout_min = body.verification_timeout_min

    if body.verification_message is not None:
        config.verification_message = body.verification_message or None

    if body.verification_button_text is not None:
        config.verification_button_text = body.verification_button_text or None

    if body.verification_kick is not None:
        config.verification_kick = body.verification_kick

    if body.verification_notify_success is not None:
        config.verification_notify_success = body.verification_notify_success

    if body.verification_welcome_dm is not None:
        config.verification_welcome_dm = body.verification_welcome_dm or None

    await session.commit()
    return {"ok": True}


@router.delete("/welcome/configs/{config_id}", status_code=204)
async def delete_welcome_config(
    config_id: int,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    result = await session.execute(
        select(WelcomeConfig).where(
            WelcomeConfig.id == config_id,
            WelcomeConfig.user_id == current_user.id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(404, "Welcome config not found")
    await session.delete(config)
    await session.commit()
