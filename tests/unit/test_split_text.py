from __future__ import annotations

import pytest

from personal_organizer.messaging.text import WHATSAPP_TEXT_LIMIT, bubbles, split_text


class TestSplitText:
    def test_a_short_body_is_one_chunk(self) -> None:
        assert split_text("hello") == ["hello"]

    def test_an_empty_body_is_one_empty_chunk(self) -> None:
        assert split_text("") == [""]

    def test_exactly_the_limit_is_one_chunk(self) -> None:
        assert split_text("a" * WHATSAPP_TEXT_LIMIT) == ["a" * WHATSAPP_TEXT_LIMIT]

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param("aaaa\n\nbbbb cc", ["aaaa\n\n", "bbbb cc"], id="paragraph"),
            pytest.param("aaaa\nbbbb cc", ["aaaa\n", "bbbb cc"], id="line"),
            pytest.param("aa. bbbb cc", ["aa. ", "bbbb cc"], id="sentence"),
            pytest.param("aaaa bbbbb", ["aaaa ", "bbbbb"], id="word"),
            pytest.param("abcdefghij", ["abcdefg", "hij"], id="hard-cut"),
        ],
    )
    def test_the_best_boundary_that_fits_wins(self, body: str, expected: list[str]) -> None:
        assert split_text(body, limit=7) == expected

    @pytest.mark.parametrize("limit", [1, 5, 50, WHATSAPP_TEXT_LIMIT])
    def test_no_chunk_exceeds_the_limit_and_nothing_is_lost(self, limit: int) -> None:
        body = ("Remind me about the dentist.\n\nThen call mum about the results. " * 400)[:20_000]
        chunks = split_text(body, limit=limit)
        assert all(len(chunk) <= limit for chunk in chunks)
        assert "".join(chunks) == body

    def test_a_zero_limit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            split_text("x", limit=0)


class TestBubbles:
    def test_splits_on_break_line(self) -> None:
        assert bubbles("one\n---\ntwo\n\n  ---  \n\nthree") == ["one", "two", "three"]

    @pytest.mark.parametrize("body", ["a --- b", "----", "- x\n--- y"])
    def test_inline_dashes_dont_split(self, body: str) -> None:
        assert bubbles(body) == [body]

    def test_caps_at_three(self) -> None:
        parts = bubbles("\n---\n".join(str(n) for n in range(10)))
        assert len(parts) == 3
        assert parts[2] == "2\n\n3\n\n4\n\n5\n\n6\n\n7\n\n8\n\n9"

    def test_no_break_is_one_part(self) -> None:
        assert bubbles("  hello there \n\nok ") == ["hello there \n\nok"]

    def test_empty_parts_dropped(self) -> None:
        assert bubbles("---\n\na\n---\n---\nb\n---") == ["a", "b"]
        assert bubbles("") == [""]
        assert bubbles("\n---\n") == [""]
