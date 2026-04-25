"""conversation_messages: добавить chat_id для отправки DM

Revision ID: 009
Revises: 008
Create Date: 2026-04-25 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("chat_id", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation_messages", "chat_id")
