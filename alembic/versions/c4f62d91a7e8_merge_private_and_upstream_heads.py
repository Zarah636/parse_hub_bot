"""Merge the private compatibility and upstream cache-cleanup heads.

Revision ID: c4f62d91a7e8
Revises: f71c0e5d2a94, 9478f8c31350
Create Date: 2026-08-31
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c4f62d91a7e8"
down_revision: str | Sequence[str] | None = ("f71c0e5d2a94", "9478f8c31350")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge both migration branches without changing the schema."""
    pass


def downgrade() -> None:
    """Split the migration graph without changing the schema."""
    pass
