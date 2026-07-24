"""Recognize the legacy private media-publications revision.

Older private deployments recorded revision ``6c83cf8af214`` after adding a
publication-history table.  This upstream-tracking branch no longer owns that
feature, but Alembic must still recognize the recorded revision so it can apply
new upstream migrations.  The table itself is intentionally left untouched.
"""

from collections.abc import Sequence

revision: str = "6c83cf8af214"
down_revision: str | Sequence[str] | None = "a43d3810bad8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
