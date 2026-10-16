"""Notes: what the agent remembers about a person (Iteration 06).

``notes`` under the tenant RLS policy, like every tenant table. Read only inside a
``tenant_session``, so no definer function is needed.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0008_notes"
down_revision: str | None = "0007_reminders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notes"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_notes_tenant_id_tenants", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_notes_tenant_id", "notes", ["tenant_id"])
    enable_tenant_rls("notes")


def downgrade() -> None:
    disable_tenant_rls("notes")
    op.drop_table("notes")
