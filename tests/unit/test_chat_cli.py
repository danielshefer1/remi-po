"""``po-chat``: its arguments, and scripted turns that touch no database."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from personal_organizer.admin.chat import OK, _parser, chat
from personal_organizer.db.engine import Database
from personal_organizer.interfaces.llm import Message
from personal_organizer.messaging.onboarding import REPLY, zone_ask
from personal_organizer.messaging.tenancy import TenantState
from personal_organizer.settings import Settings
from tests.fixtures.llm import ScriptedLLM, said

PHONE = "+31612345678"


def lines(*typed: str) -> Callable[[str], str]:
    queue = list(typed)

    def read(prompt: str) -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return read


class TestArguments:
    def test_the_tenant_is_a_uuid(self) -> None:
        tenant = uuid4()
        assert _parser().parse_args(["--tenant", str(tenant)]).tenant == tenant

    @pytest.mark.parametrize("argv", [[], ["--tenant", "not-a-uuid"]])
    def test_it_is_required_and_checked(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit):
            _parser().parse_args(argv)


async def test_scripted_turns_print_and_keep_history_in_memory(settings: Settings) -> None:
    llm = ScriptedLLM(said("Hi! Two quick steps."), said("A personal organizer."))
    printed: list[str] = []
    tenant = TenantState(
        id=UUID(int=1), status="onboarding", step="zone", language="en", first_message=True
    )
    # Neither turn issues a link, so nothing reaches the database.
    no_db = cast(Database, cast(Any, None))
    result = await chat(
        no_db,
        settings,
        tenant,
        PHONE,
        llm=llm,
        read=lines("hi", "", "what is this?"),
        write=printed.append,
    )
    assert result == OK
    choice = zone_ask("en", "Europe/Amsterdam")
    assert printed == [
        f"bot [{REPLY}]> Hi! Two quick steps.\n\n{choice}",
        f"bot [{REPLY}]> A personal organizer.\n\n{choice}",
    ]
    assert llm.calls[1][1:] == [
        Message("user", "hi"),
        Message("assistant", f"Hi! Two quick steps.\n\n{choice}"),
        Message("user", "what is this?"),
    ]
