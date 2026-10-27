"""Okaying calendar sharing in the group (issue #51): ``group_members.asked_at``.

When Remi can't see a member's calendar it asks them, in the group, to okay it; ``asked_at`` is
when. An "ok" counts only while the ask is open, and sharing (or stopping) clears it.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_group_asks"
down_revision: str | None = "0010_reminder_series"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("group_members", sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("group_members", "asked_at")
