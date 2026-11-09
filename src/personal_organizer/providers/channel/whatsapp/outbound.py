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
    AttachmentRefusedError,
    MessageTooLongError,
    RejectedChannelError,
    TransientChannelError,
)
from personal_organizer.interfaces.channel import Attachment, OutboundMessage
from personal_organizer.messaging.text import WHATSAPP_TEXT_LIMIT
from personal_organizer.providers.channel.whatsapp.inbound import CHANNEL_NAME
from personal_organizer.settings import WhatsAppSettings

log = structlog.get_logger(__name__)

#: Graph error codes that mean "slow down", which Meta may send with a 400 rather than a 429.
#: From Meta's error-code reference; confirm against real responses during the live hookup.
THROTTLING_CODES: Final = frozenset({4, 80007, 130429, 131048, 131056})

#: Meta's media reference: 5 MB for an image, 100 MB for a document
#: (https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media/).
MAX_IMAGE_BYTES: Final = 5_000_000
MAX_DOCUMENT_BYTES: Final = 100_000_000
#: Image types: JPEG, PNG and static WebP (.../cloud-api/messages/image-messages).
_IMAGE_TYPES: Final = frozenset({"image/jpeg", "image/png", "image/webp"})
#: Document types: text, PDF, Word, Excel, PowerPoint; `text/calendar` is not listed
#: (.../cloud-api/messages/document-messages).
_DOCUMENT_TYPES: Final = frozenset(
    {
        "text/plain",
        "application/pdf",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)
#: An upload is slower than a text send.
UPLOAD_TIMEOUT: Final = httpx.Timeout(60.0, connect=5.0)

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
        self._media_path = f"{phone_number_id}/media"
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
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.recipient,
        }
        if attachment := message.attachment:
            self._check(attachment)
            kind = "image" if attachment.is_image else "document"
            media: dict[str, Any] = {"id": await self._upload(attachment)}
            if message.body:
                media["caption"] = message.body
            if not attachment.is_image:
                media["filename"] = attachment.filename
            payload |= {"type": kind, kind: media}
        else:
            payload |= {"type": "text", "text": {"preview_url": False, "body": message.body}}
        data = await self._post(payload)
        try:
            provider_message_id = data["messages"][0]["id"]
        except KeyError, IndexError, TypeError:
            provider_message_id = None
        if not isinstance(provider_message_id, str):
            # A 2xx means Meta took it; without the id we cannot match its statuses.
            raise AmbiguousDeliveryError("send accepted without a message id")
        return provider_message_id

    @staticmethod
    def _check(attachment: Attachment) -> None:
        allowed = _IMAGE_TYPES if attachment.is_image else _DOCUMENT_TYPES
        if attachment.mime_type not in allowed:
            raise AttachmentRefusedError("type")
        limit = MAX_IMAGE_BYTES if attachment.is_image else MAX_DOCUMENT_BYTES
        if len(attachment.content) > limit:
            raise AttachmentRefusedError("too_big")

    async def _upload(self, attachment: Attachment) -> str:
        """Upload the bytes, returning the media id. Nothing is sent to the user yet, so any
        failure to get an answer is transient. The real filename (often Hebrew) goes only in
        the message, never in the upload."""
        try:
            response = await self._http.post(
                self._media_path,
                data={"messaging_product": "whatsapp", "type": attachment.mime_type},
                files={"file": ("upload", attachment.content, attachment.mime_type)},
                headers={"Authorization": f"Bearer {self._access_token}"},
                timeout=UPLOAD_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            log.warning("whatsapp.upload_failed", error_type=type(exc).__name__)
            raise TransientChannelError("Graph API unreachable") from exc
        if response.is_success:
            try:
                media_id = response.json()["id"]
            except ValueError, KeyError, TypeError:
                media_id = None
            if isinstance(media_id, str) and media_id:
                return media_id
            raise RejectedChannelError(None)
        code = _error_code(response)
        log.warning("whatsapp.upload_error", status_code=response.status_code, error_code=code)
        if response.status_code == 429 or response.status_code >= 500 or code in THROTTLING_CODES:
            raise TransientChannelError(f"Graph API {response.status_code}")
        raise RejectedChannelError(code)

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
