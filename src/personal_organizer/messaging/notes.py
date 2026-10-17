"""What the agent remembers about a person, behind one small port (Iteration 06).

``TenantNotes`` is the worker's: each call is its own ``tenant_session``. ``InMemoryNotes``
is ``po-chat``'s, which touches no database, and the tests'. Same shape as
``messaging.schedule``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol
from uuid import UUID, uuid4

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import Note
from personal_organizer.db.repositories import notes
from personal_organizer.db.repositories.notes import Added

#: The most notes one person keeps. The 61st asks which old one to forget.
MAX_NOTES: Final = 60
#: The note the agent saves when their Hebrew shows their gender.
_GENDER: Final = re.compile(r"\s*gender:\s*(woman|man)\b", re.IGNORECASE)


def gender(saved: Sequence[Note]) -> str | None:
    """``"woman"`` or ``"man"`` from the first ``Gender:`` note (newest first), else ``None``."""
    for note in saved:
        if match := _GENDER.match(note.content):
            return match.group(1).lower()
    return None


class Notes(Protocol):
    async def add(self, content: str) -> Added: ...
    async def all(self) -> list[Note]: ...
    async def remove(self, note_id: UUID) -> bool: ...


class TenantNotes:
    def __init__(self, db: Database, tenant_id: UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def add(self, content: str) -> Added:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            return await notes.add_note(session, self._tenant_id, content, limit=MAX_NOTES)

    async def all(self) -> list[Note]:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            return await notes.list_notes(session, self._tenant_id)

    async def remove(self, note_id: UUID) -> bool:
        async with self._db.tenant_session(TenantId(self._tenant_id)) as session:
            return await notes.remove_note(session, self._tenant_id, note_id)


class InMemoryNotes:
    """The same behaviour in a list, oldest first. ``notes`` is there to assert on."""

    def __init__(self, *seed: str) -> None:
        self.notes: list[Note] = []
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for i, content in enumerate(seed):
            self.notes.append(
                Note(id=uuid4(), content=content, created_at=start + timedelta(minutes=i))
            )

    async def add(self, content: str) -> Added:
        key = content.strip().lower()
        if any(n.content.strip().lower() == key for n in self.notes):
            return "exists"
        if len(self.notes) >= MAX_NOTES:
            return "full"
        self.notes.append(Note(id=uuid4(), content=content.strip(), created_at=datetime.now(UTC)))
        return "added"

    async def all(self) -> list[Note]:
        return list(reversed(self.notes))

    async def remove(self, note_id: UUID) -> bool:
        before = len(self.notes)
        self.notes = [n for n in self.notes if n.id != note_id]
        return len(self.notes) < before


__all__ = ["MAX_NOTES", "InMemoryNotes", "Notes", "TenantNotes", "gender"]
