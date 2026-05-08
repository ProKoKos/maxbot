"""018: индекс conversation_messages по assistant_config_id

История чата теперь фильтруется по assistant_config_id, чтобы не
было пересечений между разными группами одного бота.
"""
import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_conversation_config",
        "conversation_messages",
        ["bot_id", "max_user_id", "assistant_config_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_config", table_name="conversation_messages")
