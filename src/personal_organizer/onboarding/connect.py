"""The connect flow behind ``/connect``: open a link, hand the browser to Composio, finish on
the callback.

D4: a link is spent on POST, never on GET, because WhatsApp fetches links to build previews.
:func:`open_link` reads (the tenant's language and number, for the page) and writes nothing.

D5: the callback believes Composio's API, not its query string. :func:`complete_connect`
verifies our signed state, fetches the account and checks it before it binds anything.

A tenant who is already connected and opens an old link, or reloads the callback tab after the
state's hour, is told so (:class:`AlreadyConnected`) rather than sent back to the bot for a new
link: the bot only acknowledges an active tenant, so that would be a circle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlencode
from uuid import UUID

from personal_organizer.core.errors import ConfigError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.connections import (
    ConcurrentBindError,
    active_connection,
    bind_connection,
)
from personal_organizer.db.repositories.links import consume_link, link_spendable
from personal_organizer.db.repositories.tenants import activate, get_tenant, primary_phone
from personal_organizer.interfaces.calendar import ACCOUNT_ACTIVE, ACCOUNT_CONNECTING, ConnectLinker
from personal_organizer.onboarding.tokens import (
    LINK_SALT,
    STATE_SALT,
    TokenPayload,
    sign,
    verify,
)
from personal_organizer.settings import Settings

#: Only a tenant still onboarding may use a link. An active one has connected already, through
#: another link; reconnecting arrives with the calendar-read iteration.
_LINKABLE: Final = "onboarding"
_ACTIVE: Final = "active"
#: How many trailing digits of the number the page shows: enough for someone handed another
#: person's link to see that it is not theirs before connecting their Google account to it.
PHONE_SUFFIX_DIGITS: Final = 4
#: A callback may land again (a reload) after the first one activated the tenant.
_BINDABLE: Final = frozenset({"onboarding", "active"})
#: How long a signed state stays good: the time a user may spend on Composio's and Google's
#: screens, unverified-app warning included. Generous, because the state is not the authority
#: (Composio's API is, D5), and a late callback can only bind what it verifies.
STATE_MAX_AGE_S: Final = 3600
#: How old a link or state may be and still be recognised as ours: authentic, though too old to
#: use. It only picks the page. An active tenant with an expired token is told "already
#: connected"; anyone else gets the page a forged token gets. It grants nothing: a token past
#: its own max age never reaches ``consume_link``, Composio or ``bind_connection``. Thirty days,
#: because an old link sits in the chat and gets tapped long after it expired.
RECOGNISED_MAX_AGE_S: Final = 30 * 24 * 3600
#: Composio's connected-account ids. Checked before an id from the query reaches an API path.
_ACCOUNT_ID = re.compile(r"\Aca_[A-Za-z0-9_-]{1,64}\Z")
#: ``Refused.reason`` for an account another tenant already holds.
ACCOUNT_CONFLICT: Final = "account_conflict"
#: ``Refused.reason`` for an account still on its way to ``ACTIVE``: a reload may find it there.
NOT_READY: Final = "not_ready"
#: ``Refused.reason`` for a bind that lost a race with another account's, for the same tenant.
#: A reload binds cleanly.
BIND_RACE: Final = "bind_race"


@dataclass(frozen=True, slots=True)
class ConnectConfig:
    """The four settings the flow needs, narrowed from optional. Composio being enabled makes
    them required (``Settings._composio_is_complete``), and the router mounts only then."""

    secret: str
    base_url: str
    auth_config_id: str
    link_ttl_s: int

    @classmethod
    def of(cls, settings: Settings) -> ConnectConfig:
        secret = settings.onboarding.link_secret
        base_url = settings.app.public_base_url
        auth_config_id = settings.composio.calendar_auth_config_id
        if secret is None or base_url is None or auth_config_id is None:
            msg = "The connect pages need the settings COMPOSIO__ENABLED requires"
            raise ConfigError(msg)
        return cls(
            secret=secret.get_secret_value(),
            base_url=base_url,
            auth_config_id=auth_config_id,
            link_ttl_s=settings.onboarding.link_ttl_s,
        )

    @property
    def callback_base(self) -> str:
        return f"{self.base_url}/connect/callback"


@dataclass(frozen=True, slots=True)
class LinkPage:
    language: str
    phone_suffix: str | None


@dataclass(frozen=True, slots=True)
class Connected:
    tenant_id: UUID
    connection_id: UUID
    language: str


@dataclass(frozen=True, slots=True)
class AlreadyConnected:
    """The tenant is active: a link has nothing left to do, and a late callback nothing to
    bind. Nothing was written, and no message is due."""

    tenant_id: UUID
    language: str


@dataclass(frozen=True, slots=True)
class Refused:
    """Why a callback bound nothing: a fixed, log-safe word, never shown to the user.
    ``tenant_id`` is set when the state was verified (or, for ``expired_state``, recognised as
    ours), so the caller may log it."""

    reason: str
    tenant_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class _Link:
    payload: TokenPayload
    #: Within the link's own max age. A recognised but older link is good for nothing but
    #: telling an active tenant they are connected.
    usable: bool


def _link(token: str, config: ConnectConfig) -> _Link | None:
    payload = verify(token, secret=config.secret, salt=LINK_SALT, max_age_s=RECOGNISED_MAX_AGE_S)
    if payload is None:
        return None
    usable = verify(token, secret=config.secret, salt=LINK_SALT, max_age_s=config.link_ttl_s)
    return _Link(payload=payload, usable=usable is not None)


async def open_link(
    token: str, *, db: Database, config: ConnectConfig, now: datetime
) -> LinkPage | AlreadyConnected | None:
    """What the button page shows, or ``None`` for a link that cannot be used. Writes nothing.

    A spent link gets ``None`` here rather than a button that can only fail: people reopen old
    links from the chat. The POST stays the authority.
    """
    link = _link(token, config)
    if link is None:
        return None
    tenant_id = link.payload.tenant_id
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is not None and tenant.status == _ACTIVE:
            return AlreadyConnected(tenant_id=tenant_id, language=tenant.language)
        if tenant is None or tenant.status != _LINKABLE or not link.usable:
            return None
        if not await link_spendable(session, tenant_id, nonce=link.payload.nonce, now=now):
            return None
        phone = await primary_phone(session, tenant_id)
    return LinkPage(
        language=tenant.language,
        phone_suffix=phone[-PHONE_SUFFIX_DIGITS:] if phone else None,
    )


async def start_connect(
    token: str, *, db: Database, linker: ConnectLinker, config: ConnectConfig, now: datetime
) -> str | AlreadyConnected | None:
    """Spend the link and return Composio's URL, or ``None`` if the link cannot be used. An
    active tenant's link is not spent.

    The link is spent *before* Composio is called, in its own transaction. It stays spendable
    for ``REPLAY_WINDOW`` after that first use, so a browser that re-sends the POST (or a
    retry after a Composio failure) gets a fresh redirect instead of "expired". Each one is a
    new Composio link; the ones left behind expire unused there, and the callback's
    ``bind_connection`` keeps whichever finishes. Past the window the user asks the bot for a
    new link, which the connect step issues because none is left unused.

    Raises :class:`~personal_organizer.core.errors.CalendarProviderError` from ``linker``.
    """
    link = _link(token, config)
    if link is None:
        return None
    payload = link.payload
    tenant_id = payload.tenant_id
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is not None and tenant.status == _ACTIVE:
            return AlreadyConnected(tenant_id=tenant_id, language=tenant.language)
        if tenant is None or tenant.status != _LINKABLE or not link.usable:
            return None
        if not await consume_link(session, tenant_id, nonce=payload.nonce, now=now):
            return None
    state = sign(
        TokenPayload(tenant_id=tenant_id, nonce=payload.nonce),
        secret=config.secret,
        salt=STATE_SALT,
    )
    return await linker.link(
        user_id=str(tenant_id),
        auth_config_id=config.auth_config_id,
        callback_url=f"{config.callback_base}?{urlencode({'state': state})}",
    )


async def complete_connect(
    *,
    state: str | None,
    connected_account_id: str | None,
    db: Database,
    linker: ConnectLinker,
    config: ConnectConfig,
) -> Connected | AlreadyConnected | Refused:
    """D5: verify our state, then believe only what Composio's API says about the account.

    Everything that can refuse does so before a tenant row is touched, except the tenant's own
    status. Binding and activating are one tenant transaction. Idempotent: the same callback
    twice binds once (``bind_connection`` returns the existing row) and reports the same
    connection id, so the jobs it defers send one message between them.

    A state past :data:`STATE_MAX_AGE_S` but still recognised as ours is :func:`_late`:
    neither Composio nor the binding is reached.

    Raises :class:`~personal_organizer.core.errors.CalendarProviderError` from ``linker``.
    """
    if not state:
        return Refused("bad_state")
    payload = verify(state, secret=config.secret, salt=STATE_SALT, max_age_s=STATE_MAX_AGE_S)
    if payload is None:
        return await _late(state, db=db, config=config)
    if connected_account_id is None or not _ACCOUNT_ID.match(connected_account_id):
        return Refused("bad_account_id", payload.tenant_id)
    account = await linker.get_account(connected_account_id)
    tenant_id = payload.tenant_id
    if account.user_id != str(tenant_id):
        return Refused("user_mismatch", tenant_id)
    if account.auth_config_id != config.auth_config_id:
        return Refused("auth_config_mismatch", tenant_id)
    if account.status in ACCOUNT_CONNECTING:
        return Refused(NOT_READY, tenant_id)
    if account.status != ACCOUNT_ACTIVE:
        return Refused("not_active", tenant_id)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.status not in _BINDABLE:
            return Refused("tenant_unavailable", tenant_id)
        try:
            connection = await bind_connection(
                session,
                tenant_id,
                connected_account_id=account.id,
                auth_config_id=account.auth_config_id,
            )
        except ConcurrentBindError:
            # Another account's callback for this tenant committed first. The savepoint has
            # undone this one's writes. No activation; a reload revokes that row and binds.
            return Refused(BIND_RACE, tenant_id)
        except LookupError:
            # Only this call: KeyError and friends are LookupErrors too, and a fault in
            # get_tenant or activate must propagate, not pose as a conflict. Here another
            # tenant holds the account. bind_connection's savepoint has already undone its
            # own writes, so what the session commits is a read. No activation.
            return Refused(ACCOUNT_CONFLICT, tenant_id)
        await activate(session, tenant_id)
    return Connected(tenant_id=tenant_id, connection_id=connection.id, language=tenant.language)


async def _late(state: str, *, db: Database, config: ConnectConfig) -> AlreadyConnected | Refused:
    """A state too old to bind with. If it is ours, and its tenant is active with an active
    connection, the tab was reloaded after the fact: say "connected", and write nothing."""
    payload = verify(state, secret=config.secret, salt=STATE_SALT, max_age_s=RECOGNISED_MAX_AGE_S)
    if payload is None:
        return Refused("bad_state")
    tenant_id = payload.tenant_id
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if (
            tenant is not None
            and tenant.status == _ACTIVE
            and await active_connection(session, tenant_id) is not None
        ):
            return AlreadyConnected(tenant_id=tenant_id, language=tenant.language)
    return Refused("expired_state", tenant_id)


__all__ = [
    "ACCOUNT_CONFLICT",
    "BIND_RACE",
    "NOT_READY",
    "PHONE_SUFFIX_DIGITS",
    "RECOGNISED_MAX_AGE_S",
    "STATE_MAX_AGE_S",
    "AlreadyConnected",
    "ConnectConfig",
    "Connected",
    "LinkPage",
    "Refused",
    "complete_connect",
    "open_link",
    "start_connect",
]
