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


class VerificationStatus(str, enum.Enum):
    pending = "pending"
    verified = "verified"
    kicked = "kicked"
    expired = "expired"


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
    plan: Mapped[Plan] = mapped_column(Enum(Plan, native_enum=False), default=Plan.free)
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

    # ── Verification (captcha-gate) ───────────────────────────────────────────
    verification_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_timeout_min: Mapped[int] = mapped_column(Integer, default=10)
    # None → use built-in default template
    verification_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verification_button_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verification_kick: Mapped[bool] = mapped_column(Boolean, default=True)
    verification_notify_success: Mapped[bool] = mapped_column(Boolean, default=True)
    # None → don't send DM
    verification_welcome_dm: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="pairs")
    bot: Mapped[Optional["Bot"]] = relationship(
        back_populates="pairs", foreign_keys=[bot_id]
    )
    post_links: Mapped[list["PostLink"]] = relationship(back_populates="pair")
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(back_populates="pair")
    verification_requests: Mapped[list["VerificationRequest"]] = relationship(
        back_populates="pair",
        cascade="all, delete-orphan",
        foreign_keys="[VerificationRequest.pair_id]",
    )

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


# ─── Welcome / Verification configs ───────────────────────────────────────────

class WelcomeConfig(Base):
    """
    Standalone verification (captcha-gate) config for a group.
    Independent of channel-group pairs — can be used for any group
    without a paired channel.
    """
    __tablename__ = "welcome_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    group_link: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    # ── Verification settings ────────────────────────────────────────────────
    verification_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    verification_timeout_min: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # None → use built-in default template
    verification_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verification_button_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verification_kick: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    verification_notify_success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # None → use built-in default DM
    verification_welcome_dm: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    bot: Mapped["Bot"] = relationship()
    verification_requests: Mapped[list["VerificationRequest"]] = relationship(
        back_populates="welcome_config",
        cascade="all, delete-orphan",
        foreign_keys="[VerificationRequest.welcome_config_id]",
    )

    __table_args__ = (
        UniqueConstraint("bot_id", "group_id", name="uq_welcome_config_bot_group"),
        Index("ix_welcome_config_bot_group", "bot_id", "group_id"),
    )


# ─── Logging ──────────────────────────────────────────────────────────────────

class EventLog(Base):
    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Which bot generated the event (optional context)
    bot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bots.id", ondelete="SET NULL"), nullable=True)
    level: Mapped[LogLevel] = mapped_column(Enum(LogLevel, native_enum=False), default=LogLevel.info)
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
    status: Mapped[PostStatus] = mapped_column(Enum(PostStatus, native_enum=False), default=PostStatus.pending)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="scheduled_posts")
    pair: Mapped["ChannelGroupPair"] = relationship(back_populates="scheduled_posts")

    __table_args__ = (
        Index("ix_scheduled_status_at", "status", "scheduled_at"),
    )


# ─── Verification requests ────────────────────────────────────────────────────

class VerificationRequest(Base):
    """
    Tracks a single captcha-gate challenge for a new group member.

    Flow:
      1. user joins group → bot creates VerificationRequest (status=pending)
      2. bot posts a message with deep-link button to the group
      3. user clicks → opens bot → /start with payload "verify_<token>"
      4. bot marks status=verified, edits group message, optionally sends DM
      5. scheduler: if deadline passed and status=pending → kick + status=kicked
    """
    __tablename__ = "verification_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Legacy FK — used for pair-based verification (channel+group pairs)
    pair_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("channel_group_pairs.id", ondelete="CASCADE"), nullable=True
    )
    # New FK — used for standalone WelcomeConfig-based verification
    welcome_config_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("welcome_configs.id", ondelete="CASCADE"), nullable=True
    )
    # Max user_id of the person being verified
    max_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Secret token embedded in the deep-link payload
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Message sent in the group chat (to delete on success/failure)
    group_message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, native_enum=False), default=VerificationStatus.pending
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    pair: Mapped[Optional["ChannelGroupPair"]] = relationship(
        back_populates="verification_requests",
        foreign_keys=[pair_id],
    )
    welcome_config: Mapped[Optional["WelcomeConfig"]] = relationship(
        back_populates="verification_requests",
        foreign_keys=[welcome_config_id],
    )

    __table_args__ = (
        Index("ix_verification_token", "token"),
        Index("ix_verification_status_deadline", "status", "deadline"),
    )


# ─── AI Assistant ──────────────────────────────────────────────────────────────

class AssistantConfig(Base):
    """Per-(bot, group) AI assistant configuration."""
    __tablename__ = "assistant_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    api_url: Mapped[str] = mapped_column(String(512), nullable=False, default="https://openrouter.ai/api/v1")
    api_key: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    bot: Mapped["Bot"] = relationship()
    user_contexts: Mapped[list["UserBotContext"]] = relationship(
        back_populates="assistant_config", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("bot_id", "group_id", name="uq_assistant_config_bot_group"),
        Index("ix_assistant_config_bot_group", "bot_id", "group_id"),
    )


class UserBotContext(Base):
    """Tracks which groups a MAX user has been verified in for a given bot."""
    __tablename__ = "user_bot_contexts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
    )
    max_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    assistant_config_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("assistant_configs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    assistant_config: Mapped[Optional["AssistantConfig"]] = relationship(back_populates="user_contexts")

    __table_args__ = (
        UniqueConstraint("bot_id", "max_user_id", "group_id", name="uq_user_bot_context"),
        Index("ix_user_bot_context_lookup", "bot_id", "max_user_id"),
    )


class ConversationMessage(Base):
    """Stores full conversation history between a MAX user and the AI assistant."""
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
    )
    max_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_avatar: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    assistant_config_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("assistant_configs.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_conversation_lookup", "bot_id", "max_user_id", "created_at"),
    )


class InboxReadStatus(Base):
    """Когда владелец инбокса последний раз открывал переписку с конкретным пользователем."""
    __tablename__ = "inbox_read_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
    )
    max_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("bot_id", "max_user_id", name="uq_inbox_read_bot_user"),
        Index("ix_inbox_read_bot_user", "bot_id", "max_user_id"),
    )
