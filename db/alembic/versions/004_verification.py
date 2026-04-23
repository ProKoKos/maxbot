"""add verification support

Revision ID: 004
Revises: 003
Create Date: 2026-04-23 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Новые колонки в channel_group_pairs ───────────────────────────────────
    op.add_column(
        "channel_group_pairs",
        sa.Column("verification_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "channel_group_pairs",
        sa.Column("verification_timeout_min", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "channel_group_pairs",
        sa.Column("verification_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "channel_group_pairs",
        sa.Column("verification_button_text", sa.String(255), nullable=True),
    )
    op.add_column(
        "channel_group_pairs",
        sa.Column("verification_kick", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "channel_group_pairs",
        sa.Column("verification_notify_success", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "channel_group_pairs",
        sa.Column("verification_welcome_dm", sa.Text(), nullable=True),
    )

    # ── Таблица запросов верификации ──────────────────────────────────────────
    op.create_table(
        "verification_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("max_user_id", sa.String(64), nullable=False),
        sa.Column("user_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("group_message_id", sa.String(64), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["pair_id"], ["channel_group_pairs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_verification_token"),
    )
    op.create_index("ix_verification_token", "verification_requests", ["token"])
    op.create_index(
        "ix_verification_status_deadline", "verification_requests", ["status", "deadline"]
    )


def downgrade() -> None:
    op.drop_index("ix_verification_status_deadline", table_name="verification_requests")
    op.drop_index("ix_verification_token", table_name="verification_requests")
    op.drop_table("verification_requests")
    for col in (
        "verification_welcome_dm",
        "verification_notify_success",
        "verification_kick",
        "verification_button_text",
        "verification_message",
        "verification_timeout_min",
        "verification_enabled",
    ):
        op.drop_column("channel_group_pairs", col)
