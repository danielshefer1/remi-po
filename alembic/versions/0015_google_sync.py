"""Syncing with Google is opt-in (issue #95): ``tenants.google_sync`` and ``due_syncs``.

* ``tenants.google_sync``: Google's events are copied in hourly, and what Remi adds goes to
  Google, only once the member asks for it. Connecting still imports once. Off by default; a
  member already reading Google live asked for Google, so they start on.
* ``due_syncs`` gains ``AND t.google_sync``, replaced in place: same owner, grants and signature.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_google_sync"
down_revision: str | None = "0014_group_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DUE_SYNCS = """
CREATE OR REPLACE FUNCTION due_syncs(p_now timestamptz, p_every interval)
RETURNS TABLE (tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT t.id FROM tenants t JOIN calendar_connections c ON c.tenant_id = t.id
    WHERE t.status = 'active' AND c.status = 'active'{sync}
      AND (c.synced_at IS NULL OR c.synced_at <= p_now - p_every)
    ORDER BY c.synced_at NULLS FIRST
    LIMIT 200
$$
"""


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("google_sync", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE tenants SET google_sync = true WHERE live_calendar")
    op.execute(_DUE_SYNCS.format(sync=" AND t.google_sync"))


def downgrade() -> None:
    op.execute(_DUE_SYNCS.format(sync=""))
    op.drop_column("tenants", "google_sync")
