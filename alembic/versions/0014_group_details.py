"""Sharing event details with a group (issue #82): ``events.private`` and
``group_members.show_details``.

* ``events.private``: Google marks the event private. Nullable, with no backfill: a ``NULL`` on
  a ``google`` row (copied before this, or after a disconnect) reads as private until the next
  sync says otherwise, so nothing private is ever shown by mistake. ``remi`` rows are never private.
* ``group_members.show_details``: the member okayed showing event titles in that group.

Both tables are already under RLS (migrations 0012 and 0009), so no policy is added.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_group_details"
down_revision: str | None = "0013_event_changes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("private", sa.Boolean(), nullable=True))
    op.add_column(
        "group_members",
        sa.Column("show_details", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("group_members", "show_details")
    op.drop_column("events", "private")
