from __future__ import annotations

from personal_organizer.db.models.tenant import Note
from personal_organizer.messaging.notes import MAX_NOTES, InMemoryNotes, gender


async def test_in_memory_notes_match_the_repository() -> None:
    notes = InMemoryNotes("Wife: Dana")
    assert await notes.add("wife: dana ") == "exists"
    assert await notes.add("Diet: vegetarian") == "added"
    assert [n.content for n in await notes.all()] == ["Diet: vegetarian", "Wife: Dana"]
    newest = (await notes.all())[0]
    assert await notes.remove(newest.id)
    assert not await notes.remove(newest.id)


async def test_in_memory_notes_cap() -> None:
    notes = InMemoryNotes(*(f"note {i}" for i in range(MAX_NOTES)))
    assert await notes.add("one more") == "full"


def test_gender_reads_the_first_gender_note() -> None:
    def saved(*contents: str) -> list[Note]:
        return [Note(content=c) for c in contents]

    assert gender(saved("Wife: Dana", "Gender: woman")) == "woman"
    assert gender(saved("gender: Man (speaks of himself in the masculine)")) == "man"
    assert gender(saved("Gender: man", "Gender: woman")) == "man"  # newest first wins
    assert gender(saved("Works as a manager", "Gender: manager", "Wife: a woman")) is None
