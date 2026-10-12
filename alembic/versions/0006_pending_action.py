"""One pending write per tenant, waiting for the person's confirm (Iteration 04, D5).

``tenants.pending_action`` holds ``{id, kind, payload, expires_at}``, or ``NULL`` when
nothing waits. A column, not a table: there is at most one, and ``tenants``' RLS policy
already covers it. Claiming it is one ``UPDATE`` that nulls it
(``personal_organizer.db.repositories.pending.claim``), so a retry or a double "1" runs the
write at most once (ADR 0003).

Hand-written to match ``personal_organizer.db.models.tenant``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_pending_action"
down_revision: str | None = "0005_invites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("pending_action", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "pending_action")
