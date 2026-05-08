"""016: поля базы знаний в assistant_configs

Добавляем пять колонок для настройки RAG-пайплайна:
  - embedding_model   — имя модели для создания эмбеддингов
  - embedding_api_url — URL OpenAI-совместимого embedding API
  - embedding_api_key — Fernet-зашифрованный ключ
  - retrieval_top_k   — сколько чанков брать при поиске (default 3)
  - retrieval_threshold — минимальный косинусный порог (default 0.70)
"""
import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assistant_configs", sa.Column("embedding_model", sa.String(128), nullable=True))
    op.add_column("assistant_configs", sa.Column("embedding_api_url", sa.String(512), nullable=True))
    op.add_column("assistant_configs", sa.Column("embedding_api_key", sa.String(512), nullable=True))
    op.add_column(
        "assistant_configs",
        sa.Column("retrieval_top_k", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "assistant_configs",
        sa.Column("retrieval_threshold", sa.Float(), nullable=False, server_default="0.70"),
    )


def downgrade() -> None:
    op.drop_column("assistant_configs", "retrieval_threshold")
    op.drop_column("assistant_configs", "retrieval_top_k")
    op.drop_column("assistant_configs", "embedding_api_key")
    op.drop_column("assistant_configs", "embedding_api_url")
    op.drop_column("assistant_configs", "embedding_model")
