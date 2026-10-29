from __future__ import annotations

from dataclasses import replace

import pytest

from personal_organizer.db.models.tenant import LANGUAGES
from personal_organizer.messaging.confirm import (
    CONFIRM_ADD,
    CONFIRM_CANCEL,
    CONFIRM_TEXT,
    confirm_prompt,
    decide,
)
from personal_organizer.messaging.language import detect_language
from tests.fixtures.llm import ScriptedLLM, said

SUMMARY = "Dinner with Noa, Thu 8 Oct 20:00-21:00"


class TestTable:
    def test_exactly_the_tenant_languages_with_the_same_keys(self) -> None:
        assert set(CONFIRM_TEXT) == set(LANGUAGES)
        assert set(CONFIRM_TEXT["he"]) == set(CONFIRM_TEXT["en"])

    def test_every_hebrew_string_is_hebrew(self) -> None:
        for key, template in CONFIRM_TEXT["he"].items():
            assert detect_language(template) == "he", key


class TestDecide:
    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            ("ADD", CONFIRM_ADD),
            (" add.\n", CONFIRM_ADD),
            ("CANCEL", CONFIRM_CANCEL),
            ("OTHER", None),
            ("ADD or CANCEL", None),
            ("", None),
        ],
    )
    async def test_only_a_one_word_answer_counts(self, answer: str, expected: str | None) -> None:
        assert await decide(SUMMARY, "sure", llm=ScriptedLLM(said(answer))) == expected

    async def test_a_cut_off_answer_is_none(self) -> None:
        cut = replace(said("ADD"), finish_reason="max_tokens")
        assert await decide(SUMMARY, "sure", llm=ScriptedLLM(cut)) is None

    async def test_the_call_sees_the_summary_and_the_reply_only(self) -> None:
        llm = ScriptedLLM(said("ADD"))
        await decide(SUMMARY, "כן", llm=llm)
        [[system, user]] = llm.calls
        assert system.role == "system"
        assert SUMMARY in system.content
        assert (user.role, user.content) == ("user", "כן")


def test_rendered_in_english() -> None:
    assert confirm_prompt(SUMMARY, "en") == f"{SUMMARY}\n\nAdd this to your calendar?"


def test_rendered_in_hebrew() -> None:
    assert confirm_prompt(SUMMARY, "he") == "\n".join([SUMMARY, "", "להוסיף ליומן?"])


@pytest.mark.parametrize(
    ("question", "language", "text"),
    [
        ("confirm_change", "en", "Make this change?"),
        ("confirm_remove", "he", "להסיר מהיומן?"),
        (
            "confirm_remove_invite",
            "en",
            "This is an invite from someone else. Remove it from your calendar anyway?",
        ),
    ],
)
def test_a_change_question(question: str, language: str, text: str) -> None:
    assert confirm_prompt(SUMMARY, language, question=question) == f"{SUMMARY}\n\n{text}"
