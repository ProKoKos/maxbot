"""assistant_configs: add api_url and api_key

Revision ID: 008
Revises: 007
Create Date: 2026-04-25 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None

DEFAULT_URL = "https://openrouter.ai/api/v1"


def upgrade() -> None:
    op.add_column(
        "assistant_configs",
        sa.Column("api_url", sa.String(512), nullable=False, server_default=DEFAULT_URL),
    )
    op.add_column(
        "assistant_configs",
        sa.Column("api_key", sa.String(512), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("assistant_configs", "api_key")
    op.drop_column("assistant_configs", "api_url")
