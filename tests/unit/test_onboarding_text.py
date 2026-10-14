from __future__ import annotations

from string import Formatter

import pytest

from personal_organizer.db.models.tenant import LANGUAGES
from personal_organizer.messaging.language import detect_language
from personal_organizer.messaging.onboarding_prompt import ONBOARDING_PROMPT, onboarding_prompt
from personal_organizer.messaging.onboarding_text import ONBOARDING_TEXT, t

URL = "https://po.example.test/connect/abc.def"


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


class TestTable:
    def test_exactly_the_tenant_languages(self) -> None:
        assert set(ONBOARDING_TEXT) == set(LANGUAGES)

    def test_both_languages_have_the_same_keys(self) -> None:
        assert set(ONBOARDING_TEXT["he"]) == set(ONBOARDING_TEXT["en"])

    def test_both_languages_take_the_same_values(self) -> None:
        for key, english in ONBOARDING_TEXT["en"].items():
            assert _fields(ONBOARDING_TEXT["he"][key]) == _fields(english), key

    def test_the_keys_other_prs_use_exist(self) -> None:
        for language in LANGUAGES:
            assert URL in t("connect_link", language, url=URL)
            assert t("all_set", language)

    def test_every_hebrew_string_is_hebrew(self) -> None:
        for key, template in ONBOARDING_TEXT["he"].items():
            assert detect_language(template) == "he", key


class TestT:
    def test_formats_values(self) -> None:
        assert t("zone_set", "en", zone="London") == "Got it, you're on London time."

    def test_the_link_is_on_its_own_line(self) -> None:
        # WhatsApp linkifies a URL reliably only when nothing is glued to it.
        for language in LANGUAGES:
            assert t("connect_link", language, url=URL).endswith(f"\n{URL}")

    def test_an_unknown_language_falls_back_to_english(self) -> None:
        assert t("all_set", "fr") == t("all_set", "en")

    def test_an_unknown_key_raises(self) -> None:
        with pytest.raises(KeyError):
            t("no_such_key", "en")

    def test_a_missing_value_raises(self) -> None:
        with pytest.raises(KeyError):
            t("connect_link", "en")


class TestPrompt:
    """The onboarding prompt's table, held to the same checks as the texts."""

    def test_both_languages_have_the_same_keys_and_values(self) -> None:
        assert set(ONBOARDING_PROMPT) == set(LANGUAGES)
        for key, english in ONBOARDING_PROMPT["en"].items():
            assert _fields(ONBOARDING_PROMPT["he"][key]) == _fields(english), key

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_each_step_is_described(self, language: str) -> None:
        guessed = onboarding_prompt(language, connect=False, guess="Europe/London")
        asked = onboarding_prompt(language, connect=False, guess=None)
        connect = onboarding_prompt(language, connect=True, guess=None)
        assert "Europe/London" in guessed
        assert len({guessed, asked, connect}) == 3
        assert detect_language(connect) == language
        assert "{" not in guessed + asked + connect
