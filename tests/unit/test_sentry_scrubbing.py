"""Sentry is the second PII egress path, and it does not pass through structlog at all.

``tests/api/test_no_pii_in_logs.py`` mechanises "logs contain no raw PII" for stdout. This file
does the equivalent for the other way data leaves the process: ``sentry_sdk.capture_exception``
in the api's unhandled-exception handler, whose events never touch the log redaction chain.

**What this asserts, and what it deliberately does not.** ``PATTERN_PII`` is removed wherever it
appears, because ``scrub_text`` recognises it by shape. ``CONTENT_PII`` is prose -- a calendar
title, a note -- and no regex can spot it, so it is *not* asserted absent from a free-text field
here. In the log path the key allowlist covers that; a Sentry event has no fixed shape to
allowlist against, so the protection is structural instead: ``include_local_variables=False``
keeps stack-frame locals out of the event, ``max_request_body_size="never"`` keeps bodies out,
and the api's handler logs ``error_type`` only rather than ``str(exc)``. Those are asserted
below. Stating this explicitly is the point -- see the closing section of ``docs/adr/0001``.
"""

from __future__ import annotations

from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.integrations.anthropic import AnthropicIntegration

from personal_organizer.observability import sentry as sentry_module
from personal_organizer.observability.redaction import REDACTED_SECRET
from personal_organizer.observability.sentry import (
    init_sentry,
    scrub_breadcrumb,
    scrub_event,
)
from personal_organizer.settings import Settings
from tests.conftest import PATTERN_PII

DSN = "https://k@o.ingest.sentry.io/1"


def _event_carrying(secret: str) -> Any:
    """A Sentry-shaped event with ``secret`` threaded through every branch a real one has.

    Typed ``Any`` rather than ``Event``: that is a TypedDict with a fixed key set, and the
    point here is to feed the scrubber the messy real shapes, nesting included.
    """
    return {
        "event_id": "c" * 32,
        "level": "error",
        "message": f"IntegrityError: Key (phone)=({secret}) already exists",
        "exception": {
            "values": [
                {
                    "type": "IntegrityError",
                    "value": f"duplicate key value violates unique constraint: {secret}",
                    "stacktrace": {"frames": [{"function": "ingest", "vars": {"wa": secret}}]},
                }
            ]
        },
        "request": {"url": "https://api.example/webhook", "data": {"from": secret}},
        "extra": {"nested": {"deeper": [secret, {"deepest": secret}]}},
        "breadcrumbs": {"values": [{"message": secret, "data": {"sender": secret}}]},
        "tags": {"tenant": secret},
        "contexts": {"trace": {"trace_id": "a" * 32}},
    }


def _flatten(value: Any) -> list[str]:
    """Every string anywhere in the structure."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _flatten(item)] + [
            s for key in value for s in _flatten(key)
        ]
    if isinstance(value, list | tuple):
        return [s for item in value for s in _flatten(item)]
    return []


class TestScrubEvent:
    @pytest.mark.parametrize("secret", PATTERN_PII)
    def test_pattern_pii_is_removed_from_every_branch(self, secret: str) -> None:
        scrubbed = scrub_event(_event_carrying(secret), {})
        for text in _flatten(scrubbed):
            assert secret not in text

    def test_the_event_stays_structurally_intact(self) -> None:
        """A scrubber that flattened the event would pass the test above and make Sentry
        useless. The shape has to survive."""
        scrubbed: Any = scrub_event(_event_carrying(PATTERN_PII[0]), {})
        assert scrubbed["level"] == "error"
        assert isinstance(scrubbed["exception"]["values"], list)
        assert scrubbed["exception"]["values"][0]["type"] == "IntegrityError"
        assert scrubbed["request"]["url"] == "https://api.example/webhook"
        assert scrubbed["contexts"]["trace"]["trace_id"] == "a" * 32

    def test_secret_keys_are_dropped_wherever_they_sit(self) -> None:
        event: Any = {
            "extra": {"password": "hunter2", "nested": {"api_key": "sk-live-1234"}},
            "request": {"headers": {"authorization": "Bearer abc"}},
        }
        scrubbed: Any = scrub_event(event, {})
        assert scrubbed["extra"]["password"] == REDACTED_SECRET
        assert scrubbed["extra"]["nested"]["api_key"] == REDACTED_SECRET
        assert scrubbed["request"]["headers"]["authorization"] == REDACTED_SECRET
        assert "hunter2" not in _flatten(scrubbed)
        assert "sk-live-1234" not in _flatten(scrubbed)

    def test_correlation_ids_survive(self) -> None:
        """The inverse failure to a leak, and the one that actually bites: a Sentry event is
        mostly ids, and scrub_text's 32-char token rule matches every one of them. An issue
        whose trace_id reads <redacted:token> cannot be tied to the log line that produced it.
        """
        event: Any = {
            "event_id": "c" * 32,
            "release": "9f8e7d6c5b4a39281706f5e4d3c2b1a099887766",
            "contexts": {"trace": {"trace_id": "a" * 32, "span_id": "b" * 16}},
            "tags": {"request_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301"},
        }
        scrubbed: Any = scrub_event(event, {})
        assert scrubbed["event_id"] == "c" * 32
        assert scrubbed["release"] == "9f8e7d6c5b4a39281706f5e4d3c2b1a099887766"
        assert scrubbed["contexts"]["trace"]["trace_id"] == "a" * 32
        assert scrubbed["contexts"]["trace"]["span_id"] == "b" * 16
        assert scrubbed["tags"]["request_id"] == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    @pytest.mark.parametrize("secret", PATTERN_PII)
    def test_an_id_key_is_not_a_trusted_value(self, secret: str) -> None:
        """request_id comes from an inbound X-Request-ID header, so the key being allowlisted
        cannot imply the value is safe. Only an identifier *shape* is preserved."""
        scrubbed: Any = scrub_event(
            {"tags": {"request_id": secret, "trace_id": secret}},
            {},
        )
        assert scrubbed["tags"]["request_id"] != secret
        assert scrubbed["tags"]["trace_id"] != secret

    def test_non_string_scalars_survive(self) -> None:
        """Scrubbing must not coerce the numbers Sentry groups and filters on."""
        event: Any = {"level": "error", "extra": {"status": 500, "ok": False, "ratio": 0.5}}
        scrubbed: Any = scrub_event(event, {})
        assert scrubbed["extra"] == {"status": 500, "ok": False, "ratio": 0.5}

    def test_deep_nesting_is_capped_rather_than_recursing_forever(self) -> None:
        deep: Any = "leaf"
        for _ in range(30):
            deep = {"down": deep}
        scrubbed: Any = scrub_event({"extra": deep}, {})
        assert "<redacted:depth>" in _flatten(scrubbed)

    def test_connect_tokens_are_removed_from_request_urls(self) -> None:
        """D4: the link token is in the URL's path, the callback's state in its query."""
        token = "abc.def.ghi"
        event: Any = {
            "transaction": "/connect/{token}",
            "request": {
                "url": f"https://po.test/connect/{token}",
                "query_string": f"state={token}&connected_account_id=ca_1",
            },
        }
        scrubbed: Any = scrub_event(event, {})
        assert all(token not in text for text in _flatten(scrubbed))
        assert scrubbed["transaction"] == "/connect/{token}"
        assert scrubbed["request"]["query_string"].endswith("&connected_account_id=ca_1")

    def test_a_state_pair_in_a_list_query_string_is_removed_whatever_its_shape(self) -> None:
        event: Any = {
            "request": {"query_string": [["State", "abc"], ["connected_account_id", "ca_1"]]}
        }
        scrubbed: Any = scrub_event(event, {})
        assert scrubbed["request"]["query_string"] == [
            ["State", "<redacted:state>"],
            ["connected_account_id", "ca_1"],
        ]

    def test_an_encoded_state_in_a_header_is_removed(self) -> None:
        referer = "https://x/cb?next=%2Fcb%3Fstate%3DeyJ0IjoiMSJ9.asYPQA.u24jXyZ-signature"
        event: Any = {"request": {"headers": {"Referer": referer}}}
        scrubbed: Any = scrub_event(event, {})
        assert all("asYPQA" not in text for text in _flatten(scrubbed))


class TestScrubBreadcrumb:
    @pytest.mark.parametrize("secret", PATTERN_PII)
    def test_pattern_pii_is_removed_from_breadcrumbs(self, secret: str) -> None:
        crumb = {
            "type": "log",
            "category": "procrastinate",
            "message": f"job started for {secret}",
            "data": {"sender": secret, "nested": [secret]},
        }
        scrubbed = scrub_breadcrumb(crumb, {})
        for text in _flatten(scrubbed):
            assert secret not in text
        assert scrubbed["category"] == "procrastinate"

    def test_it_returns_a_plain_dict(self) -> None:
        """before_breadcrumb must hand the SDK back a dict, not a Mapping view."""
        assert type(scrub_breadcrumb({"message": "hello"}, {})) is dict


class TestInitSentry:
    def test_no_dsn_is_a_no_op(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        """Local and CI have no DSN, and must not initialise a client at all."""
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
        init_sentry(settings)
        assert calls == []

    def test_the_hooks_and_pii_flags_are_wired(
        self, settings_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
        cfg = settings_factory(
            APP__ENV="production",
            SENTRY__DSN=DSN,
            LOGGING__PII_PEPPER="a-real-secret",
            APP__RELEASE="abc123",
        )
        init_sentry(cfg)

        (kwargs,) = calls
        assert kwargs["before_send"] is sentry_module.scrub_event
        assert kwargs["before_send_transaction"] is sentry_module.scrub_event
        assert kwargs["before_breadcrumb"] is sentry_module.scrub_breadcrumb
        assert kwargs["send_default_pii"] is False
        assert kwargs["max_request_body_size"] == "never"
        assert kwargs["environment"] == "production"
        assert kwargs["release"] == "abc123"
        # Auto-enabled, it would report the SDK's raw errors (response bodies) before ours.
        assert [type(i) for i in kwargs["disabled_integrations"]] == [AnthropicIntegration]

    def test_stack_frame_locals_are_not_shipped(
        self, settings_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sentry-sdk defaults include_local_variables to True, and send_default_pii=False does
        not cover it. In a webhook frame those locals are the parsed message payload, which is
        prose scrub_text cannot recognise -- so this flag is the only thing stopping it."""
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
        init_sentry(
            settings_factory(
                APP__ENV="production", SENTRY__DSN=DSN, LOGGING__PII_PEPPER="a-real-secret"
            )
        )
        (kwargs,) = calls
        assert kwargs["include_local_variables"] is False
