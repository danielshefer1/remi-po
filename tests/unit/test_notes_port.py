from __future__ import annotations

from personal_organizer.messaging.notes import MAX_NOTES, InMemoryNotes


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
