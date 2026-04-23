"""add channel_link to channel_group_pairs

Revision ID: 003
Revises: 002
Create Date: 2026-04-22 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_group_pairs",
        sa.Column("channel_link", sa.String(512), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("channel_group_pairs", "channel_link")
