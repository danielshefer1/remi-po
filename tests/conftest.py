"""Shared fixtures.

``PATTERN_PII`` is PII that :func:`scrub_text` can recognise anywhere it appears.
``CONTENT_PII`` is PII that no regex can spot -- a calendar title, a note -- and which is
therefore protected *only* by the key allowlist. Keeping the two lists separate stops the
test suite from implying a guarantee the regex layer cannot make.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator

import pytest
from structlog._config import BoundLoggerLazyProxy

from personal_organizer.settings import Settings

PATTERN_PII: list[str] = [
    "+31612345678",
    "0031612345678",
    "31612345678@s.whatsapp.net",
    "daniel@example.com",
    "NL91ABNA0417164300",
    "4539578763621486",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
]

CONTENT_PII: list[str] = [
    "Oncology appointment with Dr Meyer",
    "remind me to call mum about the biopsy results",
]

_BASE_ENV: dict[str, str] = {
    "APP__ENV": "ci",
    "APP__COMPONENT": "api",
    "DATABASE__APP_URL": "postgresql://app_user:pw@localhost:5433/po_test",
    "DATABASE__OWNER_URL": "postgresql://app_owner:pw@localhost:5433/po_test",
    "DATABASE__BOOTSTRAP_URL": "postgresql://postgres:pw@localhost:5433/postgres",
    "MODELS__CHAT_MODEL": "gpt-5-mini",
    "MODELS__FALLBACK_MODEL": "claude-haiku-4-5",
    "MODELS__EMBEDDING_MODEL": "text-embedding-3-small",
    "MODELS__TRANSCRIPTION_MODEL": "stt-async-v5",
    "LOGGING__PII_PEPPER": "test-pepper",
}


@pytest.fixture(autouse=True)
def _uncached_module_loggers() -> Iterator[None]:
    """Forget each module logger's cached configuration after every test.

    structlog caches a module's ``log`` on first use together with the processor list current
    at that moment. A test that calls ``configure_logging`` installs a new list, so a logger
    first used before it keeps the old one, and a later ``capture_logs()`` -- which edits the
    current list in place -- never sees that logger's events. Clearing the cache makes every
    test's first use bind to the configuration that test runs under.
    """
    yield
    for name, module in list(sys.modules.items()):
        if name.startswith("personal_organizer"):
            proxy = getattr(module, "log", None)
            if isinstance(proxy, BoundLoggerLazyProxy):
                proxy.__dict__.pop("bind", None)


@pytest.fixture
def settings_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Settings]:
    def factory(**overrides: str) -> Settings:
        for key, value in {**_BASE_ENV, **overrides}.items():
            monkeypatch.setenv(key, value)
        # _env_file="" so a developer's real .env cannot bleed into the suite.
        cfg: Settings = Settings(_env_file="")
        return cfg

    return factory


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory()
