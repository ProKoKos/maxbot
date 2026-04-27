"""Настройки верификации пары: PATCH /pairs/{id}/verification."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from db.models import ChannelGroupPair
from web.deps import CurrentUser, DBSession, RateLimit

router = APIRouter()


class VerificationUpdate(BaseModel):
    verification_enabled: bool | None = None
    verification_timeout_min: int | None = None
    verification_message: str | None = None
    verification_button_text: str | None = None
    verification_kick: bool | None = None
    verification_notify_success: bool | None = None
    verification_welcome_dm: str | None = None


@router.patch("/pairs/{pair_id}/verification")
async def update_verification(
    pair_id: int,
    body: VerificationUpdate,
    current_user: CurrentUser,
    session: DBSession,
    _: RateLimit,
):
    result = await session.execute(
        select(ChannelGroupPair).where(
            ChannelGroupPair.id == pair_id,
            ChannelGroupPair.user_id == current_user.id,
        )
    )
    pair = result.scalar_one_or_none()
    if not pair:
        raise HTTPException(404, "Pair not found")

    if body.verification_enabled is not None:
        if body.verification_enabled and pair.bot_id is None:
            raise HTTPException(400, "Cannot enable verification on a pair with no bot assigned")
        pair.verification_enabled = body.verification_enabled

    if body.verification_timeout_min is not None:
        if not 1 <= body.verification_timeout_min <= 1440:
            raise HTTPException(400, "Timeout must be between 1 and 1440 minutes")
        pair.verification_timeout_min = body.verification_timeout_min

    if body.verification_message is not None:
        pair.verification_message = body.verification_message or None

    if body.verification_button_text is not None:
        pair.verification_button_text = body.verification_button_text or None

    if body.verification_kick is not None:
        pair.verification_kick = body.verification_kick

    if body.verification_notify_success is not None:
        pair.verification_notify_success = body.verification_notify_success

    if body.verification_welcome_dm is not None:
        pair.verification_welcome_dm = body.verification_welcome_dm or None

    await session.commit()
    return {"ok": True}
