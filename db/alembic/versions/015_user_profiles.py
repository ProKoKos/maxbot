"""015: таблица профилей пользователей MAX

Добавляем user_profiles для хранения актуальных данных профиля каждого
пользователя: имя, фамилия, @username, биография, аватар.

Данные обновляются при каждом входящем DM — поэтому всегда актуальны.
До этой миграции имя бралось из VerificationRequest (не обновлялось),
а аватар — из последнего ConversationMessage (без имени и биографии).

Revision ID: 015
Revises: 014
Create Date: 2026-04-27
"""
import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bot_id", sa.Integer(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("max_user_id", sa.String(64), nullable=False),
        sa.Column("first_name", sa.String(255), nullable=True),
        sa.Column("last_name", sa.String(255), nullable=True),
        sa.Column("username", sa.String(128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.String(512), nullable=True),
        sa.Column("full_avatar_url", sa.String(512), nullable=True),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("bot_id", "max_user_id", name="uq_user_profile"),
    )
    op.create_index("ix_user_profile_lookup", "user_profiles", ["bot_id", "max_user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_profile_lookup", table_name="user_profiles")
    op.drop_table("user_profiles")
