"""inbox_read_status: отслеживание прочитанных сообщений в инбоксе

Revision ID: 011
Revises: 010
Create Date: 2026-04-25 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_read_status",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=False),
        sa.Column("max_user_id", sa.String(64), nullable=False),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "max_user_id", name="uq_inbox_read_bot_user"),
    )
    op.create_index("ix_inbox_read_bot_user", "inbox_read_status", ["bot_id", "max_user_id"])


def downgrade() -> None:
    op.drop_index("ix_inbox_read_bot_user", table_name="inbox_read_status")
    op.drop_table("inbox_read_status")
