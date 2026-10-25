"""The worker-side half of the WhatsApp channel: Graph API sends.

The only interesting decision here is error classification, because the Cloud API has no
idempotency key: a retried send that had in fact gone through is a duplicate message on the
user's phone. So every failure is sorted by one question -- *could Meta have accepted it?*

- **Transient** -- no. We never connected, or Meta answered and refused for now (throttled,
  5xx). Safe to retry.
- **Ambiguous** -- maybe. The request left and the answer never came back. Not retried; see
  docs/adr/0003.
- **Rejected** -- no, and a retry would be refused the same way (bad recipient, expired
  token, outside the 24-hour window).

Only the HTTP status and Meta's numeric error code are logged. The response body is never
logged: Graph error messages can quote the request back.
"""

from __future__ import annotations

from typing import Any, Final

import httpx
import structlog

from personal_organizer.core.errors import (
    AmbiguousDeliveryError,
    MessageTooLongError,
    RejectedChannelError,
    TransientChannelError,
)
from personal_organizer.interfaces.channel import OutboundMessage
from personal_organizer.messaging.text import WHATSAPP_TEXT_LIMIT
from personal_organizer.providers.channel.whatsapp.inbound import CHANNEL_NAME
from personal_organizer.settings import WhatsAppSettings

log = structlog.get_logger(__name__)

#: Graph error codes that mean "slow down", which Meta may send with a 400 rather than a 429.
#: From Meta's error-code reference; confirm against real responses during the live hookup.
THROTTLING_CODES: Final = frozenset({4, 80007, 130429, 131048, 131056})

#: Nothing was sent: the connection never opened, or the pool had no slot.
_NOT_SENT: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _error_code(response: httpx.Response) -> int | None:
    try:
        code = response.json()["error"]["code"]
    except ValueError, KeyError, TypeError:
        return None
    return code if isinstance(code, int) else None


class WhatsAppOutbound:
    name: str = CHANNEL_NAME

    def __init__(self, http: httpx.AsyncClient, *, phone_number_id: str, access_token: str) -> None:
        self._http = http
        self._path = f"{phone_number_id}/messages"
        self._access_token = access_token

    @classmethod
    def from_settings(cls, settings: WhatsAppSettings, http: httpx.AsyncClient) -> WhatsAppOutbound:
        if settings.phone_number_id is None or settings.access_token is None:
            msg = "WhatsApp sending needs WHATSAPP__PHONE_NUMBER_ID and WHATSAPP__ACCESS_TOKEN"
            raise ValueError(msg)
        return cls(
            http,
            phone_number_id=settings.phone_number_id,
            access_token=settings.access_token.get_secret_value(),
        )

    def __repr__(self) -> str:
        return f"WhatsAppOutbound(path={self._path!r})"

    async def send_text(self, message: OutboundMessage) -> str:
        if len(message.body) > WHATSAPP_TEXT_LIMIT:
            raise MessageTooLongError(len(message.body), WHATSAPP_TEXT_LIMIT)
        if message.recipient.endswith("@g.us") or message.poll_options:
            raise RejectedChannelError(None)  # groups and polls are GOWA-only
        data = await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": message.recipient,
                "type": "text",
                "text": {"preview_url": False, "body": message.body},
            }
        )
        try:
            provider_message_id = data["messages"][0]["id"]
        except KeyError, IndexError, TypeError:
            provider_message_id = None
        if not isinstance(provider_message_id, str):
            # A 2xx means Meta took it; without the id we cannot match its statuses.
            raise AmbiguousDeliveryError("send accepted without a message id")
        return provider_message_id

    async def mark_read(self, provider_message_id: str) -> None:
        await self._post(
            {"messaging_product": "whatsapp", "status": "read", "message_id": provider_message_id}
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A per-request header, never ``?access_token=``: a query string ends up in access
        # logs, proxies and exception messages.
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            response = await self._http.post(self._path, json=payload, headers=headers)
        except _NOT_SENT as exc:
            log.warning("whatsapp.graph_unreachable", error_type=type(exc).__name__)
            raise TransientChannelError("Graph API unreachable") from exc
        except httpx.HTTPError as exc:
            # The request may have reached Meta: a read timeout, a reset mid-response.
            log.warning("whatsapp.graph_no_response", error_type=type(exc).__name__)
            raise AmbiguousDeliveryError("no response from the Graph API") from exc

        if response.is_success:
            try:
                data = response.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        code = _error_code(response)
        log.warning("whatsapp.graph_error", status_code=response.status_code, error_code=code)
        if response.status_code == 429 or response.status_code >= 500 or code in THROTTLING_CODES:
            raise TransientChannelError(f"Graph API {response.status_code}")
        raise RejectedChannelError(code)


__all__ = ["THROTTLING_CODES", "WhatsAppOutbound"]
