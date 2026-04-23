import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ─── Enums ────────────────────────────────────────────────────────────────────

class Plan(str, enum.Enum):
    free = "free"
    pro = "pro"


class LogLevel(str, enum.Enum):
    info = "info"
    warning = "warning"
    error = "error"


class PostStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


# ─── Users & SaaS ─────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    api_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    subscription: Mapped["Subscription"] = relationship(back_populates="user", uselist=False)
    bots: Mapped[list["Bot"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    pairs: Mapped[list["ChannelGroupPair"]] = relationship(back_populates="user")
    logs: Mapped[list["EventLog"]] = relationship(back_populates="user")
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(back_populates="user")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    plan: Mapped[Plan] = mapped_column(Enum(Plan), default=Plan.free)
    # JSON-encoded limits: {"max_bots": 3, "max_pairs": 5, "max_posts_per_day": 20}
    limits: Mapped[str] = mapped_column(
        Text, default='{"max_bots": 1, "max_pairs": 1, "max_posts_per_day": 10}'
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="subscription")


# ─── Bots ─────────────────────────────────────────────────────────────────────

class Bot(Base):
    """
    A Max messenger bot owned by a user.
    Token is stored encrypted (Fernet) — decrypt with bot.crypto.decrypt_token().
    """
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Fernet-encrypted Max bot token
    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    # Cached info from /me (username, bot_id from Max)
    max_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    max_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="bots")
    pairs: Mapped[list["ChannelGroupPair"]] = relationship(
        back_populates="bot",
        foreign_keys="ChannelGroupPair.bot_id",
    )
    polling_marker: Mapped[Optional["PollingMarker"]] = relationship(
        back_populates="bot", uselist=False, cascade="all, delete-orphan"
    )


# ─── Bot core ─────────────────────────────────────────────────────────────────

class ChannelGroupPair(Base):
    """Maps a Max channel → discussion group, operated by a specific Bot."""

    __tablename__ = "channel_group_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Nullable: when bot is deleted, bot_id is SET NULL and pair is auto-disabled
    bot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bots.id", ondelete="SET NULL"), nullable=True
    )
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    channel_link: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    group_link: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="pairs")
    bot: Mapped[Optional["Bot"]] = relationship(
        back_populates="pairs", foreign_keys=[bot_id]
    )
    post_links: Mapped[list["PostLink"]] = relationship(back_populates="pair")
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(back_populates="pair")

    __table_args__ = (
        Index("ix_pair_bot_channel", "bot_id", "channel_id", "enabled"),
    )

    @property
    def is_operational(self) -> bool:
        """A pair is operational only when it has an active bot and is enabled."""
        return self.enabled and self.bot_id is not None


class PostLink(Base):
    """Tracks channel post → discussion group message mapping."""

    __tablename__ = "post_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("channel_group_pairs.id"))
    channel_post_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    group_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    pair: Mapped["ChannelGroupPair"] = relationship(back_populates="post_links")


class PollingMarker(Base):
    """Per-bot long-polling cursor. Survives restarts."""

    __tablename__ = "polling_markers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    marker: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    bot: Mapped["Bot"] = relationship(back_populates="polling_marker")


# ─── Logging ──────────────────────────────────────────────────────────────────

class EventLog(Base):
    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Which bot generated the event (optional context)
    bot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bots.id", ondelete="SET NULL"), nullable=True)
    level: Mapped[LogLevel] = mapped_column(Enum(LogLevel), default=LogLevel.info)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[Optional["User"]] = relationship(back_populates="logs")

    __table_args__ = (
        Index("ix_log_created", "created_at"),
    )


# ─── Scheduled posts ──────────────────────────────────────────────────────────

class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    pair_id: Mapped[int] = mapped_column(ForeignKey("channel_group_pairs.id"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    attachments_json: Mapped[str] = mapped_column(Text, default="[]")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[PostStatus] = mapped_column(Enum(PostStatus), default=PostStatus.pending)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="scheduled_posts")
    pair: Mapped["ChannelGroupPair"] = relationship(back_populates="scheduled_posts")

    __table_args__ = (
        Index("ix_scheduled_status_at", "status", "scheduled_at"),
    )
