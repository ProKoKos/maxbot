"""014: таблица членства пользователей в каналах

Добавляем user_channel_memberships для отслеживания подписок
пользователей MAX на каналы бота в реальном времени (через события
user_added/user_removed с флагом is_channel=True).

Используется для маршрутизации FAQ/KB: при общении в DM бот знает,
в каких каналах состоит пользователь, и подключает соответствующие
базы знаний.

Revision ID: 014
Revises: 013
Create Date: 2026-04-27
"""
import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_channel_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bot_id", sa.Integer(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("max_user_id", sa.String(64), nullable=False),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("channel_title", sa.String(255), nullable=True),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("bot_id", "max_user_id", "channel_id", name="uq_user_channel_membership"),
    )
    op.create_index("ix_user_channel_membership_lookup", "user_channel_memberships", ["bot_id", "max_user_id"])
    op.create_index("ix_user_channel_membership_channel", "user_channel_memberships", ["bot_id", "channel_id"])


def downgrade() -> None:
    op.drop_index("ix_user_channel_membership_channel", table_name="user_channel_memberships")
    op.drop_index("ix_user_channel_membership_lookup", table_name="user_channel_memberships")
    op.drop_table("user_channel_memberships")
