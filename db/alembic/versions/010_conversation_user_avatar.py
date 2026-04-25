"""conversation_messages: добавить user_avatar для отображения фото пользователя

Revision ID: 010
Revises: 009
Create Date: 2026-04-25 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("user_avatar", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation_messages", "user_avatar")
