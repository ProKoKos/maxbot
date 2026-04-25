"""012: дополнительные индексы для производительности

Добавляем индексы под самые частые фильтры, которые делает приложение,
и которых ещё нет в БД.

Что и зачем:

  • event_logs(user_id, created_at DESC) — экран /logs (страница и API)
    всегда фильтрует по user_id и сортирует по created_at DESC.
    На больших объёмах журнала старый «full scan + filesort» становится
    медленным.

  • event_logs(bot_id, created_at DESC) — будущие фильтры «логи конкретного
    бота» в UI. bot_id есть почти у каждой записи.

  • verification_requests(welcome_config_id, status) — handlers и scheduler
    часто ищут pending-запросы для конкретного конфига. Раньше работал
    только индекс по token и по (status, deadline).

  • verification_requests(pair_id, status) — то же, но для legacy pair-based
    верификации.

  • scheduled_posts(pair_id, status) — UI на странице автопостинга
    показывает посты конкретной пары, отфильтрованные по статусу.

  • assistant_configs(user_id) — list_assistant_configs делает
    WHERE user_id = current_user.id. На небольшом числе конфигов
    разница незаметна, но индекс копеечный по размеру и упрощает план.

  • welcome_configs(user_id) — аналогично, для list_welcome_configs.

  • channel_group_pairs(user_id) — аналогично, для list_pairs.

  • bots(user_id) — аналогично, для list_bots.

Revision ID: 012
Revises: 011
Create Date: 2026-04-25
"""
from alembic import op


revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Журнал событий — фильтр по владельцу и сортировка по дате.
    op.create_index(
        "ix_event_log_user_created",
        "event_logs",
        ["user_id", "created_at"],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_event_log_bot_created",
        "event_logs",
        ["bot_id", "created_at"],
        postgresql_using="btree",
    )

    # Запросы верификации — поиск по конфигу и статусу
    # (handlers._handle_member_added, scheduler.kick_expired_verifications).
    op.create_index(
        "ix_verification_welcome_config_status",
        "verification_requests",
        ["welcome_config_id", "status"],
    )
    op.create_index(
        "ix_verification_pair_status",
        "verification_requests",
        ["pair_id", "status"],
    )

    # Расписание публикаций — выборка постов одной пары на странице автопостинга.
    op.create_index(
        "ix_scheduled_post_pair_status",
        "scheduled_posts",
        ["pair_id", "status"],
    )

    # Списочные API: WHERE user_id = …
    op.create_index("ix_assistant_config_user", "assistant_configs", ["user_id"])
    op.create_index("ix_welcome_config_user", "welcome_configs", ["user_id"])
    op.create_index("ix_pair_user", "channel_group_pairs", ["user_id"])
    op.create_index("ix_bot_user", "bots", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_bot_user", table_name="bots")
    op.drop_index("ix_pair_user", table_name="channel_group_pairs")
    op.drop_index("ix_welcome_config_user", table_name="welcome_configs")
    op.drop_index("ix_assistant_config_user", table_name="assistant_configs")
    op.drop_index("ix_scheduled_post_pair_status", table_name="scheduled_posts")
    op.drop_index("ix_verification_pair_status", table_name="verification_requests")
    op.drop_index("ix_verification_welcome_config_status", table_name="verification_requests")
    op.drop_index("ix_event_log_bot_created", table_name="event_logs")
    op.drop_index("ix_event_log_user_created", table_name="event_logs")
