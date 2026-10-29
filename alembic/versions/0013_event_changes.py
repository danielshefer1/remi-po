"""Change and delete events (issue #63): ``series_id``, ``original_start``, ``skipped`` and
``invited`` on ``events``.

* ``series_id``: the series a native single-date row or a Google instance belongs to.
* ``original_start``: where that date fell before it moved.
* ``skipped``: occurrence starts a series leaves out.
* ``invited``: the owner was invited, not the organizer. A bool only; no attendee is stored.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0013_event_changes"
down_revision: str | None = "0012_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("events", sa.Column("series_id", sa.Text(), nullable=True))
    op.add_column("events", sa.Column("original_start", _TZ, nullable=True))
    op.add_column(
        "events",
        sa.Column("skipped", ARRAY(_TZ), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "events",
        sa.Column("invited", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    for name in ("invited", "skipped", "original_start", "series_id"):
        op.drop_column("events", name)
