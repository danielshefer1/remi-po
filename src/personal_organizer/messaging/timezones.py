"""Time zones for onboarding: a guess from the number, and a match for what the user types (D10).

**The guess** comes from the country calling code, and only for codes that span exactly one
zone: ``+972``, the UK, Ireland and the common EU codes. Multi-zone codes (``+1``, ``+7``,
``+34`` with the Canaries, ``+351`` with the Azores, ``+55``, ``+61``) get no guess, and the
user is asked for a city. The guess is taken without asking: onboarding stores it and moves on,
and naming a city later replaces it.

**The match** is deliberately small: no geocoding. Tried in order:

1. A short alias table: Hebrew names, plus English names the IANA table spells another
   way or more than one way ("tel aviv", "istanbul", "buenos aires", "utc").
2. A full IANA name, case-insensitively ("europe/london"). Names with no area (``EST``,
   ``GB``) and ``Etc/*`` are not offered. They are fixed offsets or sign-inverted, which
   is the opposite of what a person typing them means.
3. The last segment of a zone in a geographic area ("London", "new york"), but only when
   exactly one zone has it. "Cordoba" names two zones and matches neither.

Spaces, underscores and hyphens are interchangeable ("new york", "New_York", "New-York"),
directional marks are ignored, and edge punctuation is dropped. Anything else is ``None``,
and the caller re-prompts with an example.
"""

from __future__ import annotations

import functools
import re
import zoneinfo
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_organizer.messaging.language import HEBREW, strip_bidi

#: Country calling code -> its single zone. Longest prefix wins (``+420`` before ``+40``).
ZONE_GUESSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "+972": "Asia/Jerusalem",
        "+44": "Europe/London",
        "+353": "Europe/Dublin",
        "+31": "Europe/Amsterdam",
        "+32": "Europe/Brussels",
        "+33": "Europe/Paris",
        "+39": "Europe/Rome",
        "+41": "Europe/Zurich",
        "+43": "Europe/Vienna",
        "+45": "Europe/Copenhagen",
        "+46": "Europe/Stockholm",
        "+47": "Europe/Oslo",
        "+48": "Europe/Warsaw",
        "+49": "Europe/Berlin",
        "+30": "Europe/Athens",
        "+36": "Europe/Budapest",
        "+40": "Europe/Bucharest",
        "+358": "Europe/Helsinki",
        "+420": "Europe/Prague",
    }
)
_PREFIXES: Final = tuple(sorted(ZONE_GUESSES, key=len, reverse=True))

_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ירושלים": "Asia/Jerusalem",
        "תל אביב": "Asia/Jerusalem",
        "תל אביב יפו": "Asia/Jerusalem",
        "ישראל": "Asia/Jerusalem",
        "חיפה": "Asia/Jerusalem",
        "באר שבע": "Asia/Jerusalem",
        "אילת": "Asia/Jerusalem",
        "לונדון": "Europe/London",
        "פריז": "Europe/Paris",
        "ברלין": "Europe/Berlin",
        "אמסטרדם": "Europe/Amsterdam",
        "ניו יורק": "America/New_York",
        # IANA's own "Asia/Tel_Aviv" is a legacy link; store the canonical name.
        "tel aviv": "Asia/Jerusalem",
        "tel aviv yafo": "Asia/Jerusalem",
        "israel": "Asia/Jerusalem",
        "haifa": "Asia/Jerusalem",
        "beer sheva": "Asia/Jerusalem",
        "beersheba": "Asia/Jerusalem",
        "be'er sheva": "Asia/Jerusalem",
        "eilat": "Asia/Jerusalem",
        # Two zones share these last segments; both are the same clock.
        "istanbul": "Europe/Istanbul",
        "nicosia": "Asia/Nicosia",
        "buenos aires": "America/Argentina/Buenos_Aires",
        "utc": "UTC",
    }
)

#: Areas whose zones are places. ``US/``, ``Canada/``, ``Brazil/`` etc. are legacy links that
#: would make "Eastern" or "Pacific" ambiguous, so they match by full name only.
_GEOGRAPHIC_AREAS: Final = frozenset(
    {
        "Africa",
        "America",
        "Antarctica",
        "Arctic",
        "Asia",
        "Atlantic",
        "Australia",
        "Europe",
        "Indian",
        "Pacific",
    }
)

#: A city or a zone name. Longer input is a sentence, not an answer.
_MAX_INPUT: Final = 64
_EDGES: Final = " \t\r\n.!?,;:'\""
_SLASH: Final = re.compile(r"\s*/\s*")


def _key(text: str) -> str:
    text = _SLASH.sub("/", strip_bidi(text).strip(_EDGES))
    return "_".join(text.replace("-", " ").replace("_", " ").split()).casefold()


@functools.cache
def _index() -> tuple[dict[str, str], dict[str, str], dict[str, str | None]]:
    """(aliases, full names, unique last segments). ``None`` marks an ambiguous segment."""
    aliases = {_key(name): zone for name, zone in _ALIASES.items()}
    full: dict[str, str] = {}
    last: dict[str, str | None] = {}
    for name in zoneinfo.available_timezones():
        if "/" not in name or name.startswith("Etc/"):
            continue
        full[_key(name)] = name
        area, _, rest = name.partition("/")
        if area in _GEOGRAPHIC_AREAS:
            segment = _key(rest.rsplit("/", 1)[-1])
            last[segment] = name if last.get(segment, name) == name else None
    return aliases, full, last


def guess_zone(phone: str | None) -> str | None:
    """The zone of an E.164 number's country, if that country has exactly one."""
    if not phone:
        return None
    for prefix in _PREFIXES:
        if phone.startswith(prefix):
            return ZONE_GUESSES[prefix]
    return None


#: The Hebrew name a zone is shown by: the first Hebrew alias that names it ("ירושלים").
_HEBREW_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {zone: name for name, zone in reversed([*_ALIASES.items()]) if not name.isascii()}
)


def zone_label(zone: str, language: str) -> str:
    """``zone`` as people say it: "Jerusalem", "New York", or its Hebrew name if it has one."""
    if language == HEBREW and zone in _HEBREW_NAMES:
        return _HEBREW_NAMES[zone]
    return zone.rsplit("/", 1)[-1].replace("_", " ")


def match_zone(text: str) -> str | None:
    """The IANA zone ``text`` names, or ``None`` if it names none or more than one."""
    if len(text) > _MAX_INPUT:
        return None
    key = _key(text)
    if not key:
        return None
    aliases, full, last = _index()
    return aliases.get(key) or full.get(key) or last.get(key)


__all__ = ["ZONE_GUESSES", "guess_zone", "match_zone", "zone_label"]
