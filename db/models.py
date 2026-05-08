"""
ORM-модели проекта (SQLAlchemy 2.0, async).

Группы таблиц:
  • Users / Subscription — SaaS-уровень: пользователь и его лимиты;
  • Bot / PollingMarker  — MAX-боты, их зашифрованные токены и
    long-polling-курсоры;
  • ChannelGroupPair / PostLink — пары канал↔группа + лог дублирования;
  • WelcomeConfig + VerificationRequest — captcha-gate;
  • ScheduledPost — отложенный автопостинг;
  • EventLog — журнал событий для UI;
  • AssistantConfig + UserBotContext + ConversationMessage +
    InboxReadStatus — AI-ассистент и веб-инбокс.

Принципы:
  • datetimes хранятся timezone-aware (``DateTime(timezone=True)``);
  • Enum'ы — без native enum в БД (``native_enum=False``), хранятся как
    VARCHAR — это упрощает добавление новых значений без миграций типов;
  • ondelete=CASCADE применяется только там, где удаление родителя
    действительно делает дочерние записи бессмысленными
    (PollingMarker, VerificationRequest, ConversationMessage и т.п.);
  • ondelete=SET NULL — когда дочерние записи нужно сохранить как историю
    даже после удаления родителя (EventLog.bot_id, ChannelGroupPair.bot_id).
"""
import enum
from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
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
    """База ORM-моделей. Все модели наследуются отсюда."""
    pass


# ─── Перечисления (хранятся как VARCHAR, а не PostgreSQL enum) ───────────────
# native_enum=False во всех колонках Enum: добавление нового значения
# не требует миграции ALTER TYPE … ADD VALUE.

class Plan(str, enum.Enum):
    """Тарифный план пользователя."""
    free = "free"
    pro = "pro"


class LogLevel(str, enum.Enum):
    """Уровни записей в EventLog. Используются как фильтр в UI."""
    info = "info"
    warning = "warning"
    error = "error"


class PostStatus(str, enum.Enum):
    """Жизненный цикл отложенного поста: pending → sent | failed."""
    pending = "pending"
    sent = "sent"
    failed = "failed"


class VerificationStatus(str, enum.Enum):
    """Статус captcha-gate-запроса.

    pending  — ждём, пока пользователь нажмёт кнопку;
    verified — пользователь прошёл проверку;
    kicked   — scheduler выгнал по таймауту;
    expired  — таймаут истёк, но кик не настроен (просто запрос «протух»).
    """
    pending = "pending"
    verified = "verified"
    kicked = "kicked"
    expired = "expired"


# ─── Пользователи и SaaS-уровень ─────────────────────────────────────────────

class User(Base):
    """Пользователь сервиса (владелец ботов и пар)."""
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
    """Тарифный план пользователя и его лимиты.

    1:1 к User. Лимиты хранятся как JSON-строка в Text — это позволяет
    добавлять новые ключи без миграций. Формат::

        {"max_bots": 3, "max_pairs": 5, "max_posts_per_day": 20}

    Сейчас лимиты не енфорсятся в коде (TODO для биллинга).
    """
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    plan: Mapped[Plan] = mapped_column(Enum(Plan, native_enum=False), default=Plan.free)
    limits: Mapped[str] = mapped_column(
        Text, default='{"max_bots": 1, "max_pairs": 1, "max_posts_per_day": 10}'
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="subscription")


# ─── Боты ────────────────────────────────────────────────────────────────────

class Bot(Base):
    """Бот мессенджера MAX, привязанный к пользователю.

    Токен лежит зашифрованным Fernet'ом в ``encrypted_token`` —
    расшифровка через :func:`bot.crypto.decrypt_token`.
    Поля ``max_user_id`` / ``max_username`` кешируются из MAX /me
    при первом успешном подключении в supervisor'е и используются
    для построения deep-link'ов и проверок «бот это или нет».
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

    __table_args__ = (
        # Списочный API /api/bots: WHERE user_id = current_user.id.
        Index("ix_bot_user", "user_id"),
    )


# ─── Ядро: пары канал↔группа ─────────────────────────────────────────────────

class ChannelGroupPair(Base):
    """Связка «канал MAX → группа обсуждений», обслуживаемая одним ботом.

    Содержит как настройки канала/группы (id, name, link), так и параметры
    captcha-gate-верификации (verification_*) — последние используются,
    когда для группы НЕ задан отдельный WelcomeConfig (legacy-путь).

    Удаление бота не должно сносить пару (там может быть история постов
    и автопостинг). Поэтому ondelete=SET NULL: пара остаётся, но
    становится «неоперациональной» (см. ``is_operational``).
    """

    __tablename__ = "channel_group_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # SET NULL — чтобы пара пережила удаление бота. UI показывает её как
    # «без бота» и автоматически обесцвечивает enabled (см. is_operational).
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

    # ── Настройки captcha-gate-верификации (legacy путь) ─────────────────────
    # Используются, только если для группы НЕТ отдельного WelcomeConfig
    # (тот имеет приоритет — см. CLAUDE.md и handlers._handle_member_added).
    verification_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_timeout_min: Mapped[int] = mapped_column(Integer, default=10)
    # None → берём DEFAULT_VERIFY_MSG из bot/constants.py.
    verification_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verification_button_text: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verification_kick: Mapped[bool] = mapped_column(Boolean, default=True)
    verification_notify_success: Mapped[bool] = mapped_column(Boolean, default=True)
    # None → welcome-DM не шлём вовсе.
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
        # Композитный индекс под основной запрос handler'а:
        # «найди активную пару для этого канала и этого бота».
        Index("ix_pair_bot_channel", "bot_id", "channel_id", "enabled"),
        # Списочный API /api/pairs: WHERE user_id = current_user.id.
        Index("ix_pair_user", "user_id"),
    )

    @property
    def is_operational(self) -> bool:
        """Пара считается «работающей» только если она enabled и привязан бот."""
        return self.enabled and self.bot_id is not None


class PostLink(Base):
    """Связь «пост в канале → сообщение в группе обсуждений».

    Главное назначение — дедупликация: при поступлении ``message_created``
    handler смотрит, нет ли уже PostLink с таким channel_post_id, и если
    есть — пропускает обработку (long-polling может прислать апдейт повторно).

    Также используется как реестр всех продублированных постов для будущих
    фич (например, синк правок «канал → группа»).
    """

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
    """Курсор long-polling MAX API на одного бота.

    Хранится в БД, чтобы пережить рестарты контейнера: иначе после
    редеплоя бот получил бы заново все накопленные за окно события.
    Удаляется CASCADE'ом вместе с ботом.
    """

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


# ─── Конфиги приветствия / верификации ───────────────────────────────────────

class WelcomeConfig(Base):
    """Standalone-конфиг captcha-gate для произвольной группы.

    Появился позже, чем verification_* в ChannelGroupPair, и теперь является
    рекомендуемым способом: группе не обязательно быть привязанной к
    каналу. Если на одну и ту же группу заведён и WelcomeConfig, и пара —
    приоритет у WelcomeConfig (см. CLAUDE.md и handlers).

    UNIQUE(bot_id, group_id) защищает от дублей: на одну группу — один
    конфиг для одного бота.
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
        # Списочный API /api/welcome/configs: WHERE user_id = current_user.id.
        Index("ix_welcome_config_user", "user_id"),
    )


# ─── Журнал событий ──────────────────────────────────────────────────────────

class EventLog(Base):
    """Журнал бизнес-событий для отображения в UI.

    Не путать с logger.info/.error — туда уходят только сообщения для
    оператора сервера. EventLog — это то, что видит конкретный
    пользователь в /logs (его собственные действия и события его ботов).

    bot_id обнуляется при удалении бота, чтобы записи в журнале
    остались как историческая справка.
    """
    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    bot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bots.id", ondelete="SET NULL"), nullable=True)
    level: Mapped[LogLevel] = mapped_column(Enum(LogLevel, native_enum=False), default=LogLevel.info)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[Optional["User"]] = relationship(back_populates="logs")

    __table_args__ = (
        Index("ix_log_created", "created_at"),
        # Страница /logs всегда фильтрует по user_id и сортирует по created_at DESC.
        Index("ix_event_log_user_created", "user_id", "created_at"),
        # Будущая фильтрация «логи конкретного бота» в UI.
        Index("ix_event_log_bot_created", "bot_id", "created_at"),
    )


# ─── Отложенные публикации ───────────────────────────────────────────────────

class ScheduledPost(Base):
    """Отложенный пост для автопостинга.

    Опрашивается scheduler'ом каждые 30 сек (см. scheduler/main.py).
    attachments_json хранит JSON-массив вложений (схема MAX inline_keyboard
    и т.п.); строка вместо JSONB — для совместимости с миграциями.
    """
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
        # Используется scheduler'ом: WHERE status=pending AND scheduled_at<=now.
        Index("ix_scheduled_status_at", "status", "scheduled_at"),
        # Страница автопостинга: посты конкретной пары, фильтр по статусу.
        Index("ix_scheduled_post_pair_status", "pair_id", "status"),
    )


# ─── Запросы верификации ─────────────────────────────────────────────────────

class VerificationRequest(Base):
    """Один запрос captcha-gate для конкретного нового участника.

    Поток:
      1. user_added → handler создаёт запись (status=pending);
      2. бот шлёт в группу сообщение с deep-link-кнопкой;
      3. юзер кликает → bot_started с payload="verify_<token>";
      4. handler помечает status=verified, удаляет сообщение, шлёт DM;
      5. при истечении deadline scheduler делает status=kicked + kick.

    Запись связана с одним из двух конфигов: welcome_config_id (новый
    стиль) либо pair_id (legacy). Ровно один из FK заполнен.
    """
    __tablename__ = "verification_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Legacy: верификация в рамках пары канал↔группа.
    pair_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("channel_group_pairs.id", ondelete="CASCADE"), nullable=True
    )
    # Новый стиль: standalone-конфиг для произвольной группы.
    welcome_config_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("welcome_configs.id", ondelete="CASCADE"), nullable=True
    )
    # MAX user_id того, кого верифицируем (строка, т.к. в MAX это число
    # большой разрядности — храним как строку, чтобы избежать целочисл. переполнений).
    max_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Секретный токен в payload deep-link'а — генерируется secrets.token_hex(24).
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # ID сообщения, отправленного в группу — чтобы потом его удалить
    # (на успех или на kick).
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
        # Лукап в _handle_bot_started по token (deep-link payload).
        Index("ix_verification_token", "token"),
        # scheduler.kick_expired_verifications: WHERE status=pending AND deadline<=now.
        Index("ix_verification_status_deadline", "status", "deadline"),
        # handlers._handle_member_added: проверка «уже есть pending».
        Index("ix_verification_welcome_config_status", "welcome_config_id", "status"),
        Index("ix_verification_pair_status", "pair_id", "status"),
    )


# ─── AI-ассистент ────────────────────────────────────────────────────────────

class AssistantConfig(Base):
    """Конфигурация AI-ассистента для пары (бот, группа).

    На один (bot_id, group_id) — один конфиг. Активируется флагом
    is_enabled, чтобы можно было временно «выключить мозги» без удаления
    настроек. api_key хранится Fernet-зашифрованным, как и токены ботов.
    """
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
    # ── База знаний (RAG) ──────────────────────────────────────────────────────
    embedding_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    embedding_api_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Fernet-зашифрованный ключ embedding API (аналогично api_key)
    embedding_api_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    retrieval_top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    retrieval_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.70)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    bot: Mapped["Bot"] = relationship()
    user_contexts: Mapped[list["UserBotContext"]] = relationship(
        back_populates="assistant_config", cascade="all, delete-orphan"
    )
    knowledge_documents: Mapped[list["KnowledgeDocument"]] = relationship(
        back_populates="config", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("bot_id", "group_id", name="uq_assistant_config_bot_group"),
        Index("ix_assistant_config_bot_group", "bot_id", "group_id"),
        # Списочный API /api/assistant/configs: WHERE user_id = current_user.id.
        Index("ix_assistant_config_user", "user_id"),
    )


class UserBotContext(Base):
    """«Членство» MAX-пользователя в конкретном AI-ассистенте.

    Создаётся при успешной верификации (см. handlers._save_user_bot_context),
    если для (bot, group) есть AssistantConfig. Используется как маркер
    «этому юзеру можно отвечать AI-моделью в DM». Если пользователь
    верифицирован в нескольких группах одного бота — записей будет несколько.

    assistant_config_id обнуляется при удалении конфига (SET NULL),
    но запись остаётся — handlers умеют переподвязать её к новому
    конфигу с тем же group_id.
    """
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
    """История переписки между пользователем MAX и AI-ассистентом.

    Хранится целиком (без обрезки): нужна и для контекста модели
    (см. _handle_dm_message), и для веб-инбокса (владелец видит, что
    отвечал бот). chat_id и user_avatar заполняются по возможности —
    они нужны, чтобы из инбокса можно было ответить вручную и показать
    аватарку без повторного запроса в MAX API.
    """
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
    # "user" — сообщение от MAX-пользователя; "assistant" — ответ AI или
    # ручной ответ владельца через инбокс. Совместимо с OpenAI-форматом.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-массив вложений из DM: [{"type": "image", "token": "...", ...}, ...]
    # Хранится как Text (как ScheduledPost.attachments_json) — без JSONB,
    # чтобы не усложнять миграции. По умолчанию пустой массив "[]".
    attachments_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_conversation_lookup", "bot_id", "max_user_id", "created_at"),
    )


class UserProfile(Base):
    """Профильные данные MAX-пользователя, кешированные от бота.

    Создаётся/обновляется при каждом входящем DM (``_handle_dm_message``),
    поэтому данные всегда отражают последнее состояние профиля в MAX.

    Хранит все поля, которые MAX Bot API возвращает в объекте ``sender``:
    ``first_name``, ``last_name``, ``username``, ``description`` (биография),
    ``avatar_url``, ``full_avatar_url``. Комбинированное ``name`` = объединение
    first_name + last_name вычисляется на лету (property).

    UNIQUE(bot_id, max_user_id) — один профиль на пару бот/пользователь.
    """
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
    )
    max_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # @username без символа @
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Биография / «О себе»
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    full_avatar_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Время последнего обновления из MAX API
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("bot_id", "max_user_id", name="uq_user_profile"),
        Index("ix_user_profile_lookup", "bot_id", "max_user_id"),
    )

    @property
    def display_name(self) -> str:
        """Полное имя: «Имя Фамилия» или fallback на username / max_user_id."""
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return " ".join(parts)
        return self.username or self.max_user_id


class UserChannelMembership(Base):
    """Членство MAX-пользователя в канале бота.

    Создаётся при получении события ``user_added`` с ``is_channel=True``,
    удаляется при ``user_removed`` с ``is_channel=True``.
    Используется для маршрутизации FAQ/KB: бот знает, каким каналам
    релевантен конкретный пользователь при обращении в DM.

    UNIQUE(bot_id, max_user_id, channel_id) — чтобы повторный вход
    (или дублированный apdate) не плодил дубли; при upsert'е просто
    обновляем joined_at.
    """
    __tablename__ = "user_channel_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(
        ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
    )
    max_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Название канала — берётся из ChannelGroupPair при наличии пары,
    # иначе остаётся None (будет отображаться как channel_id в UI).
    channel_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("bot_id", "max_user_id", "channel_id", name="uq_user_channel_membership"),
        # Основной лукап: профиль пользователя → его каналы.
        Index("ix_user_channel_membership_lookup", "bot_id", "max_user_id"),
        # Будущий лукап: какие пользователи подписаны на конкретный канал.
        Index("ix_user_channel_membership_channel", "bot_id", "channel_id"),
    )


class KnowledgeDocument(Base):
    """Документ базы знаний для AI-ассистента.

    Каждый документ принадлежит конкретному AssistantConfig и проходит
    жизненный цикл: pending → indexing → ready (или error).
    После индексирования нарезается на KnowledgeChunk'и с векторами.
    """
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assistant_config_id: Mapped[int] = mapped_column(
        ForeignKey("assistant_configs.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # "text" | "file" | "url"
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_hint: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # "pending" | "indexing" | "ready" | "error"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    config: Mapped["AssistantConfig"] = relationship(back_populates="knowledge_documents")
    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_knowledge_document_config", "assistant_config_id"),
        Index("ix_knowledge_document_status", "assistant_config_id", "status"),
    )


class KnowledgeChunk(Base):
    """Фрагмент (чанк) документа базы знаний с векторным эмбеддингом.

    Хранит текст + embedding размерностью 1536 (OpenAI text-embedding-3-small
    и совместимые). Используется для cosine-similarity поиска при обработке
    входящих DM: наиболее релевантные чанки инжектируются в system_prompt.
    """
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)

    document: Mapped["KnowledgeDocument"] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_knowledge_chunk_document", "document_id"),
    )


class InboxReadStatus(Base):
    """Время последнего открытия переписки в инбоксе.

    Используется UI для подсчёта непрочитанных сообщений (badge на аватарке
    пользователя/группы) — сравниваем ConversationMessage.created_at
    с last_read_at. Запись upsert'ится при открытии переписки —
    см. ``inbox_messages`` в web/routers/api.py.
    """
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
