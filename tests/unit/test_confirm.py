from __future__ import annotations

import pytest

from personal_organizer.db.models.tenant import LANGUAGES
from personal_organizer.messaging.choices import render_text, resolve
from personal_organizer.messaging.confirm import (
    CONFIRM_ADD,
    CONFIRM_CANCEL,
    CONFIRM_TEXT,
    confirm_choice,
)
from personal_organizer.messaging.language import detect_language

SUMMARY = "Dinner with Noa, Thu 8 Oct 20:00-21:00"


def _picked(language: str, *, text: str | None = None, reply_id: str | None = None) -> str | None:
    option = resolve(confirm_choice(SUMMARY, language), reply_id=reply_id, text=text)
    return option.id if option else None


class TestTable:
    def test_exactly_the_tenant_languages_with_the_same_keys(self) -> None:
        assert set(CONFIRM_TEXT) == set(LANGUAGES)
        assert set(CONFIRM_TEXT["he"]) == set(CONFIRM_TEXT["en"])

    def test_every_hebrew_string_is_hebrew(self) -> None:
        for key, template in CONFIRM_TEXT["he"].items():
            assert detect_language(template) == "he", key


@pytest.mark.parametrize("language", LANGUAGES)
class TestResolve:
    def test_numbers(self, language: str) -> None:
        assert _picked(language, text="1") == CONFIRM_ADD
        assert _picked(language, text="2") == CONFIRM_CANCEL
        assert _picked(language, text="3") is None

    @pytest.mark.parametrize("text", ["Add", "yes", "OK!", "כן", "להוסיף", "👍"])
    def test_add_by_word_in_either_language(self, language: str, text: str) -> None:
        assert _picked(language, text=text) == CONFIRM_ADD

    @pytest.mark.parametrize("text", ["Cancel", "no", "לא", "ביטול", "בטל"])
    def test_cancel_by_word_in_either_language(self, language: str, text: str) -> None:
        assert _picked(language, text=text) == CONFIRM_CANCEL

    def test_button_ids(self, language: str) -> None:
        assert _picked(language, reply_id=CONFIRM_ADD, text="whatever") == CONFIRM_ADD
        assert _picked(language, reply_id=CONFIRM_CANCEL, text="1") == CONFIRM_CANCEL

    def test_free_text_resolves_to_nothing(self, language: str) -> None:
        assert _picked(language, text="make it 9pm instead") is None


def test_rendered_in_english() -> None:
    assert render_text(confirm_choice(SUMMARY, "en"), "en") == (
        f"{SUMMARY}\n\nAdd this to your calendar?\n\n1. Add\n2. Cancel\n\nReply with a number."
    )


def test_rendered_in_hebrew() -> None:
    assert render_text(confirm_choice(SUMMARY, "he"), "he") == (
        "\n".join(
            [SUMMARY, "", "להוסיף ליומן?", "", "1. להוסיף", "2. ביטול", "", "נא להשיב במספר."]
        )
    )
