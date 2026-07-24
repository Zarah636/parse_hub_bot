"""Merge the legacy private revision with the current upstream migration."""

from collections.abc import Sequence

revision: str = "f71c0e5d2a94"
down_revision: str | Sequence[str] | None = ("b3a0e10df4b2", "6c83cf8af214")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
