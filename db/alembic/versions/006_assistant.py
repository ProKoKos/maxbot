"""add assistant_configs, user_bot_contexts, conversation_messages

Revision ID: 006
Revises: 005
Create Date: 2026-04-24 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.String(64), nullable=False),
        sa.Column("group_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=False, server_default="llama-3.3-70b-versatile"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "group_id", name="uq_assistant_config_bot_group"),
    )
    op.create_index("ix_assistant_config_bot_group", "assistant_configs", ["bot_id", "group_id"])

    op.create_table(
        "user_bot_contexts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=False),
        sa.Column("max_user_id", sa.String(64), nullable=False),
        sa.Column("assistant_config_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assistant_config_id"], ["assistant_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "max_user_id", "assistant_config_id", name="uq_user_bot_context"),
    )
    op.create_index("ix_user_bot_context_lookup", "user_bot_contexts", ["bot_id", "max_user_id"])

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=False),
        sa.Column("max_user_id", sa.String(64), nullable=False),
        sa.Column("assistant_config_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assistant_config_id"], ["assistant_configs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversation_lookup", "conversation_messages", ["bot_id", "max_user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_conversation_lookup", table_name="conversation_messages")
    op.drop_table("conversation_messages")

    op.drop_index("ix_user_bot_context_lookup", table_name="user_bot_contexts")
    op.drop_table("user_bot_contexts")

    op.drop_index("ix_assistant_config_bot_group", table_name="assistant_configs")
    op.drop_table("assistant_configs")
