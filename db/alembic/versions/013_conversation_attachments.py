"""013: вложения в сообщениях DM

Добавляем колонку attachments_json в conversation_messages для хранения
медиа/файловых вложений, которые пользователь отправил боту в личном диалоге.

Формат — JSON-массив объектов, совместимый с MAX API:
  [{"type": "image", "token": "...", "preview_url": "..."}, ...]
Хранится как Text (а не JSONB) для единообразия с ScheduledPost.attachments_json.

Revision ID: 013
Revises: 012
Create Date: 2026-04-26
"""
import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("attachments_json", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("conversation_messages", "attachments_json")
