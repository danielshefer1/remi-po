"""Repeating reminders (issue #52): a series is a chain of ``reminders`` rows.

* ``reminders.rule`` (an RRULE without ``COUNT``, ``NULL`` for a one-off) and
  ``reminders.series_id`` (the first row's id, ``NULL`` on that first row).
* ``due_reminders`` keeps a stale repeating row in its result, so the tick can skip it and
  schedule the next occurrence. A one-off still drops out after 12 hours. Same ``SECURITY
  DEFINER`` function as 0007; ``CREATE OR REPLACE`` keeps its owner and grants.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_reminder_series"
down_revision: str | None = "0009_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DUE_REMINDERS = """
CREATE OR REPLACE FUNCTION due_reminders(p_now timestamptz)
RETURNS TABLE (reminder_id uuid, tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT r.id, r.tenant_id FROM reminders r JOIN tenants t ON t.id = r.tenant_id
    WHERE r.status = 'pending' AND t.status = 'active'
      AND r.due_at <= p_now AND {window}
    ORDER BY r.due_at
    LIMIT 500
$$
"""
_WINDOW_0007 = "r.due_at > p_now - interval '12 hours'"
_WINDOW_0010 = "(r.due_at > p_now - interval '12 hours' OR r.rule IS NOT NULL)"


def upgrade() -> None:
    op.add_column("reminders", sa.Column("rule", sa.Text(), nullable=True))
    op.add_column("reminders", sa.Column("series_id", sa.Uuid(), nullable=True))
    op.execute(_DUE_REMINDERS.format(window=_WINDOW_0010))


def downgrade() -> None:
    op.execute(_DUE_REMINDERS.format(window=_WINDOW_0007))
    op.drop_column("reminders", "series_id")
    op.drop_column("reminders", "rule")
