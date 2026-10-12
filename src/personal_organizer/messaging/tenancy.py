"""Who a message is from, as a tenant: the network-keyed identities of D12.

``tenant_identities`` is keyed by **network**, not by gateway. Meta's ``whatsapp`` channel and
the ``gowa`` gateway are both the network ``whatsapp``, so one person writing to either
number is one tenant (ADR 0004's "one person, one key"). A key is ``SenderRef.key``: ``uid:``
plus Meta's BSUID, or ``tel:`` plus the E.164 number.

A sender is looked up by every key their message carries, strongest first. The ``uid:`` key
survives a user hiding their number behind a username, and ``tel:`` is what the gateway and
the invite list know. Two consequences:

- Found by ``tel:`` while the message also carried a ``uid:`` (a Meta BSUID arriving for a
  number first seen on the gateway): the ``uid:`` key is added to that tenant, so their next
  message resolves even with no number in it.
- A new tenant is created on the ``tel:`` key when the sender has a phone (the strongest key
  otherwise), and every key not yet recorded is linked at once. Enrolling through Meta and
  writing later through the gateway is therefore still one person. A key owned by another
  tenant is skipped and logged as ``tenant.identity_conflict``.

Resolution and creation run as ``app_user`` through the two ``SECURITY DEFINER`` functions
(D1). The worker cannot read ``tenant_identities`` before it knows the tenant, by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP
from personal_organizer.db.repositories.invites import mark_used
from personal_organizer.db.repositories.messages import latest_inbound_channel, recent_messages
from personal_organizer.db.repositories.tenants import (
    add_identity,
    create_tenant,
    get_tenant,
    primary_phone,
    resolve_tenant,
)
from personal_organizer.interfaces.llm import Message
from personal_organizer.messaging.inbox import InboxRow

log = structlog.get_logger(__name__)

#: How many recorded messages, both directions, an LLM turn sees (D4).
HISTORY_LIMIT: Final = 20

#: Channel name -> identity network. A future Telegram channel maps to ``"telegram"``.
_NETWORKS: Final = MappingProxyType({"whatsapp": NETWORK_WHATSAPP, "gowa": NETWORK_WHATSAPP})


@dataclass(frozen=True, slots=True)
class TenantState:
    """What the gate and the onboarding steps decide from, read once per job."""

    id: UUID
    status: str
    step: str | None
    language: str
    #: No inbound message of theirs is recorded yet, so this one is their first. Recording
    #: happens in the transaction that finishes a row (D7), so a job that died before
    #: finishing still counts as the first message when it re-runs.
    first_message: bool
    #: Set on the ``zone`` step, so every active tenant has one.
    timezone: str | None = None


def network_for(channel: str) -> str:
    try:
        return _NETWORKS[channel]
    except KeyError:
        msg = f"no identity network for channel {channel!r}"
        raise ValueError(msg) from None


def identity_keys(row: InboxRow) -> tuple[str, ...]:
    """The row's keys, strongest first. The first is always ``row.sender_key``."""
    keys: list[str] = []
    if row.sender_user_id:
        keys.append(f"uid:{row.sender_user_id}")
    if row.sender_phone:
        keys.append(f"tel:{row.sender_phone}")
    return tuple(keys)


async def _first_match(
    session: AsyncSession, network: str, keys: tuple[str, ...]
) -> tuple[UUID, int] | None:
    for index, key in enumerate(keys):
        tenant_id = await resolve_tenant(session, network=network, external_id=key)
        if tenant_id is not None:
            return tenant_id, index
    return None


async def _link(db: Database, tenant_id: UUID, row: InboxRow, keys: tuple[str, ...]) -> None:
    """Add ``keys`` to the tenant. A key that exists and is *not* this tenant's is logged."""
    network = network_for(row.channel)
    skipped: list[str] = []
    async with db.tenant_session(TenantId(tenant_id)) as session:
        for key in keys:
            added = await add_identity(
                session, tenant_id, network=network, external_id=key, phone=row.sender_phone
            )
            if not added:
                skipped.append(key)
    for key in skipped:
        async with db.system_session() as session:
            owner = await resolve_tenant(session, network=network, external_id=key)
        if owner != tenant_id:
            # No key, phone or uid in the log: only allowlisted fields.
            log.warning("tenant.identity_conflict", tenant_id=str(tenant_id), channel=row.channel)


async def resolve_sender(db: Database, row: InboxRow) -> UUID | None:
    """The tenant this message is from, or ``None``. Records any of its keys not yet recorded."""
    network = network_for(row.channel)
    keys = identity_keys(row)
    async with db.system_session() as session:
        found = await _first_match(session, network, keys)
    if found is None:
        return None
    tenant_id, index = found
    others = keys[:index] + keys[index + 1 :]
    if others:
        await _link(db, tenant_id, row, others)
    return tenant_id


async def enrol(db: Database, row: InboxRow, *, language: str) -> UUID:
    """Create the tenant for an invited sender. Idempotent on the identity key.

    Call only after :func:`resolve_sender` returned ``None``. The tenant is created on the
    ``tel:`` key whenever the sender has a number (an invited sender always does): the gateway
    and Meta see one person under different first keys, and two first messages racing on
    different channels must meet at the same ``create_tenant`` call, whose race-safety is on
    one key. Creating on ``uid:`` here would let both win and split the person in two. The
    remaining keys are then linked.

    An open invite for the number is marked used in the transaction that creates the
    tenant (ADR 0006), so the two commit together; a re-run or a racing first message finds
    nothing open and logs nothing.
    """
    network = network_for(row.channel)
    keys = identity_keys(row)
    first = f"tel:{row.sender_phone}" if row.sender_phone else keys[0]
    async with db.system_session() as session:
        tenant_id = await create_tenant(
            session,
            network=network,
            external_id=first,
            phone=row.sender_phone,
            language=language,
        )
        used = row.sender_phone is not None and await mark_used(session, row.sender_phone)
    if used:
        log.info("invite.used", tenant_id=str(tenant_id))
    rest = tuple(key for key in keys if key != first)
    if rest:
        await _link(db, tenant_id, row, rest)
    return tenant_id


async def load_state(db: Database, tenant_id: UUID) -> TenantState | None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None:
            return None
        first = await latest_inbound_channel(session, tenant_id) is None
    return TenantState(
        id=tenant.id,
        status=tenant.status,
        step=tenant.onboarding_step,
        language=tenant.language,
        timezone=tenant.timezone,
        first_message=first,
    )


async def load_history(db: Database, tenant_id: UUID) -> tuple[Message, ...]:
    """The last :data:`HISTORY_LIMIT` messages as the model reads them: ours as its own."""
    async with db.tenant_session(TenantId(tenant_id)) as session:
        rows = await recent_messages(session, tenant_id, limit=HISTORY_LIMIT)
    return tuple(
        Message("assistant" if row.direction == "out" else "user", row.body or "") for row in rows
    )


async def phone_of(db: Database, tenant_id: UUID) -> str | None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await primary_phone(session, tenant_id)


__all__ = [
    "HISTORY_LIMIT",
    "TenantState",
    "enrol",
    "identity_keys",
    "load_history",
    "load_state",
    "network_for",
    "phone_of",
    "resolve_sender",
]
