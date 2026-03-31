"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-01-01 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("api_token", sa.String(64), unique=True, nullable=True),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), unique=True),
        sa.Column("plan", sa.String(16), default="free"),
        sa.Column("limits", sa.Text, default='{"max_pairs": 1, "max_posts_per_day": 10}'),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "channel_group_pairs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("channel_name", sa.String(255), default=""),
        sa.Column("group_id", sa.String(64), nullable=False),
        sa.Column("group_name", sa.String(255), default=""),
        sa.Column("group_link", sa.String(512), default=""),
        sa.Column("enabled", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_pair_channel", "channel_group_pairs", ["channel_id", "enabled"])

    op.create_table(
        "post_links",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("pair_id", sa.Integer, sa.ForeignKey("channel_group_pairs.id")),
        sa.Column("channel_post_id", sa.String(64), nullable=False, unique=True),
        sa.Column("group_message_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "polling_markers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("marker", sa.BigInteger, default=0),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "event_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("level", sa.String(16), default="info"),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_log_created", "event_logs", ["created_at"])

    op.create_table(
        "scheduled_posts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("pair_id", sa.Integer, sa.ForeignKey("channel_group_pairs.id")),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("attachments_json", sa.Text, default="[]"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), default="pending"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_scheduled_status_at", "scheduled_posts", ["status", "scheduled_at"])


def downgrade() -> None:
    op.drop_table("scheduled_posts")
    op.drop_table("event_logs")
    op.drop_table("polling_markers")
    op.drop_table("post_links")
    op.drop_table("channel_group_pairs")
    op.drop_table("subscriptions")
    op.drop_table("users")
