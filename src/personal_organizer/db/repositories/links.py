"""Onboarding links: the row that makes a signed connect link spendable once, plus a short replay
window (D4, D10).

The token's signature proves we issued it; ``used_at`` records when it was first spent.
:func:`consume_link` is one conditional ``UPDATE``. A link stays spendable for
:data:`REPLAY_WINDOW` after its first use, because some browsers re-send the form POST on their
own about half a second after our 303 (desktop Chrome, 2026-10-14). With a single-use link, the
browser then showed "this link has expired" instead of Composio. ``used_at`` keeps the first use,
so replays never stretch the window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import OnboardingLink
from personal_organizer.db.repositories.scope import scoped

#: How long after its first use a link still sends the browser to Composio. Short: the token
#: stays a bearer for this tenant for that long after the owner used it.
REPLAY_WINDOW: Final = timedelta(seconds=60)


def _spendable(nonce: str, now: datetime) -> ColumnElement[bool]:
    return (
        (OnboardingLink.nonce == nonce)
        & (OnboardingLink.expires_at > now)
        & or_(OnboardingLink.used_at.is_(None), OnboardingLink.used_at > now - REPLAY_WINDOW)
    )


async def create_link(
    session: AsyncSession, tenant_id: UUID, *, nonce: str, expires_at: datetime
) -> OnboardingLink:
    link = OnboardingLink(tenant_id=tenant_id, nonce=nonce, expires_at=expires_at)
    session.add(link)
    await session.flush()
    await session.refresh(link)
    return link


async def latest_usable_link(
    session: AsyncSession, tenant_id: UUID, *, now: datetime
) -> OnboardingLink | None:
    """The newest link that is unused and unexpired at ``now``, for a re-send (D10)."""
    link: OnboardingLink | None = await session.scalar(
        scoped(select(OnboardingLink), OnboardingLink, tenant_id)
        .where(OnboardingLink.used_at.is_(None), OnboardingLink.expires_at > now)
        .order_by(OnboardingLink.created_at.desc(), OnboardingLink.expires_at.desc())
        .limit(1)
    )
    return link


async def link_spendable(
    session: AsyncSession, tenant_id: UUID, *, nonce: str, now: datetime
) -> bool:
    """Whether :func:`consume_link` would succeed at ``now``. Writes nothing."""
    found = await session.scalar(
        scoped(select(OnboardingLink.id), OnboardingLink, tenant_id)
        .where(_spendable(nonce, now))
        .limit(1)
    )
    return found is not None


async def consume_link(
    session: AsyncSession, tenant_id: UUID, *, nonce: str, now: datetime
) -> bool:
    """Spend the link: ``True`` if unused, or first used within :data:`REPLAY_WINDOW` of
    ``now``. Never once it has expired."""
    result = await session.execute(
        scoped(update(OnboardingLink), OnboardingLink, tenant_id)
        .where(_spendable(nonce, now))
        .values(used_at=func.coalesce(OnboardingLink.used_at, now))
        .returning(OnboardingLink.id)
    )
    return result.first() is not None


__all__ = ["REPLAY_WINDOW", "consume_link", "create_link", "latest_usable_link", "link_spendable"]
