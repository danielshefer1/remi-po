"""Graph API sends over ``httpx.MockTransport``: the exact requests, and error classification.

Classification is the part that matters. The Cloud API has no idempotency key, so calling a
maybe-delivered failure "transient" means a retry that double-sends to someone's phone.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from personal_organizer.core.errors import (
    AmbiguousDeliveryError,
    MessageTooLongError,
    RejectedChannelError,
    TransientChannelError,
)
from personal_organizer.interfaces.channel import OutboundChannel, OutboundMessage
from personal_organizer.messaging.text import WHATSAPP_TEXT_LIMIT
from personal_organizer.providers.channel.whatsapp.outbound import WhatsAppOutbound

BASE = "https://graph.facebook.com/v24.0/"
TOKEN = "EAAG-graph-token"
OK_SEND = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": "+31612345678", "wa_id": "31612345678"}],
    "messages": [{"id": "wamid.SENT1"}],
}


class Recorder:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def _channel(
    respond: Callable[[httpx.Request], httpx.Response],
) -> tuple[WhatsAppOutbound, Recorder]:
    recorder = Recorder(respond)
    http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(recorder))
    return WhatsAppOutbound(http, phone_number_id="106540352242922", access_token=TOKEN), recorder


def _json(status: int, body: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=body)


def _raise(exc: type[httpx.RequestError]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        raise exc("boom", request=request)

    return respond


def _graph_error(code: int) -> dict[str, Any]:
    return {"error": {"message": "(#131047) Re-engagement message +31612345678", "code": code}}


MESSAGE = OutboundMessage(recipient="+31612345678", body="Got it")


class TestRequests:
    def test_it_is_an_outbound_channel(self) -> None:
        channel, _ = _channel(_json(200, OK_SEND))
        assert isinstance(channel, OutboundChannel)

    async def test_send_text(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND))
        assert await channel.send_text(MESSAGE) == "wamid.SENT1"

        (request,) = recorder.requests
        assert request.method == "POST"
        assert str(request.url) == BASE + "106540352242922/messages"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert "access_token" not in str(request.url)
        assert json.loads(request.content) == {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "+31612345678",
            "type": "text",
            "text": {"preview_url": False, "body": "Got it"},
        }

    async def test_mark_read(self) -> None:
        channel, recorder = _channel(_json(200, {"success": True}))
        await channel.mark_read("wamid.IN1")
        (request,) = recorder.requests
        assert json.loads(request.content) == {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": "wamid.IN1",
        }

    async def test_an_over_long_body_is_refused_before_any_request(self) -> None:
        """Never truncated: "Your appointment is at" is worse than an error."""
        channel, recorder = _channel(_json(200, OK_SEND))
        with pytest.raises(MessageTooLongError):
            await channel.send_text(
                OutboundMessage("+31612345678", "x" * (WHATSAPP_TEXT_LIMIT + 1))
            )
        assert recorder.requests == []

    async def test_exactly_the_limit_is_sent(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND))
        await channel.send_text(OutboundMessage("+31612345678", "x" * WHATSAPP_TEXT_LIMIT))
        assert len(recorder.requests) == 1

    def test_the_token_is_not_in_the_repr(self) -> None:
        channel, _ = _channel(_json(200, OK_SEND))
        assert TOKEN not in repr(channel)


class TestNoGroups:
    @pytest.mark.parametrize(
        "message",
        [
            OutboundMessage(recipient="120363025246125486@g.us", body="hi"),
            OutboundMessage(recipient="+31612345678", body="Q", poll_options=("a", "b")),
        ],
    )
    async def test_groups_and_polls_are_rejected(self, message: OutboundMessage) -> None:
        channel, recorder = _channel(_json(200, OK_SEND))
        with pytest.raises(RejectedChannelError):
            await channel.send_text(message)
        assert recorder.requests == []


class TestClassification:
    @pytest.mark.parametrize(
        "respond",
        [
            pytest.param(_json(429, _graph_error(80007)), id="429"),
            pytest.param(_json(500, {}), id="500"),
            pytest.param(_json(503, "not json"), id="503-no-json"),
            pytest.param(_json(400, _graph_error(130429)), id="400-throughput"),
            pytest.param(_json(400, _graph_error(131056)), id="400-pair-rate"),
            pytest.param(_raise(httpx.ConnectError), id="connect-error"),
            pytest.param(_raise(httpx.ConnectTimeout), id="connect-timeout"),
            pytest.param(_raise(httpx.PoolTimeout), id="pool-timeout"),
        ],
    )
    async def test_transient_means_not_delivered_and_retryable(
        self, respond: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        channel, _ = _channel(respond)
        with pytest.raises(TransientChannelError):
            await channel.send_text(MESSAGE)

    @pytest.mark.parametrize(
        "exc",
        [httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.RemoteProtocolError],
    )
    async def test_ambiguous_means_it_may_have_arrived(self, exc: type[httpx.RequestError]) -> None:
        channel, _ = _channel(_raise(exc))
        with pytest.raises(AmbiguousDeliveryError):
            await channel.send_text(MESSAGE)

    async def test_a_success_without_a_message_id_is_ambiguous(self) -> None:
        channel, _ = _channel(_json(200, {"messages": []}))
        with pytest.raises(AmbiguousDeliveryError):
            await channel.send_text(MESSAGE)

    @pytest.mark.parametrize(
        ("status", "body", "code"),
        [
            pytest.param(400, _graph_error(131047), 131047, id="outside-24h-window"),
            pytest.param(400, _graph_error(131026), 131026, id="undeliverable"),
            pytest.param(401, _graph_error(190), 190, id="expired-token"),
            pytest.param(404, "no json", None, id="no-json"),
        ],
    )
    async def test_rejected_carries_the_code(
        self, status: int, body: Any, code: int | None
    ) -> None:
        channel, _ = _channel(_json(status, body))
        with pytest.raises(RejectedChannelError) as excinfo:
            await channel.send_text(MESSAGE)
        assert excinfo.value.provider_code == code
