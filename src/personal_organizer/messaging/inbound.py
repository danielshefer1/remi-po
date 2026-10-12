"""Handling one stored inbound message: the gate in front of the agent.

Two gates, chosen by configuration. With Composio off, before the Google connect is set up
on an environment, it is Iteration 02's allowlist gate, unchanged. An invited sender gets the
fixed acknowledgement. Anyone else gets one invite-only line a day, and their content is
deleted. With Composio on it is the tenant gate (D2 in docs/plan-iteration-03.md):

====================================  ======================================================
Sender                                Outcome (disposition)
====================================  ======================================================
resolves to an ``active`` tenant      ``on_allowed`` (``allowed``)
resolves to an ``onboarding`` tenant  the current onboarding step (``onboarding``)
no tenant, number invited             ``enrol``, then the first step (``onboarding``)
anything else, suspended included     invite-only reply and purge (``stranger[_muted]``)
====================================  ======================================================

"Invited" is :func:`~personal_organizer.messaging.invites.is_invited`: on
``WHATSAPP__ALLOWED_PHONES``, or holding an open invite in ``invites`` (ADR 0006).

What both guarantee: a sender who is not invited never reaches the acknowledgement or
``on_allowed``, the agent's turn (:func:`~personal_organizer.messaging.agent.agent_turn`), and
so never spends an LLM call. Identity wins over the invite list: an onboarded
person stays served after their number is removed from it.

**D7.** A known tenant's message is copied into ``messages``, under RLS, and nulled here.
That happens in the one transaction that finishes the row, together with whatever state the
onboarding step decided: ``channel_inbox`` is not an RLS table, so the tenant-scoped session
that writes ``messages`` can update it too, and the step's change, the copy, the nulling and
``processed_at`` commit or roll back as one. For a job that finishes, nothing a known tenant
writes outlives it outside RLS. A job that exhausts its retries leaves the body in
``channel_inbox`` until it is processed (retention is Iteration 12's audit item). Strangers'
messages are purged as before. So are those of an invited number whose first message came too
late to answer, since there is no tenant to keep them under. The transaction claims the row
first (``processed_at IS NULL``), so an overlapping retry writes nothing.

**Re-runs.** ``processed_at`` ends a re-run early and every reply goes through
:func:`send_once`. A job that dies after its sends and before that transaction re-runs from
the same stored state (the tenant row, "is there a recorded message yet", the message), so it
makes the same decision, hits already-claimed sends, and commits the change exactly once.

Replies go out on the channel the message came in on. Several channels can be live at once,
so the handler is given a resolver and looks up the row's ``channel`` by name. The
invite-only mute, though, is per *person* (``recipient_key``), not per channel: a stranger
who writes to two of our numbers is told once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import exists, func, select, update

from personal_organizer.core.errors import ChannelError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.channel import ChannelInbox, ChannelOutbox
from personal_organizer.db.repositories.messages import record_inbound, record_outbound
from personal_organizer.db.repositories.tenants import (
    set_language,
    set_onboarding_step,
    set_timezone,
)
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.interfaces.llm import LLMProvider
from personal_organizer.messaging.inbox import InboxRow, load_row
from personal_organizer.messaging.invites import is_invited
from personal_organizer.messaging.language import detect_language
from personal_organizer.messaging.onboarding import Advance, onboarding_step
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.replies import (
    ACK_TEXT,
    INVITE_ONLY_MUTE,
    INVITE_ONLY_TEXT,
    SERVICE_WINDOW,
    SERVICE_WINDOW_MARGIN,
)
from personal_organizer.messaging.tenancy import (
    TenantState,
    enrol,
    load_history,
    load_state,
    resolve_sender,
)
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

ACK: Final = "ack"
INVITE_ONLY: Final = "invite_only"

#: Outbox states that count as "we already told this stranger". A reply that is still
#: pending or that Meta refused does not mute the next one.
_MUTING_STATUSES: Final = ("sending", "unknown", "accepted", "sent", "delivered", "read")

#: Tenant statuses the gate serves. ``suspended`` (and anything newer) is turned away.
_SERVED: Final = frozenset({"active", "onboarding"})

#: What a purge nulls: the message's content, leaving what deduplication and the mute need.
_PURGED: Final = MappingProxyType(
    {"body": None, "raw": None, "media_id": None, "media_mime_type": None}
)


#: The tenant gate's handoff for an active tenant: ``(row, channel, tenant, now)``, answering
#: on ``channel``. It returns what it decided, for ``_finish_known`` to commit with the row:
#: the agent's reply to record, or ``None`` for nothing.
OnAllowed = Callable[[InboxRow, OutboundChannel, TenantState, datetime], Awaitable[Advance | None]]

#: Looks up an outbound channel by name; raises ``ChannelNotConfiguredError`` if it is absent.
ChannelResolver = Callable[[str], OutboundChannel]


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _already_told(db: Database, row: InboxRow, now: datetime) -> bool:
    """Has this sender had the invite-only reply, for another message, within the mute?"""
    async with db.system_session() as session:
        told = await session.scalar(
            select(
                exists().where(
                    ChannelOutbox.recipient_key == row.sender_key,
                    ChannelOutbox.kind == INVITE_ONLY,
                    ChannelOutbox.status.in_(_MUTING_STATUSES),
                    ChannelOutbox.created_at > now - INVITE_ONLY_MUTE,
                    ChannelOutbox.inbox_id.is_distinct_from(row.id),
                )
            )
        )
    return bool(told)


async def _finish(db: Database, row: InboxRow, disposition: str, *, purge: bool) -> None:
    values: dict[str, object] = {"processed_at": func.now(), "disposition": disposition}
    if purge:
        # A stranger's words have no reason to be kept, and every reason not to be.
        values |= _PURGED
    async with db.system_session() as session:
        await session.execute(update(ChannelInbox).where(ChannelInbox.id == row.id).values(values))


async def _finish_known(
    db: Database,
    row: InboxRow,
    tenant_id: UUID,
    disposition: str,
    advance: Advance | None = None,
) -> None:
    """D7: copy the content under RLS and null it here, in the transaction that finishes the
    row, together with the state the onboarding step decided and the reply it sent."""
    change = advance or Advance()
    async with db.tenant_session(TenantId(tenant_id)) as session:
        # Claim the row first. ``messages.inbox_id`` is not unique, so an overlapping retry that
        # loaded the row before this one committed must find it finished and write nothing.
        finished = await session.scalar(
            update(ChannelInbox)
            .where(ChannelInbox.id == row.id, ChannelInbox.processed_at.is_(None))
            .values(processed_at=func.now(), disposition=disposition, **_PURGED)
            .returning(ChannelInbox.id)
        )
        if finished is None:
            log.info("inbound.already_processed", inbox_id=str(row.id))
            return
        if change.timezone is not None:
            await set_timezone(session, tenant_id, change.timezone)
        if change.next_step is not None:
            await set_onboarding_step(session, tenant_id, change.next_step)
        if change.language is not None:
            await set_language(session, tenant_id, change.language)
        await record_inbound(
            session,
            tenant_id,
            inbox_id=row.id,
            channel=row.channel,
            message_type=row.message_type,
            body=row.body,
            sent_at=row.sent_at,
        )
        if change.reply is not None:
            await record_outbound(
                session, tenant_id, inbox_id=row.id, channel=row.channel, body=change.reply
            )


async def acknowledge(row: InboxRow, channel: OutboundChannel, *, db: Database) -> None:
    """Iteration 02's answer: the fixed acknowledgement. The allowlist gate's, and the tenant
    gate's while the agent is off."""
    if row.sender_phone is None:
        # An active tenant who writes with a BSUID and no number reaches here (the tenant gate
        # does not need a phone). Until Iteration 04 there is no reply to such a sender.
        return
    await send_once(
        db,
        channel,
        inbox_id=row.id,
        kind=ACK,
        recipient_key=row.sender_key,
        to=row.sender_phone,
        text=ACK_TEXT,
    )


async def _turn_away(db: Database, channel: OutboundChannel, row: InboxRow, now: datetime) -> str:
    if row.sender_phone is None:
        # A BSUID-only sender. Whether Graph accepts a BSUID as ``to`` is unconfirmed, so
        # there is no reply; Iteration 03's tenant identities settle it.
        log.info("inbound.reply_unaddressable", inbox_id=str(row.id), sender=row.sender_key)
        return "stranger"
    if await _already_told(db, row, now):
        return "stranger_muted"
    await send_once(
        db,
        channel,
        inbox_id=row.id,
        kind=INVITE_ONLY,
        recipient_key=row.sender_key,
        to=row.sender_phone,
        text=INVITE_ONLY_TEXT,
    )
    return "stranger"


async def _mark_read(channel: OutboundChannel, row: InboxRow) -> None:
    try:
        await channel.mark_read(row.provider_message_id)
    except ChannelError as exc:
        # Blue ticks are cosmetic; failing the job over them would delay the reply.
        log.warning("inbound.mark_read_failed", inbox_id=str(row.id), error_type=type(exc).__name__)


def _is_stale(row: InboxRow, current: datetime) -> bool:
    # Redelivered after an outage. The reply window has closed, so any send would be
    # refused -- and replying to a day-old message out of the blue is worse than silence.
    return current - row.sent_at >= SERVICE_WINDOW - SERVICE_WINDOW_MARGIN


async def _allowlist_gate(
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    current: datetime,
    *,
    allowlist: frozenset[str],
) -> str:
    """Iteration 02's gate, kept exactly for environments without Composio."""
    allowed = await is_invited(db, row.sender_phone, allowlist)
    if _is_stale(row, current):
        disposition = "stale"
    elif allowed:
        await _mark_read(channel, row)
        await acknowledge(row, channel, db=db)
        disposition = "allowed"
    else:
        disposition = await _turn_away(db, channel, row, current)
    await _finish(db, row, disposition, purge=not allowed)
    return disposition


async def _tenant_gate(
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    current: datetime,
    *,
    allowlist: frozenset[str],
    on_allowed: OnAllowed,
    settings: Settings,
    llm: LLMProvider | None,
    last_attempt: bool,
) -> str:
    """D2: serve, onboard, enrol or turn away. See the module docstring's table."""
    stale = _is_stale(row, current)
    tenant_id = await resolve_sender(db, row)
    # Asked only for a sender with no tenant: identity wins over invites.
    if tenant_id is None and not stale and await is_invited(db, row.sender_phone, allowlist):
        # Their first message. Its words decide the tenant's language (D11).
        tenant_id = await enrol(db, row, language=detect_language(row.body))
    tenant = await load_state(db, tenant_id) if tenant_id is not None else None

    if tenant is None or tenant.status not in _SERVED:
        disposition = "stale" if stale else await _turn_away(db, channel, row, current)
        await _finish(db, row, disposition, purge=True)
        return disposition
    if stale:
        await _finish_known(db, row, tenant.id, "stale")
        return "stale"
    await _mark_read(channel, row)
    if tenant.status == "active":
        decided = await on_allowed(row, channel, tenant, current)
        await _finish_known(db, row, tenant.id, "allowed", decided)
        return "allowed"
    history = await load_history(db, tenant.id) if llm is not None else ()
    advance = await onboarding_step(
        db,
        channel,
        row,
        tenant,
        settings=settings,
        now=current,
        llm=llm,
        history=history,
        last_attempt=last_attempt,
    )
    await _finish_known(db, row, tenant.id, "onboarding", advance)
    return "onboarding"


async def handle_inbound(
    inbox_id: UUID,
    *,
    db: Database,
    channels: ChannelResolver,
    allowlist: frozenset[str],
    on_allowed: OnAllowed,
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
    last_attempt: bool = False,
    now: Callable[[], datetime] = _utcnow,
) -> str | None:
    """Process one inbox row. Returns its disposition, or ``None`` if the row is gone.

    ``settings`` with ``composio.enabled`` selects the tenant gate. ``None``, or Composio
    off, is Iteration 02's allowlist gate exactly. ``llm`` talks onboarding through (D10);
    ``last_attempt`` is the job's last, when an unavailable model falls back instead (D3).
    """
    row = await load_row(db, inbox_id)
    if row is None:
        log.warning("inbound.missing", inbox_id=str(inbox_id))
        return None
    if row.processed_at is not None:
        return row.disposition
    # Before anything is claimed or sent: a row whose channel this worker does not serve
    # (switched off with jobs still queued) fails here and stays unprocessed for a re-run.
    channel = channels(row.channel)

    current = now()
    if settings is not None and settings.composio.enabled:
        disposition = await _tenant_gate(
            db,
            channel,
            row,
            current,
            allowlist=allowlist,
            on_allowed=on_allowed,
            settings=settings,
            llm=llm,
            last_attempt=last_attempt,
        )
    else:
        disposition = await _allowlist_gate(db, channel, row, current, allowlist=allowlist)
    log.info(
        "inbound.handled",
        inbox_id=str(row.id),
        channel=row.channel,
        message_type=row.message_type,
        disposition=disposition,
        sender=row.sender_key,
    )
    return disposition


__all__ = [
    "ACK",
    "INVITE_ONLY",
    "ChannelResolver",
    "InboxRow",
    "OnAllowed",
    "acknowledge",
    "handle_inbound",
]
