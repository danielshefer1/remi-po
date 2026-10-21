"""Family groups (Iteration 07): where a message was sent and who by name.

Task 1 adds the two ``channel_inbox`` columns; later tasks extend this revision.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_groups"
down_revision: str | None = "0008_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channel_inbox", sa.Column("group_id", sa.Text(), nullable=True))
    op.add_column("channel_inbox", sa.Column("sender_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("channel_inbox", "sender_name")
    op.drop_column("channel_inbox", "group_id")
