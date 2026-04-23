"""add standalone welcome_configs table

Revision ID: 005
Revises: 004
Create Date: 2026-04-23 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Create welcome_configs table ─────────────────────────────────────────
    op.create_table(
        "welcome_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.String(64), nullable=False),
        sa.Column("group_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("group_link", sa.String(512), nullable=False, server_default=""),
        sa.Column("verification_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verification_timeout_min", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("verification_message", sa.Text(), nullable=True),
        sa.Column("verification_button_text", sa.String(255), nullable=True),
        sa.Column("verification_kick", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verification_notify_success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verification_welcome_dm", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "group_id", name="uq_welcome_config_bot_group"),
    )
    op.create_index("ix_welcome_config_bot_group", "welcome_configs", ["bot_id", "group_id"])

    # ── Update verification_requests ─────────────────────────────────────────
    # Make pair_id nullable (was NOT NULL)
    op.alter_column("verification_requests", "pair_id", nullable=True)

    # Add welcome_config_id FK (nullable)
    op.add_column(
        "verification_requests",
        sa.Column("welcome_config_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_vr_welcome_config_id",
        "verification_requests", "welcome_configs",
        ["welcome_config_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_vr_welcome_config_id", "verification_requests", ["welcome_config_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_vr_welcome_config_id", table_name="verification_requests")
    op.drop_constraint("fk_vr_welcome_config_id", "verification_requests", type_="foreignkey")
    op.drop_column("verification_requests", "welcome_config_id")
    op.alter_column("verification_requests", "pair_id", nullable=False)

    op.drop_index("ix_welcome_config_bot_group", table_name="welcome_configs")
    op.drop_table("welcome_configs")
