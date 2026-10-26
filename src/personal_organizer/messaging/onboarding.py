"""The onboarding conversation: the ``zone`` step (D10).

Setup is the time zone only. The calendar is optional: the reply that sets the zone also
activates the tenant and offers the connect link, which works for an active tenant too
(``onboarding/connect.py``). The ``connect`` step is only for tenants who reached it before
the calendar became optional: their next message answers them, offers the link and activates
them.

The tenant gate calls this for every message from an ``onboarding`` tenant. It decides from
four things only: the tenant's stored step, whether this is their first recorded message,
the guess from their number, and the message itself. None of those change until the gate
commits, so a re-run of the job reaches the same decision.

**Send first, return the change.** The step sends through :func:`send_once` under one kind
per decision, and *returns* an :class:`Advance` rather than writing it. The gate commits the
advance together with the finished inbox row and its copy into ``messages`` (D7), in one
transaction. Consider a job that dies after a send and before that commit. It re-runs into a
send that is already claimed and an advance that has not happened, and the step moves once.
The reverse order, writing the step first, would make the re-run hand the same message to
the agent, which would answer it a second time.

**The guess is taken, not asked.** When the number's country has one zone, the first message
sets it and finishes setup, saying which zone was set. Without a guess the user is asked for
a city. A wrong guess is fixed afterwards by telling the agent. Nothing offers numbered
options.

**The model writes the words, code keeps the rails (D10 of docs/plan-iteration-04.md).** With
an LLM, the branches that send a fixed, context-free text -- the welcome, the guessed zone, an
unmatched message in ``zone``, and any message in ``connect`` -- send the model's answer to what was
actually said, with the zone question or the link appended by code. The model may call
``set_timezone``, which then takes the confirm path, and ``set_language``. Its text is
dropped for the fixed one if it holds ``http`` or :data:`LINK_PLACEHOLDER`, is empty, or would
not fit one message. These branches send under the one kind :data:`REPLY`, with the model or
without it, and a re-run that finds that reply sent asks the model nothing: what it decided
now need not be what went out. An exact city never calls the model. ``llm=None`` is the fixed
conversation exactly; so is a model that refuses, or is down on the job's last attempt (D3).

*shortcut: a crash between the send and the commit loses what the model decided with that
reply, a ``set_timezone`` say, and the reply is not recorded for the history either. The next
message asks again. Store the decision with the send if that ever shows up in the logs.*
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

import structlog

from personal_organizer.core.errors import LLMRejectedError, LLMUnavailableError
from personal_organizer.db.engine import Database
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.interfaces.llm import LLMProvider, Message
from personal_organizer.messaging.conversation import converse
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.language import ENGLISH, HEBREW
from personal_organizer.messaging.onboarding_prompt import onboarding_prompt
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import already_sent, send_once
from personal_organizer.messaging.tenancy import TenantState, phone_of
from personal_organizer.messaging.text import WHATSAPP_TEXT_LIMIT
from personal_organizer.messaging.timezones import guess_zone, match_zone, zone_label
from personal_organizer.onboarding.links import issue_or_reuse_link
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

CONNECT: Final = "onboarding:connect"
#: The welcome, the guessed zone, an unmatched zone message and the connect resend, model-written
#: or fixed: one kind, so turning the model on or off between a send and its commit cannot send
#: twice.
REPLY: Final = "onboarding:reply"

ZONE_STEP: Final = "zone"
#: Only tenants who reached it before the calendar became optional are in it.
CONNECT_STEP: Final = "connect"

#: Stands in for the connect link in a recorded reply: the link is a bearer credential, and a
#: model shown a URL in its history may write one.
LINK_PLACEHOLDER: Final = "[link]"

#: The model's onboarding tools. No calendar tools: there is no calendar yet.
TOOLS: Final[tuple[Mapping[str, Any], ...]] = (
    {
        "name": "set_timezone",
        "description": (
            "Save the person's time zone once they have said where they are. Use the IANA "
            "name, for example Asia/Jerusalem or America/New_York."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"zone": {"type": "string", "description": "An IANA time zone."}},
            "required": ["zone"],
        },
    },
    {
        "name": "set_language",
        "description": "Switch every further message to this language, when they ask for it.",
        "input_schema": {
            "type": "object",
            "properties": {"language": {"type": "string", "enum": [HEBREW, ENGLISH]}},
            "required": ["language"],
        },
    },
)


@dataclass(frozen=True, slots=True)
class Advance:
    """The state change a step decided, for the gate to commit with the finished row.
    ``None`` leaves a field as it is. ``activate`` ends onboarding. ``reply`` is the text
    sent, to record (D4)."""

    timezone: str | None = None
    activate: bool = False
    language: str | None = None
    reply: str | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """What a step sends, under which ``send_once`` kind, and the change it decided."""

    kind: str
    text: str
    advance: Advance


def welcome_text(language: str) -> str:
    """The first message without a guess: who this is, then the city question."""
    return f"{t('welcome', language)}\n\n{t('zone_ask_city', language)}"


def guessed_text(language: str, zone: str, url: str, *, welcome: bool = False) -> str:
    """The zone taken from the number, how to change it, and the offer of the link."""
    guessed = t("zone_guessed", language, zone=zone_label(zone, language))
    text = f"{guessed}\n\n{t('connect_link', language, url=url)}"
    return f"{t('welcome', language)}\n\n{text}" if welcome else text


def connect_text(language: str, zone: str, url: str) -> str:
    zone_set = t("zone_set", language, zone=zone_label(zone, language))
    return f"{zone_set}\n\n{t('connect_link', language, url=url)}"


async def next_reply(
    db: Database,
    row: InboxRow,
    tenant: TenantState,
    *,
    to: str,
    settings: Settings,
    now: datetime,
    llm: LLMProvider | None = None,
    history: Sequence[Message] = (),
    last_attempt: bool = False,
) -> Reply:
    """Decide the answer to ``row`` from an onboarding ``tenant`` writing from ``to``.

    Sends nothing and writes no tenant state, though it may issue a connect link. ``po-chat``
    calls it directly.
    """
    language = tenant.language
    guess = guess_zone(to)
    links: list[str] = []

    async def link() -> str:
        url = await issue_or_reuse_link(db, tenant.id, settings=settings, now=now)
        links.append(url)
        return url

    async def confirm(zone: str) -> Reply:
        text = connect_text(language, zone, await link())
        return Reply(CONNECT, text, Advance(timezone=zone, activate=True))

    async def talk(
        model: LLMProvider,
        fixed: Callable[[str], str],
        *,
        url: str | None = None,
        preset: str | None = None,
    ) -> Reply:
        """The model's answer over the suffix: the guessed zone ``preset`` and the link, the
        link alone when ``url`` is given, else the city question. ``fixed`` is the text sent
        without a model for this branch, in a given language. Offering the link ends setup."""
        decided: dict[str, str] = {}
        taken = Advance(timezone=preset, activate=url is not None)

        async def run_tool(name: str, arguments: Mapping[str, Any]) -> str:
            if name == "set_timezone":
                zone = match_zone(str(arguments.get("zone", "")))
                if zone is None:
                    return "error: not a time zone; ask which city they are in"
                decided["timezone"] = zone
                return f"ok: time zone set to {zone}"
            if name == "set_language" and arguments.get("language") in (HEBREW, ENGLISH):
                decided["language"] = str(arguments["language"])
                return f"ok: language set to {decided['language']}"
            return "error: unknown tool or argument"

        try:
            said = await converse(
                history,
                (row.body or "").strip() or f"[{row.message_type}]",
                system=onboarding_prompt(language, connect=tenant.step != ZONE_STEP, guess=guess),
                tools=TOOLS,
                run_tool=run_tool,
                llm=model,
            )
        except LLMUnavailableError:
            if not last_attempt:
                raise
            log.warning("onboarding.llm_fallback", inbox_id=str(row.id), reason="unavailable")
            return Reply(REPLY, fixed(language), taken)
        except LLMRejectedError:
            log.warning("onboarding.llm_fallback", inbox_id=str(row.id), reason="rejected")
            return Reply(REPLY, fixed(language), taken)

        switched = decided.get("language")
        lang = switched or language
        zone = decided.get("timezone")
        if zone is not None:
            suffix = connect_text(lang, zone, url or await link())
            advance = Advance(timezone=zone, activate=True, language=switched)
        else:
            if preset is not None and url is not None:
                suffix = guessed_text(lang, preset, url)
            else:
                suffix = t("connect_link", lang, url=url) if url else t("zone_ask_city", lang)
            advance = replace(taken, language=switched)
        said = said.strip()
        text = f"{said}\n\n{suffix}"
        echoed = "http" in said.lower() or LINK_PLACEHOLDER in said
        if not said or echoed or len(text) > WHATSAPP_TEXT_LIMIT:
            log.info("onboarding.reply_dropped", inbox_id=str(row.id))
            text = suffix if zone is not None else fixed(lang)
        return Reply(REPLY, text, advance)

    async def decide() -> Reply:
        zone = match_zone(row.body) if row.body else None
        if tenant.step != ZONE_STEP:
            # ``connect`` from before the calendar was optional, and defensively anything else:
            # answer, offer the link and finish. A city still replaces the zone set before.
            if zone is not None:
                return await confirm(zone)
            url = await link()
            if llm is not None:
                return await talk(llm, lambda lang: t("connect_link", lang, url=url), url=url)
            return Reply(REPLY, t("connect_link", language, url=url), Advance(activate=True))
        # A first message is never read as an answer to a question not yet asked.
        if zone is not None and not tenant.first_message:
            return await confirm(zone)
        if guess is not None:
            url = await link()
            welcome = tenant.first_message

            def fixed(lang: str) -> str:
                return guessed_text(lang, guess, url, welcome=welcome)

            if llm is not None:
                return await talk(llm, fixed, url=url, preset=guess)
            return Reply(REPLY, fixed(language), Advance(timezone=guess, activate=True))
        if tenant.first_message:
            if llm is not None:
                return await talk(llm, welcome_text)
            return Reply(REPLY, welcome_text(language), Advance())
        log.info("onboarding.zone_unmatched", inbox_id=str(row.id), tenant_id=str(tenant.id))
        if llm is not None:
            return await talk(llm, lambda lang: t("zone_retry", lang))
        return Reply(REPLY, t("zone_retry", language), Advance())

    reply = await decide()
    if llm is None:
        # Nothing reads the history without a model, and the fixed conversation stays exact.
        return reply
    recorded = reply.text
    for url in links:
        recorded = recorded.replace(url, LINK_PLACEHOLDER)
    return replace(reply, advance=replace(reply.advance, reply=recorded))


async def onboarding_step(
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    tenant: TenantState,
    *,
    settings: Settings,
    now: datetime,
    llm: LLMProvider | None = None,
    history: Sequence[Message] = (),
    last_attempt: bool = False,
) -> Advance:
    """Answer ``row`` for an onboarding ``tenant``. Sends; writes no tenant state.

    ``history`` is the recorded conversation before ``row`` (D4), which only the model reads.
    On the job's ``last_attempt`` an unavailable model falls back instead of raising.
    """
    address = row.sender_phone or await phone_of(db, tenant.id)
    if address is None:
        # A BSUID-only sender whose tenant has no number on file. Whether Graph accepts a
        # BSUID as ``to`` is unconfirmed, so there is no reply, exactly as for strangers.
        log.info("onboarding.unaddressable", inbox_id=str(row.id), tenant_id=str(tenant.id))
        return Advance()
    if await already_sent(db, row.id, REPLY):
        # A re-run takes the branch the first run took, so this is one of the REPLY ones: the
        # model asked again could decide other than what was sent.
        # shortcut: the sent text then isn't in the history, and what the first run decided
        # is lost (the next message asks again); store both with the send if that bites.
        log.info("onboarding.reply_already_sent", inbox_id=str(row.id))
        # In the zone step with a guess, that branch took the guess, which needs no model; any
        # REPLY outside the zone step offered the link. Both finished setup.
        if tenant.step != ZONE_STEP:
            return Advance(activate=True)
        guess = guess_zone(address)
        return Advance(timezone=guess, activate=True) if guess is not None else Advance()
    reply = await next_reply(
        db,
        row,
        tenant,
        to=address,
        settings=settings,
        now=now,
        llm=llm,
        history=history,
        last_attempt=last_attempt,
    )
    await send_once(
        db,
        channel,
        inbox_id=row.id,
        kind=reply.kind,
        recipient_key=row.sender_key,
        to=address,
        text=reply.text,
    )
    return reply.advance


__all__ = [
    "CONNECT",
    "CONNECT_STEP",
    "LINK_PLACEHOLDER",
    "REPLY",
    "TOOLS",
    "ZONE_STEP",
    "Advance",
    "Reply",
    "connect_text",
    "guessed_text",
    "next_reply",
    "onboarding_step",
    "welcome_text",
]
