"""Reminders, the morning-briefing setting, and the two scans the tick reads (Iteration 05).

* ``reminders`` under the tenant RLS policy, like every tenant table.
* ``tenants.briefing_at`` (local time, default ``07:30``, ``NULL`` is off) and
  ``tenants.briefed_on`` (the last local day a briefing went out). Existing tenants get the
  default too.
* ``due_reminders`` and ``due_briefings``: ``SECURITY DEFINER``, owned by ``app_definer``,
  callable by ``app_user`` only, the same DDL order as 0004's functions. The tick runs with no
  tenant GUC, so these are its one way to find what is due; they return ids only, never
  content.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from rls import disable_tenant_rls, enable_tenant_rls

from personal_organizer.db.roles import DEFINER_ROLE_NAME

revision: str = "0007_reminders"
down_revision: str | None = "0006_pending_action"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)

# The 12-hour cut-off keeps a reminder that cannot be sent from being picked up forever.
_DUE_REMINDERS = """
CREATE FUNCTION due_reminders(p_now timestamptz)
RETURNS TABLE (reminder_id uuid, tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT r.id, r.tenant_id FROM reminders r JOIN tenants t ON t.id = r.tenant_id
    WHERE r.status = 'pending' AND t.status = 'active'
      AND r.due_at <= p_now AND r.due_at > p_now - interval '12 hours'
    ORDER BY r.due_at
    LIMIT 500
$$
"""

# local::time - briefing_at, not briefing_at + interval: time arithmetic wraps at midnight.
_DUE_BRIEFINGS = """
CREATE FUNCTION due_briefings(p_now timestamptz)
RETURNS TABLE (tenant_id uuid, local_day date)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT t.id, (p_now AT TIME ZONE t.timezone)::date
    FROM tenants t
    WHERE t.status = 'active' AND t.briefing_at IS NOT NULL AND t.timezone IS NOT NULL
      AND (p_now AT TIME ZONE t.timezone)::time >= t.briefing_at
      AND (p_now AT TIME ZONE t.timezone)::time - t.briefing_at < interval '2 hours'
      AND t.briefed_on IS DISTINCT FROM (p_now AT TIME ZONE t.timezone)::date
$$
"""

_FUNCTIONS = (
    ("due_reminders(timestamptz)", _DUE_REMINDERS),
    ("due_briefings(timestamptz)", _DUE_BRIEFINGS),
)


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("briefing_at", sa.Time(), server_default=sa.text("'07:30'"), nullable=True),
    )
    op.add_column("tenants", sa.Column("briefed_on", sa.Date(), nullable=True))

    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_at", _TZ, nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_reminders"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_reminders_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'cancelled', 'failed')", name="ck_reminders_status"
        ),
    )
    op.create_index("ix_reminders_tenant_id", "reminders", ["tenant_id"])
    op.create_index("ix_reminders_status_due_at", "reminders", ["status", "due_at"])
    enable_tenant_rls("reminders")

    op.execute(f'GRANT SELECT ON reminders TO "{DEFINER_ROLE_NAME}"')
    for signature, ddl in _FUNCTIONS:
        op.execute(ddl)
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f'ALTER FUNCTION {signature} OWNER TO "{DEFINER_ROLE_NAME}"')


def downgrade() -> None:
    for signature, _ in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")
    disable_tenant_rls("reminders")
    op.drop_table("reminders")
    op.drop_column("tenants", "briefed_on")
    op.drop_column("tenants", "briefing_at")
