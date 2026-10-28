"""Remi's own calendar (issue #50): ``events``, ``tenants.live_calendar``,
``calendar_connections.synced_at`` and the ``due_syncs`` scan.

* ``events`` under the tenant RLS policy. ``source`` is ``remi`` (made here) or ``google``
  (imported); ``external_id`` is unique per tenant, so an import upserts. ``rule`` is unused
  until #53.
* ``due_syncs``: ``SECURITY DEFINER``, owned by ``app_definer``, callable by ``app_user`` only,
  the same DDL order as 0007's functions. It returns tenant ids only, never content.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from rls import disable_tenant_rls, enable_tenant_rls

from personal_organizer.db.roles import DEFINER_ROLE_NAME

revision: str = "0012_events"
down_revision: str | None = "0011_group_asks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)

_DUE_SYNCS = """
CREATE FUNCTION due_syncs(p_now timestamptz, p_every interval)
RETURNS TABLE (tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT t.id FROM tenants t JOIN calendar_connections c ON c.tenant_id = t.id
    WHERE t.status = 'active' AND c.status = 'active'
      AND (c.synced_at IS NULL OR c.synced_at <= p_now - p_every)
    ORDER BY c.synced_at NULLS FIRST
    LIMIT 200
$$
"""
_SIGNATURE = "due_syncs(timestamptz, interval)"


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("live_calendar", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("calendar_connections", sa.Column("synced_at", _TZ, nullable=True))

    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", _TZ, nullable=False),
        sa.Column("ends_at", _TZ, nullable=False),
        sa.Column("all_day", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("rule", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_events_tenant_id_tenants", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("tenant_id", "external_id", name="uq_events_tenant_id_external_id"),
        sa.CheckConstraint("source IN ('remi', 'google')", name="ck_events_source"),
    )
    op.create_index("ix_events_tenant_id", "events", ["tenant_id"])
    enable_tenant_rls("events")

    op.execute(f'GRANT SELECT ON calendar_connections TO "{DEFINER_ROLE_NAME}"')
    op.execute(_DUE_SYNCS)
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f'ALTER FUNCTION {_SIGNATURE} OWNER TO "{DEFINER_ROLE_NAME}"')


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
    op.execute(f'REVOKE SELECT ON calendar_connections FROM "{DEFINER_ROLE_NAME}"')
    disable_tenant_rls("events")
    op.drop_table("events")
    op.drop_column("calendar_connections", "synced_at")
    op.drop_column("tenants", "live_calendar")
