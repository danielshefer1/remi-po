"""GOWA sends over ``httpx.MockTransport``: the exact requests, and error classification.

As with Meta, classification is what matters: calling a maybe-delivered failure "transient"
means a retry that double-sends to someone's phone. The error codes are the gateway's own,
from its source.
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
from personal_organizer.providers.channel.gowa.outbound import (
    REACHOUT_TIMELOCK_CODE,
    GowaOutbound,
)
from personal_organizer.settings import GowaSettings

BASE = "http://gowa.railway.internal:3000"
OK_SEND = {
    "code": "SUCCESS",
    "message": "Success",
    "results": {"message_id": "3EB0B430B6F8F1D0E053AC", "status": "Message sent"},
}
MESSAGE = OutboundMessage(recipient="+31612345678", body="Got it")


class Recorder:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.draws: list[tuple[float, float]] = []
        #: What the stubbed random draw returns: the jitter's upper bound, scaled by this.
        self.draw_fraction = 0.5

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def uniform(self, low: float, high: float) -> float:
        self.draws.append((low, high))
        return low + (high - low) * self.draw_fraction

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def _channel(
    respond: Callable[[httpx.Request], httpx.Response],
    *,
    typing_delay_s: float = 0.0,
    typing_jitter_s: float = 0.0,
    device_id: str | None = None,
) -> tuple[GowaOutbound, Recorder]:
    recorder = Recorder(respond)
    http = httpx.AsyncClient(
        base_url=BASE, auth=("po", "pw"), transport=httpx.MockTransport(recorder)
    )
    channel = GowaOutbound(
        http,
        device_id=device_id,
        typing_delay_s=typing_delay_s,
        typing_jitter_s=typing_jitter_s,
        sleep=recorder.sleep,
        uniform=recorder.uniform,
    )
    return channel, recorder


def _json(status: int, body: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=body)


def _raise(exc: type[httpx.RequestError]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        raise exc("boom", request=request)

    return respond


def _gowa_error(status: int, code: str) -> Callable[[httpx.Request], httpx.Response]:
    return _json(status, {"code": code, "message": "+31612345678 failed", "results": None})


class TestRequests:
    def test_it_is_an_outbound_channel(self) -> None:
        channel, _ = _channel(_json(200, OK_SEND))
        assert isinstance(channel, OutboundChannel)

    async def test_send_text(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND))
        assert await channel.send_text(MESSAGE) == "3EB0B430B6F8F1D0E053AC"

        (request,) = recorder.requests
        assert request.method == "POST"
        assert request.url.path == "/send/message"
        assert json.loads(request.content) == {
            "phone": "31612345678@s.whatsapp.net",
            "message": "Got it",
        }
        assert request.headers["authorization"].startswith("Basic ")
        assert "x-device-id" not in request.headers

    async def test_send_to_group_jid_posts_jid_unchanged(self) -> None:
        jid = "120363025246125486@g.us"
        channel, recorder = _channel(_json(200, OK_SEND))
        await channel.send_text(OutboundMessage(recipient=jid, body="hi all"))
        assert json.loads(recorder.requests[0].content)["phone"] == jid

    async def test_send_poll_posts_to_send_poll(self) -> None:
        jid = "120363025246125486@g.us"
        channel, recorder = _channel(_json(200, OK_SEND), typing_delay_s=1.0)
        message = OutboundMessage(recipient=jid, body="Where to?", poll_options=("Beach", "Park"))
        assert await channel.send_text(message) == "3EB0B430B6F8F1D0E053AC"
        (request,) = recorder.requests  # no typing indicator for a poll
        assert request.url.path == "/send/poll"
        assert json.loads(request.content) == {
            "phone": jid,
            "question": "Where to?",
            "options": ["Beach", "Park"],
            "max_answer": 1,
        }

    async def test_poll_without_id_is_ambiguous(self) -> None:
        channel, _ = _channel(_json(200, {"code": "SUCCESS", "results": {}}))
        message = OutboundMessage(
            recipient="120363025246125486@g.us", body="Q", poll_options=("a", "b")
        )
        with pytest.raises(AmbiguousDeliveryError):
            await channel.send_text(message)

    async def test_the_device_is_named_when_configured(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND), device_id="po-bot")
        await channel.send_text(MESSAGE)
        assert recorder.requests[0].headers["x-device-id"] == "po-bot"

    async def test_a_typing_indicator_and_a_pause_come_first(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND), typing_delay_s=1.5)
        await channel.send_text(MESSAGE)

        assert recorder.paths == ["/send/chat-presence", "/send/message"]
        assert json.loads(recorder.requests[0].content) == {
            "phone": "31612345678@s.whatsapp.net",
            "action": "start",
        }
        assert recorder.sleeps == [1.5]

    async def test_the_pause_gets_a_random_extra(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND), typing_delay_s=3.0, typing_jitter_s=2.0)
        await channel.send_text(MESSAGE)

        assert recorder.draws == [(0.0, 2.0)]
        assert recorder.sleeps == [4.0]

    async def test_each_reply_draws_its_own_pause(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND), typing_delay_s=3.0, typing_jitter_s=2.0)
        await channel.send_text(MESSAGE)
        recorder.draw_fraction = 1.0
        await channel.send_text(MESSAGE)

        assert recorder.sleeps == [4.0, 5.0]

    async def test_jitter_alone_still_types_and_pauses(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND), typing_jitter_s=2.0)
        await channel.send_text(MESSAGE)

        assert recorder.paths == ["/send/chat-presence", "/send/message"]
        assert recorder.sleeps == [1.0]

    async def test_the_pause_comes_from_settings(self) -> None:
        recorder = Recorder(_json(200, OK_SEND))
        http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(recorder))
        channel = GowaOutbound.from_settings(
            GowaSettings(typing_delay_s=2.5, typing_jitter_s=1.5), http
        )
        assert channel._typing_delay_s == 2.5
        assert channel._typing_jitter_s == 1.5

    async def test_a_failed_typing_indicator_does_not_stop_the_reply(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/send/chat-presence":
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json=OK_SEND)

        channel, recorder = _channel(respond, typing_delay_s=1.0)
        assert await channel.send_text(MESSAGE) == "3EB0B430B6F8F1D0E053AC"
        assert recorder.paths[-1] == "/send/message"

    async def test_a_refused_typing_indicator_does_not_stop_the_reply(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            status = 400 if request.url.path == "/send/chat-presence" else 200
            return httpx.Response(status, json=OK_SEND)

        channel, _ = _channel(respond, typing_delay_s=1.0)
        assert await channel.send_text(MESSAGE) == "3EB0B430B6F8F1D0E053AC"

    async def test_no_delay_means_no_typing_indicator(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND))
        await channel.send_text(MESSAGE)
        assert recorder.paths == ["/send/message"]
        assert recorder.sleeps == []

    async def test_an_overlong_body_is_refused_before_any_request(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND), typing_delay_s=1.0)
        with pytest.raises(MessageTooLongError):
            await channel.send_text(
                OutboundMessage("+31612345678", "x" * (WHATSAPP_TEXT_LIMIT + 1))
            )
        assert recorder.requests == []

    async def test_a_recipient_that_is_not_a_number_is_rejected(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND))
        with pytest.raises(RejectedChannelError):
            await channel.send_text(OutboundMessage("not-a-number", "hi"))
        assert recorder.requests == []

    async def test_mark_read_is_a_no_op(self) -> None:
        channel, recorder = _channel(_json(200, OK_SEND))
        await channel.mark_read("3EB0C127D7BACC83D6A1")
        assert recorder.requests == []

    def test_credentials_are_not_in_the_repr(self) -> None:
        channel, _ = _channel(_json(200, OK_SEND), device_id="po-bot")
        assert "pw" not in repr(channel)
        assert "po-bot" in repr(channel)


class TestClassification:
    @pytest.mark.parametrize(
        "respond",
        [
            _raise(httpx.ConnectError),
            _raise(httpx.ConnectTimeout),
            _raise(httpx.PoolTimeout),
            _gowa_error(401, "AUTHENTICATION_ERROR"),  # not connected / not logged in
            lambda _request: httpx.Response(401, text="Unauthorized"),  # basic auth
            _gowa_error(500, "INVALID_WA_CLI"),
            _gowa_error(429, "TOO_MANY_REQUESTS"),
            lambda _request: httpx.Response(502, text="Bad Gateway"),
            lambda _request: httpx.Response(503, text="Service Unavailable"),
        ],
        ids=[
            "connect",
            "connect-timeout",
            "pool",
            "not-logged-in",
            "basic-auth",
            "no-client",
            "throttled",
            "502",
            "503",
        ],
    )
    async def test_definitely_not_sent_is_transient(
        self, respond: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        channel, _ = _channel(respond)
        with pytest.raises(TransientChannelError):
            await channel.send_text(MESSAGE)

    @pytest.mark.parametrize(
        "respond",
        [
            _raise(httpx.ReadTimeout),
            _raise(httpx.RemoteProtocolError),
            _gowa_error(504, "GATEWAY_TIMEOUT"),
            _gowa_error(408, "CONTEXT_ERROR"),
            _gowa_error(500, "INTERNAL_SERVER_ERROR"),
            _json(200, {"code": "SUCCESS", "results": {}}),
            lambda _request: httpx.Response(200, text="not json"),
        ],
        ids=["read-timeout", "reset", "504", "408", "500", "no-id", "not-json"],
    )
    async def test_maybe_sent_is_ambiguous(
        self, respond: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        channel, _ = _channel(respond)
        with pytest.raises(AmbiguousDeliveryError):
            await channel.send_text(MESSAGE)

    @pytest.mark.parametrize(
        ("respond", "code"),
        [
            (_gowa_error(400, "INVALID_JID"), None),
            (_gowa_error(404, "NOT_FOUND"), None),
            (_gowa_error(429, "WA_REACHOUT_TIMELOCK"), REACHOUT_TIMELOCK_CODE),
        ],
        ids=["not-on-whatsapp", "not-found", "reach-out-timelock"],
    )
    async def test_refused_for_good_is_rejected(
        self, respond: Callable[[httpx.Request], httpx.Response], code: int | None
    ) -> None:
        channel, _ = _channel(respond)
        with pytest.raises(RejectedChannelError) as excinfo:
            await channel.send_text(MESSAGE)
        assert excinfo.value.provider_code == code
