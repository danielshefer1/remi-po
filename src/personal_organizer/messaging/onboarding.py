"""The onboarding conversation: the ``zone`` and ``connect`` steps (D10).

The tenant gate calls this for every message from an ``onboarding`` tenant. It decides from
four things only: the tenant's stored step, whether this is their first recorded message,
the guess from their number, and the message itself. None of those change until the gate
commits, so a re-run of the job reaches the same decision.

**Send first, return the change.** The step sends through :func:`send_once` under one kind
per decision, and *returns* an :class:`Advance` rather than writing it. The gate commits the
advance together with the finished inbox row and its copy into ``messages`` (D7), in one
transaction. Consider a job that dies after a send and before that commit. It re-runs into a
send that is already claimed and an advance that has not happened, and the step moves once.
The reverse order, writing the step first, would make the re-run treat the same message as
a ``connect``-step message and send the link a second time.

**No choice is stored (D9).** While the step is ``zone`` and the number's country gives a
guess, the Correct/Change choice is open, rebuilt from the guess on every message. There is
no separate "waiting for a city" state either. A city is accepted whenever it is sent, so
"Change" only has to ask for one. One consequence: "1" after "Change" still means Correct.
That is harmless, because the user is shown the zone they are keeping.

**The model writes the words, code keeps the rails (D10 of docs/plan-iteration-04.md).** With
an LLM, the three branches that send a fixed, context-free text -- the welcome, an unmatched
message in ``zone``, and any message in ``connect`` -- send the model's answer to what was
actually said, with the zone question or the link appended by code. The model may call
``set_timezone``, which then takes the confirm path, and ``set_language``. Its text is
dropped for the fixed one if it holds ``http`` or :data:`LINK_PLACEHOLDER`, is empty, or would
not fit one message. These three branches send under the one kind :data:`REPLY`, with the
model or without it, and a re-run that finds that reply sent asks the model nothing: what it
decided now need not be what went out. The default path ("1", a button, an alias, an exact
city, Change) never calls the model. ``llm=None`` is the fixed conversation exactly; so is a
model that refuses, or is down on the job's last attempt (D3).

*shortcut: a crash between the send and the commit loses what the model decided with that
reply, a ``set_timezone`` say, and the reply is not recorded for the history either. The next
message asks again, and a link already sent keeps working, because connecting depends on
status, not step. Store the decision with the send if that ever shows up in the logs.*
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
from personal_organizer.messaging.choices import Choice, Option, render_text, resolve
from personal_organizer.messaging.conversation import converse
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.language import ENGLISH, HEBREW
from personal_organizer.messaging.onboarding_prompt import onboarding_prompt
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import already_sent, send_once
from personal_organizer.messaging.tenancy import TenantState, phone_of
from personal_organizer.messaging.text import WHATSAPP_TEXT_LIMIT
from personal_organizer.messaging.timezones import guess_zone, match_zone
from personal_organizer.onboarding.links import issue_or_reuse_link
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

ZONE_ASK_CITY: Final = "onboarding:zone_ask_city"
CONNECT: Final = "onboarding:connect"
#: The welcome, an unmatched zone message and the connect resend, model-written or fixed: one
#: kind, so turning the model on or off between a send and its commit cannot send twice.
REPLY: Final = "onboarding:reply"

ZONE_CORRECT: Final = "zone:correct"
ZONE_CHANGE: Final = "zone:change"

ZONE_STEP: Final = "zone"
CONNECT_STEP: Final = "connect"

#: Stands in for the connect link in a recorded reply: the link is a bearer credential, and a
#: model shown a URL in its history may write one.
LINK_PLACEHOLDER: Final = "[link]"

#: Both languages, whatever the tenant's: people answer in the language they think in.
_CORRECT_ALIASES: Final = ("yes", "y", "ok", "correct", "right", "כן", "נכון", "👍")
_CHANGE_ALIASES: Final = ("no", "n", "change", "wrong", "לא", "לשנות", "שנה")

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
    ``None`` leaves a field as it is. ``reply`` is the text sent, to record (D4)."""

    timezone: str | None = None
    next_step: str | None = None
    language: str | None = None
    reply: str | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """What a step sends, under which ``send_once`` kind, and the change it decided."""

    kind: str
    text: str
    advance: Advance


def zone_choice(zone: str, language: str) -> Choice:
    """Correct first: ``zone_retry_keep`` tells the user to reply 1 to keep the guess."""
    return Choice(
        prompt=t("zone_confirm", language, zone=zone),
        options=(
            Option(ZONE_CORRECT, t("option_correct", language), _CORRECT_ALIASES),
            Option(ZONE_CHANGE, t("option_change", language), _CHANGE_ALIASES),
        ),
    )


def zone_ask(language: str, guess: str | None) -> str:
    """The zone step's question: the choice built from the guess, or the city prompt."""
    if guess:
        return render_text(zone_choice(guess, language), language)
    return t("zone_ask_city", language)


def welcome_text(language: str, guess: str | None) -> str:
    return f"{t('welcome', language)}\n\n{zone_ask(language, guess)}"


def retry_text(language: str, guess: str | None) -> str:
    retry = t("zone_retry", language)
    return f"{retry}\n{t('zone_retry_keep', language, zone=guess)}" if guess else retry


def connect_text(language: str, zone: str, url: str) -> str:
    return f"{t('zone_set', language, zone=zone)}\n\n{t('connect_link', language, url=url)}"


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
        return Reply(CONNECT, text, Advance(timezone=zone, next_step=CONNECT_STEP))

    async def talk(model: LLMProvider, url: str | None, fixed: Callable[[str], str]) -> Reply:
        """The model's answer over the suffix: the link when ``url`` is given, else the zone
        question. ``fixed`` is today's text for this branch, in a given language."""
        decided: dict[str, str] = {}

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
                system=onboarding_prompt(language, connect=url is not None, guess=guess),
                tools=TOOLS,
                run_tool=run_tool,
                llm=model,
            )
        except LLMUnavailableError:
            if not last_attempt:
                raise
            log.warning("onboarding.llm_fallback", inbox_id=str(row.id), reason="unavailable")
            return Reply(REPLY, fixed(language), Advance())
        except LLMRejectedError:
            log.warning("onboarding.llm_fallback", inbox_id=str(row.id), reason="rejected")
            return Reply(REPLY, fixed(language), Advance())

        switched = decided.get("language")
        lang = switched or language
        zone = decided.get("timezone")
        if zone is not None:
            suffix = connect_text(lang, zone, url or await link())
            advance = Advance(timezone=zone, next_step=CONNECT_STEP, language=switched)
        else:
            suffix = t("connect_link", lang, url=url) if url else zone_ask(lang, guess)
            advance = Advance(language=switched)
        said = said.strip()
        text = f"{said}\n\n{suffix}"
        echoed = "http" in said.lower() or LINK_PLACEHOLDER in said
        if not said or echoed or len(text) > WHATSAPP_TEXT_LIMIT:
            log.info("onboarding.reply_dropped", inbox_id=str(row.id))
            text = suffix if zone is not None else fixed(lang)
        return Reply(REPLY, text, advance)

    async def decide() -> Reply:
        if tenant.step != ZONE_STEP:
            # ``connect``, and defensively anything else: connecting is the only way out.
            url = await link()
            if llm is not None:
                return await talk(llm, url, lambda lang: t("connect_link", lang, url=url))
            return Reply(REPLY, t("connect_link", language, url=url), Advance())
        if tenant.first_message:
            if llm is not None:
                return await talk(llm, None, lambda lang: welcome_text(lang, guess))
            return Reply(REPLY, welcome_text(language, guess), Advance())
        if guess is not None:
            picked = resolve(zone_choice(guess, language), reply_id=row.reply_id, text=row.body)
            if picked is not None and picked.id == ZONE_CORRECT:
                return await confirm(guess)
            if picked is not None:
                return Reply(ZONE_ASK_CITY, t("zone_ask_city", language), Advance())
        zone = match_zone(row.body) if row.body else None
        if zone is not None:
            return await confirm(zone)
        log.info("onboarding.zone_unmatched", inbox_id=str(row.id), tenant_id=str(tenant.id))
        if llm is not None:
            return await talk(llm, None, lambda lang: retry_text(lang, guess))
        return Reply(REPLY, retry_text(language, guess), Advance())

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
        # A re-run takes the branch the first run took, so this is one of the three: the
        # model asked again could decide other than what was sent.
        # shortcut: the sent text then isn't in the history, and what the first run decided
        # is lost (the next message asks again); store both with the send if that bites.
        log.info("onboarding.reply_already_sent", inbox_id=str(row.id))
        return Advance()
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
    "ZONE_ASK_CITY",
    "ZONE_CHANGE",
    "ZONE_CORRECT",
    "ZONE_STEP",
    "Advance",
    "Reply",
    "connect_text",
    "next_reply",
    "onboarding_step",
    "retry_text",
    "welcome_text",
    "zone_ask",
    "zone_choice",
]
