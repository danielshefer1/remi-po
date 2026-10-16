"""The worker-side half of the GOWA channel: sends through the gateway's REST API.

As with Meta there is no idempotency key, so every failure is sorted by one question --
*could the message have gone out?* (docs/adr/0003). The gateway's answers, from its source
(``pkg/error``, ``ui/rest/middleware/recovery.go``):

- **Transient** -- no. We never reached it; or it is up but its WhatsApp session is not
  (401 ``AUTHENTICATION_ERROR``: "not connected" / "not logged in" -- and a wrong basic-auth
  password, which also stops before sending); or it has no client (500 ``INVALID_WA_CLI``);
  or 429 for anything but the reach-out timelock; or 502/503 from in front of it.
- **Ambiguous** -- maybe. The request left and the answer never came back, or the gateway
  timed out waiting for WhatsApp (504 ``GATEWAY_TIMEOUT``, 408), or failed some other way
  mid-send (any other 5xx).
- **Rejected** -- no, and a retry would be refused the same way: not a WhatsApp number
  (400 ``INVALID_JID``), a malformed request, or WhatsApp's anti-spam timelock on starting a
  new chat (429 ``WA_REACHOUT_TIMELOCK``, recorded under WhatsApp's own code, 463).

Downloads (Iteration 06) are GETs: any failure that may pass is transient, a refusal is
``MediaUnavailableError``.

Only the HTTP status and the gateway's error code are logged, never the body: its error
messages can quote the recipient back.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any, Final
from urllib.parse import quote

import httpx
import structlog

from personal_organizer.core.errors import (
    AmbiguousDeliveryError,
    MediaUnavailableError,
    MessageTooLongError,
    RejectedChannelError,
    TransientChannelError,
)
from personal_organizer.core.phone import normalise_e164
from personal_organizer.interfaces.channel import OutboundMessage
from personal_organizer.interfaces.voice import MAX_AUDIO_BYTES, Audio
from personal_organizer.messaging.text import WHATSAPP_TEXT_LIMIT
from personal_organizer.providers.channel.gowa.inbound import CHANNEL_NAME
from personal_organizer.providers.channel.gowa.parser import phone_jid
from personal_organizer.settings import GowaSettings

log = structlog.get_logger(__name__)

SEND_PATH: Final = "send/message"
PRESENCE_PATH: Final = "send/chat-presence"
DOWNLOAD_PATH: Final = "message/{message_id}/download"
#: The gateway fetches from WhatsApp's CDN before it answers: slower than a send.
DOWNLOAD_TIMEOUT: Final = httpx.Timeout(30.0, connect=5.0)
#: Statuses where the gateway itself is not ready: worth another attempt.
_RETRY_STATUSES: Final = frozenset({401, 408, 429})
DEVICE_HEADER: Final = "X-Device-Id"

#: WhatsApp's own number for the reach-out timelock, stored as the outbox ``error_code``.
REACHOUT_TIMELOCK_CODE: Final = 463
_TIMELOCK: Final = "WA_REACHOUT_TIMELOCK"
#: 500s that mean the gateway had no WhatsApp client to send with: nothing left.
_NO_CLIENT_CODES: Final = frozenset({"INVALID_WA_CLI"})

#: Nothing was sent: the connection never opened, or the pool had no slot.
_NOT_SENT: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

Sleep = Callable[[float], Awaitable[None]]
Uniform = Callable[[float, float], float]


def _error_code(response: httpx.Response) -> str | None:
    try:
        code = response.json()["code"]
    except ValueError, KeyError, TypeError:
        return None
    return code if isinstance(code, str) else None


class GowaOutbound:
    name: str = CHANNEL_NAME

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        device_id: str | None = None,
        typing_delay_s: float = 0.0,
        typing_jitter_s: float = 0.0,
        sleep: Sleep = asyncio.sleep,
        uniform: Uniform = random.uniform,
    ) -> None:
        self._http = http
        self._headers = {DEVICE_HEADER: device_id} if device_id else {}
        self._typing_delay_s = typing_delay_s
        self._typing_jitter_s = typing_jitter_s
        self._sleep = sleep
        self._uniform = uniform

    @classmethod
    def from_settings(cls, settings: GowaSettings, http: httpx.AsyncClient) -> GowaOutbound:
        return cls(
            http,
            device_id=settings.device_id,
            typing_delay_s=settings.typing_delay_s,
            typing_jitter_s=settings.typing_jitter_s,
        )

    def __repr__(self) -> str:
        return f"GowaOutbound(device_id={self._headers.get(DEVICE_HEADER)!r})"

    async def send_text(self, message: OutboundMessage) -> str:
        if len(message.body) > WHATSAPP_TEXT_LIMIT:
            raise MessageTooLongError(len(message.body), WHATSAPP_TEXT_LIMIT)
        phone = normalise_e164(message.recipient)
        if phone is None:
            raise RejectedChannelError(None)
        jid = phone_jid(phone)
        await self._typing(jid)
        data = await self._post(SEND_PATH, {"phone": jid, "message": message.body})
        results = data.get("results")
        provider_message_id = results.get("message_id") if isinstance(results, dict) else None
        if not isinstance(provider_message_id, str) or not provider_message_id:
            # A 2xx means the gateway took it; without the id we cannot match receipts.
            raise AmbiguousDeliveryError("send accepted without a message id")
        return provider_message_id

    async def mark_read(self, provider_message_id: str) -> None:
        """A no-op: the gateway's read endpoint needs the chat's JID, which this protocol
        does not carry, and blue ticks are cosmetic."""

    async def download(
        self, provider_message_id: str, *, phone: str | None, user_id: str | None
    ) -> Audio:
        """A received message's media, saved by the gateway and fetched from it (Iteration 06).

        Only the path of the gateway's ``file_url`` is used, through this client: the worker
        never goes to a host a response names.
        """
        chat = phone_jid(phone) if phone else user_id
        if not chat:
            raise MediaUnavailableError("no chat to download from")
        path = DOWNLOAD_PATH.format(message_id=quote(provider_message_id, safe=""))
        response = await self._get(path, params={"phone": chat})
        try:
            data = response.json()
        except ValueError:
            data = None
        results = data.get("results") if isinstance(data, dict) else None
        results = results if isinstance(results, dict) else {}
        file_url, size = results.get("file_url"), results.get("file_size")
        if not isinstance(file_url, str) or not file_url:
            raise MediaUnavailableError("the gateway saved no file")
        if isinstance(size, int) and size > MAX_AUDIO_BYTES:
            raise MediaUnavailableError("audio too large")
        file_path = httpx.URL(file_url).path.lstrip("/")
        # shortcut: the file is read whole into memory before its size is checked when the
        # gateway gave no file_size; fine at 20 MB on our own gateway, stream it if it ever
        # serves files it did not size.
        content = (await self._get(file_path)).content
        if len(content) > MAX_AUDIO_BYTES:
            raise MediaUnavailableError("audio too large")
        filename = results.get("filename")
        if not isinstance(filename, str) or "." not in filename:
            filename = file_path.rsplit("/", 1)[-1] or "voice.ogg"
        return Audio(content, filename)

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = await self._http.get(
                path, params=params, headers=self._headers, timeout=DOWNLOAD_TIMEOUT
            )
        except httpx.HTTPError as exc:
            log.warning("gowa.download_failed", error_type=type(exc).__name__)
            raise TransientChannelError("GOWA gateway unreachable") from exc
        if response.is_success:
            return response
        status, code = response.status_code, _error_code(response)
        log.warning("gowa.download_error", status_code=status, reason=code)
        if status >= httpx.codes.INTERNAL_SERVER_ERROR or status in _RETRY_STATUSES:
            raise TransientChannelError(f"GOWA {status}")
        raise MediaUnavailableError(f"GOWA {status}")

    async def _typing(self, jid: str) -> None:
        """Best effort: a typing indicator, then a pause of varying length. Never fails the
        send."""
        if self._typing_delay_s <= 0 and self._typing_jitter_s <= 0:
            return
        try:
            response = await self._http.post(
                PRESENCE_PATH, json={"phone": jid, "action": "start"}, headers=self._headers
            )
        except httpx.HTTPError as exc:
            log.info("gowa.presence_failed", error_type=type(exc).__name__)
        else:
            if not response.is_success:
                log.info("gowa.presence_failed", status_code=response.status_code)
        await self._sleep(self._typing_delay_s + self._uniform(0.0, self._typing_jitter_s))

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(path, json=payload, headers=self._headers)
        except _NOT_SENT as exc:
            log.warning("gowa.unreachable", error_type=type(exc).__name__)
            raise TransientChannelError("GOWA gateway unreachable") from exc
        except httpx.HTTPError as exc:
            # The request may have reached the gateway: a read timeout, a reset mid-response.
            log.warning("gowa.no_response", error_type=type(exc).__name__)
            raise AmbiguousDeliveryError("no response from the GOWA gateway") from exc

        if response.is_success:
            try:
                data = response.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        status, code = response.status_code, _error_code(response)
        log.warning("gowa.error", status_code=status, reason=code)
        if status == httpx.codes.TOO_MANY_REQUESTS:
            if code == _TIMELOCK:
                raise RejectedChannelError(REACHOUT_TIMELOCK_CODE)
            raise TransientChannelError(f"GOWA {status}")
        if status in (httpx.codes.UNAUTHORIZED, httpx.codes.BAD_GATEWAY):
            raise TransientChannelError(f"GOWA {status}")
        if status == httpx.codes.SERVICE_UNAVAILABLE or code in _NO_CLIENT_CODES:
            raise TransientChannelError(f"GOWA {status}")
        if status >= httpx.codes.INTERNAL_SERVER_ERROR or status == httpx.codes.REQUEST_TIMEOUT:
            raise AmbiguousDeliveryError(f"GOWA {status}")
        raise RejectedChannelError(None)


__all__ = ["DEVICE_HEADER", "REACHOUT_TIMELOCK_CODE", "GowaOutbound"]
