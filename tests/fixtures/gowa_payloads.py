"""GOWA-shaped webhook payloads.

The JSON under ``gowa/`` follows the shapes in the gateway's ``docs/webhook-payload.md``,
with fake ids and the PII corpus's phone number, so a leak test can look for it. Replace each
with a redacted real capture once a device is linked (docs/runbook-whatsapp-gateway.md).

PROVISIONAL: ``group_text``, ``group_mention``, ``group_reply``, ``group_poll_vote`` and
``direct_poll_vote`` are built from the v9.6.0 docs, not captured; replace them with real
captures. The mention shape is undocumented, so ``group_mention`` has it as body text.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from personal_organizer.providers.channel.hmac_sha256 import sign
from tests.fixtures.payloads import encode

DIR: Final = Path(__file__).resolve().parent / "gowa"

#: The sender every message fixture comes from, as the gateway spells it.
SENDER_JID: Final = "31612345678@s.whatsapp.net"
#: The bot's own linked number.
DEVICE_JID: Final = "31600000001@s.whatsapp.net"
WEBHOOK_SECRET: Final = b"test-gowa-webhook-secret-0123456789"


def load(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((DIR / f"{name}.json").read_text())
    return payload


def text_message(
    *,
    message_id: str = "3EB0TEST0000000000000001",
    from_: str = SENDER_JID,
    body: str = "hello",
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """The text fixture with the fields a test usually needs to vary."""
    payload = copy.deepcopy(load("text"))
    payload["payload"].update(
        id=message_id,
        chat_id=from_,
        body=body,
        timestamp=(timestamp or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
    )
    payload["payload"]["from"] = from_
    return payload


def signed(payload: dict[str, Any], secret: bytes = WEBHOOK_SECRET) -> tuple[bytes, dict[str, str]]:
    body = encode(payload)
    return body, {
        "content-type": "application/json",
        "x-hub-signature-256": sign(secret, body),
    }
