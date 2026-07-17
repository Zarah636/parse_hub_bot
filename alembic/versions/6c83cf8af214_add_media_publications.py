"""add media publications

Revision ID: 6c83cf8af214
Revises: a43d3810bad8
Create Date: 2026-07-18 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6c83cf8af214"
down_revision: str | Sequence[str] | None = "a43d3810bad8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_publications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("content_key", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_thread_id", sa.BigInteger(), nullable=False),
        sa.Column("topic_title", sa.Text(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_link", sa.Text(), nullable=True),
        sa.Column("requester_user_id", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_key", "chat_id", name="uq_media_publication_chat"),
    )
    op.create_index(
        "ix_media_publication_chat",
        "media_publications",
        ["chat_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_media_publication_chat", table_name="media_publications")
    op.drop_table("media_publications")
