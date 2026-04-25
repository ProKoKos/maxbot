"""user_bot_contexts: add group_id, FK assistant_config_id SET NULL

Revision ID: 007
Revises: 006
Create Date: 2026-04-25 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_bot_contexts",
        sa.Column("group_id", sa.String(64), nullable=False, server_default=""),
    )
    op.drop_constraint(
        "user_bot_contexts_assistant_config_id_fkey",
        "user_bot_contexts",
        type_="foreignkey",
    )
    op.alter_column("user_bot_contexts", "assistant_config_id", nullable=True)
    op.create_foreign_key(
        "user_bot_contexts_assistant_config_id_fkey",
        "user_bot_contexts",
        "assistant_configs",
        ["assistant_config_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "user_bot_contexts_assistant_config_id_fkey",
        "user_bot_contexts",
        type_="foreignkey",
    )
    op.alter_column("user_bot_contexts", "assistant_config_id", nullable=False)
    op.create_foreign_key(
        "user_bot_contexts_assistant_config_id_fkey",
        "user_bot_contexts",
        "assistant_configs",
        ["assistant_config_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_column("user_bot_contexts", "group_id")
