"""Family groups (Iteration 07): groups, who is in them, detail grants, outings and answers.

* ``channel_inbox.group_id`` and ``channel_inbox.sender_name``: where a message was sent, and
  who by name. The group gate's four dispositions.
* ``wa_groups``, ``group_context`` and ``group_polls``: not tenant-scoped, like
  ``channel_inbox``. A group exists before anyone in it is known.
* ``group_members``, ``detail_grants`` and ``poll_answers`` under the tenant RLS policy, and
  ``tenants.display_name``.
* ``group_tenants``, ``peers_of``, ``detail_grantors`` and ``due_nudges``: ``SECURITY
  DEFINER``, owned by ``app_definer``, callable by ``app_user`` only, the same DDL order as
  0004's functions. A group turn has no single tenant GUC that could read its members, and
  the tick has none at all, so these are the one way across; they return ids (and whether a
  member shares), never content.

Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from rls import disable_tenant_rls, enable_tenant_rls

from personal_organizer.db.roles import DEFINER_ROLE_NAME

revision: str = "0009_groups"
down_revision: str | None = "0008_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)
_GEN_UUID = sa.text("gen_random_uuid()")
_TENANT_TABLES = ("group_members", "detail_grants", "poll_answers")

_DISPOSITIONS_0004 = (
    "disposition IN ('allowed', 'stranger', 'stranger_muted', 'stale', 'onboarding')"
)
_DISPOSITIONS_0009 = (
    "disposition IN ('allowed', 'stranger', 'stranger_muted', 'stale', 'onboarding', "
    "'group', 'group_unserved', 'group_stale', 'group_off')"
)

_GROUP_TENANTS = """
CREATE FUNCTION group_tenants(p_group uuid)
RETURNS TABLE (tenant_id uuid, sharing boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT m.tenant_id, m.sharing FROM group_members m JOIN tenants t ON t.id = m.tenant_id
    WHERE m.group_id = p_group AND t.status = 'active'
    ORDER BY m.seen_at, m.tenant_id
$$
"""

_PEERS_OF = """
CREATE FUNCTION peers_of(p_tenant uuid)
RETURNS TABLE (tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT DISTINCT o.tenant_id
    FROM group_members me
    JOIN group_members o ON o.group_id = me.group_id AND o.tenant_id <> me.tenant_id
    JOIN tenants t ON t.id = o.tenant_id
    WHERE me.tenant_id = p_tenant AND t.status = 'active'
$$
"""

# A grant alone opens nothing: the owner must also be sharing in this very group.
_DETAIL_GRANTORS = """
CREATE FUNCTION detail_grantors(p_viewer uuid, p_group uuid)
RETURNS TABLE (tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT g.tenant_id
    FROM detail_grants g
    JOIN group_members m ON m.tenant_id = g.tenant_id AND m.group_id = p_group AND m.sharing
    JOIN tenants t ON t.id = g.tenant_id
    WHERE g.viewer_tenant_id = p_viewer AND t.status = 'active'
$$
"""

# The LEFT JOIN's NULLs make "no answer row" and "a row with neither answer nor reason" alike.
_DUE_NUDGES = """
CREATE FUNCTION due_nudges(p_now timestamptz)
RETURNS TABLE (poll_id uuid, tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT p.id, m.tenant_id
    FROM group_polls p
    JOIN group_members m ON m.group_id = p.group_id AND m.sharing
    JOIN tenants t ON t.id = m.tenant_id
    LEFT JOIN poll_answers a ON a.poll_id = p.id AND a.tenant_id = m.tenant_id
    WHERE p.status = 'open' AND p.starts_at > p_now
      AND p.created_at <= p_now - interval '3 hours'
      AND t.status = 'active' AND t.timezone IS NOT NULL
      AND (p_now AT TIME ZONE t.timezone)::time >= time '10:00'
      AND (p_now AT TIME ZONE t.timezone)::time < time '20:00'
      AND a.answer IS NULL AND a.reason IS NULL
      AND (a.nudged_at IS NULL OR a.nudged_at < p_now - interval '20 hours')
    ORDER BY p.starts_at
    LIMIT 500
$$
"""

_FUNCTIONS = (
    ("group_tenants(uuid)", _GROUP_TENANTS),
    ("peers_of(uuid)", _PEERS_OF),
    ("detail_grantors(uuid, uuid)", _DETAIL_GRANTORS),
    ("due_nudges(timestamptz)", _DUE_NUDGES),
)


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False)


def _group_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["group_id"], ["wa_groups.id"], name=f"fk_{table}_group_id_wa_groups", ondelete="CASCADE"
    )


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=f"fk_{table}_tenant_id_tenants", ondelete="CASCADE"
    )


def _create_group_tables() -> None:
    op.create_table(
        "wa_groups",
        _id(),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("jid", sa.Text(), nullable=False),
        sa.Column("served_at", _TZ, nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_wa_groups"),
        sa.UniqueConstraint("channel", "jid", name="uq_wa_groups_channel_jid"),
    )
    op.create_table(
        "group_context",
        _id(),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("sender_name", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sent_at", _TZ, nullable=False),
        sa.Column("inbox_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_group_context"),
        _group_fk("group_context"),
        sa.UniqueConstraint("inbox_id", name="uq_group_context_inbox_id"),
    )
    op.create_index("ix_group_context_group_id_sent_at", "group_context", ["group_id", "sent_at"])
    op.create_table(
        "group_polls",
        _id(),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("inbox_id", sa.Uuid(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", _TZ, nullable=False),
        sa.Column("ends_at", _TZ, nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("in_option", sa.Text(), nullable=False),
        sa.Column("out_option", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_group_polls"),
        _group_fk("group_polls"),
        sa.UniqueConstraint("inbox_id", name="uq_group_polls_inbox_id"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_group_polls_status"),
    )


def _create_tenant_tables() -> None:
    op.create_table(
        "group_members",
        _id(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("sharing", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("seen_at", _TZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_group_members"),
        _tenant_fk("group_members"),
        _group_fk("group_members"),
        sa.UniqueConstraint("tenant_id", "group_id", name="uq_group_members_tenant_id_group_id"),
    )
    op.create_table(
        "detail_grants",
        _id(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("viewer_tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_detail_grants"),
        _tenant_fk("detail_grants"),
        sa.ForeignKeyConstraint(
            ["viewer_tenant_id"],
            ["tenants.id"],
            name="fk_detail_grants_viewer_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id", "viewer_tenant_id", name="uq_detail_grants_tenant_id_viewer_tenant_id"
        ),
    )
    op.create_table(
        "poll_answers",
        _id(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("poll_id", sa.Uuid(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("claimed_at", _TZ, nullable=True),
        sa.Column("nudged_at", _TZ, nullable=True),
        sa.Column("updated_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_poll_answers"),
        _tenant_fk("poll_answers"),
        sa.ForeignKeyConstraint(
            ["poll_id"],
            ["group_polls.id"],
            name="fk_poll_answers_poll_id_group_polls",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "poll_id", name="uq_poll_answers_tenant_id_poll_id"),
        sa.CheckConstraint("answer IN ('in', 'out')", name="ck_poll_answers_answer"),
    )
    for table in _TENANT_TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        enable_tenant_rls(table)


def upgrade() -> None:
    op.add_column("channel_inbox", sa.Column("group_id", sa.Text(), nullable=True))
    op.add_column("channel_inbox", sa.Column("sender_name", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("display_name", sa.Text(), nullable=True))

    _create_group_tables()
    _create_tenant_tables()

    tables = "group_members, detail_grants, group_polls, poll_answers"
    op.execute(f'GRANT SELECT ON {tables} TO "{DEFINER_ROLE_NAME}"')
    for signature, ddl in _FUNCTIONS:
        op.execute(ddl)
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f'ALTER FUNCTION {signature} OWNER TO "{DEFINER_ROLE_NAME}"')

    op.drop_constraint("ck_channel_inbox_disposition", "channel_inbox", type_="check")
    op.create_check_constraint("ck_channel_inbox_disposition", "channel_inbox", _DISPOSITIONS_0009)


def downgrade() -> None:
    # 0004's check knows no group dispositions. Every group row was purged unanswered, which is
    # what 'stranger' records.
    op.execute(
        "UPDATE channel_inbox SET disposition = 'stranger' "
        "WHERE disposition IN ('group', 'group_unserved', 'group_stale', 'group_off')"
    )
    op.drop_constraint("ck_channel_inbox_disposition", "channel_inbox", type_="check")
    op.create_check_constraint("ck_channel_inbox_disposition", "channel_inbox", _DISPOSITIONS_0004)

    for signature, _ in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")
    for table in reversed(_TENANT_TABLES):
        disable_tenant_rls(table)
        op.drop_table(table)
    for table in ("group_polls", "group_context", "wa_groups"):
        op.drop_table(table)
    op.drop_column("tenants", "display_name")
    op.drop_column("channel_inbox", "sender_name")
    op.drop_column("channel_inbox", "group_id")
