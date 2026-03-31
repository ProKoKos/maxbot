"""multi-bot support: bots table, bot_id on pairs and markers

Revision ID: 002
Revises: 001
Create Date: 2026-01-02 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── New: bots table ───────────────────────────────────────────────────────
    op.create_table(
        "bots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("encrypted_token", sa.Text, nullable=False),
        sa.Column("max_user_id", sa.String(64), nullable=True),
        sa.Column("max_username", sa.String(128), nullable=True),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_bots_user", "bots", ["user_id"])

    # ── Modify: channel_group_pairs — add bot_id (nullable, SET NULL on delete) ─
    op.add_column(
        "channel_group_pairs",
        sa.Column("bot_id", sa.Integer,
                  sa.ForeignKey("bots.id", ondelete="SET NULL"), nullable=True),
    )
    # Drop old index, recreate with bot_id
    op.drop_index("ix_pair_channel", table_name="channel_group_pairs")
    op.create_index("ix_pair_bot_channel", "channel_group_pairs", ["bot_id", "channel_id", "enabled"])

    # ── Modify: polling_markers — replace single-row with per-bot ─────────────
    # Drop the old table (it had a single row with no bot_id)
    op.drop_table("polling_markers")
    op.create_table(
        "polling_markers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("bot_id", sa.Integer,
                  sa.ForeignKey("bots.id", ondelete="CASCADE"),
                  unique=True, nullable=False),
        sa.Column("marker", sa.BigInteger, default=0),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── Modify: event_logs — add bot_id for context ───────────────────────────
    op.add_column(
        "event_logs",
        sa.Column("bot_id", sa.Integer,
                  sa.ForeignKey("bots.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("event_logs", "bot_id")

    op.drop_table("polling_markers")
    op.create_table(
        "polling_markers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("marker", sa.BigInteger, default=0),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.drop_index("ix_pair_bot_channel", table_name="channel_group_pairs")
    op.create_index("ix_pair_channel", "channel_group_pairs", ["channel_id", "enabled"])
    op.drop_column("channel_group_pairs", "bot_id")

    op.drop_index("ix_bots_user", table_name="bots")
    op.drop_table("bots")
