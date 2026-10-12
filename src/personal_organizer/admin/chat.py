"""``po-chat --tenant <uuid>`` -- talk to the onboarding conversation from a terminal.

The same decision code the worker runs (:func:`~personal_organizer.messaging.onboarding.
next_reply`), with the real model, starting from the tenant's stored step and language. The
history lives in memory and replies are printed, not sent. Nothing about the tenant is
written: a step the conversation would take is applied in memory only. The one write is a
connect link, issued or reused exactly as the worker would, so the printed link works.

For the model check of docs/plan-iteration-04.md: on Railway, ``railway ssh --service worker
--environment staging``, then ``po-chat --tenant <uuid>``. It needs ``AGENT__API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from personal_organizer.core.errors import ConfigError
from personal_organizer.db.engine import Database
from personal_organizer.interfaces.llm import LLMProvider, Message
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.onboarding import next_reply
from personal_organizer.messaging.tenancy import TenantState, load_state, phone_of
from personal_organizer.observability.logging import configure_logging
from personal_organizer.providers.llm.anthropic import AnthropicLLM
from personal_organizer.settings import Settings, get_settings

OK: Final = 0
REFUSED: Final = 1

#: The channel name the in-memory rows carry. No channel by that name exists to send on.
CHAT_CHANNEL: Final = "po-chat"


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def _row(phone: str, body: str) -> InboxRow:
    return InboxRow(
        id=uuid4(),
        channel=CHAT_CHANNEL,
        provider_message_id=CHAT_CHANNEL,
        sender_key=f"tel:{phone}",
        sender_user_id=None,
        sender_phone=phone,
        message_type="text",
        body=body,
        reply_id=None,
        sent_at=datetime.now(UTC),
        processed_at=None,
        disposition=None,
    )


async def chat(
    db: Database,
    settings: Settings,
    tenant: TenantState,
    phone: str,
    *,
    llm: LLMProvider,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = _out,
) -> int:
    """Turns until end of input. ``read`` blocks, so it runs on a thread."""
    history: list[Message] = []
    while True:
        try:
            line = await asyncio.to_thread(read, "you> ")
        except EOFError:
            return OK
        if not line.strip():
            continue
        reply = await next_reply(
            db,
            _row(phone, line),
            tenant,
            to=phone,
            settings=settings,
            now=datetime.now(UTC),
            llm=llm,
            history=history,
        )
        write(f"bot [{reply.kind}]> {reply.text}")
        change = reply.advance
        if change.timezone is not None:
            write(f"(time zone {change.timezone}, in memory only)")
        history += [Message("user", line), Message("assistant", change.reply or reply.text)]
        tenant = replace(
            tenant,
            step=change.next_step or tenant.step,
            language=change.language or tenant.language,
            first_message=False,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="po-chat", description="talk to the onboarding conversation; prints, never sends"
    )
    parser.add_argument("--tenant", type=UUID, required=True, help="the tenant's uuid")
    return parser


async def _run(tenant_id: UUID, settings: Settings) -> int:
    try:
        llm = AnthropicLLM.from_settings(settings)
    except ConfigError:
        _out("AGENT__API_KEY is not set")
        return REFUSED
    db = Database(settings)
    try:
        tenant = await load_state(db, tenant_id)
        if tenant is None or tenant.status != "onboarding":
            _out("no onboarding tenant with that id; po-chat covers onboarding only")
            return REFUSED
        phone = await phone_of(db, tenant_id)
        if phone is None:
            _out("the tenant has no number on file, so there is no zone guess to start from")
            return REFUSED
        return await chat(db, settings, tenant, phone, llm=llm)
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    return asyncio.run(_run(args.tenant, settings))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
