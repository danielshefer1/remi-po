"""GOWA media download over ``httpx.MockTransport`` (Iteration 06)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from personal_organizer.core.errors import MediaUnavailableError, TransientChannelError
from personal_organizer.interfaces.voice import MAX_AUDIO_BYTES, MediaSource
from personal_organizer.providers.channel.gowa.outbound import GowaOutbound

BASE = "http://gowa.railway.internal:3000"
OGG = b"OggS\x00fake-opus"

Handler = Callable[[httpx.Request], httpx.Response]


def _gowa(handler: Handler) -> tuple[GowaOutbound, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(base_url=f"{BASE}/", transport=httpx.MockTransport(record))
    return GowaOutbound(http, device_id="dev-1"), seen


def _ok(file_url: str | None = f"{BASE}/statics/media/voice.ogg", size: int = len(OGG)) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/message/"):
            results: dict[str, object] = {
                "media_type": "audio",
                "filename": "voice.ogg",
                "file_size": size,
            }
            if file_url is not None:
                results["file_url"] = file_url
            return httpx.Response(200, json={"code": "SUCCESS", "results": results})
        return httpx.Response(200, content=OGG)

    return handler


def _status(status: int) -> Handler:
    return lambda request: httpx.Response(status, json={"code": "X"})


def test_gowa_is_a_media_source() -> None:
    gowa, _ = _gowa(_ok())
    assert isinstance(gowa, MediaSource)


async def test_download_asks_the_gateway_then_fetches_the_file() -> None:
    gowa, seen = _gowa(_ok())
    audio = await gowa.download("3EB0ABC", phone="+972521234567", user_id=None)
    assert (audio.content, audio.filename) == (OGG, "voice.ogg")
    first, second = seen
    assert first.url.path == "/message/3EB0ABC/download"
    assert first.url.params["phone"] == "972521234567@s.whatsapp.net"
    assert first.headers["X-Device-Id"] == "dev-1"
    assert second.url.path == "/statics/media/voice.ogg"


async def test_a_lid_sender_is_named_by_its_lid() -> None:
    gowa, seen = _gowa(_ok())
    await gowa.download("3EB0ABC", phone=None, user_id="123456789@lid")
    assert seen[0].url.params["phone"] == "123456789@lid"


async def test_only_the_path_of_file_url_is_fetched() -> None:
    gowa, seen = _gowa(_ok(file_url="http://169.254.169.254/statics/media/voice.ogg"))
    await gowa.download("3EB0ABC", phone="+972521234567", user_id=None)
    assert seen[1].url.host == "gowa.railway.internal"
    assert seen[1].url.path == "/statics/media/voice.ogg"


@pytest.mark.parametrize("status", [400, 404])
async def test_a_refused_download_is_unavailable(status: int) -> None:
    gowa, _ = _gowa(_status(status))
    with pytest.raises(MediaUnavailableError):
        await gowa.download("3EB0ABC", phone="+972521234567", user_id=None)


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_a_gateway_hiccup_is_transient(status: int) -> None:
    gowa, _ = _gowa(_status(status))
    with pytest.raises(TransientChannelError):
        await gowa.download("3EB0ABC", phone="+972521234567", user_id=None)


async def test_an_unreachable_gateway_is_transient() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    gowa, _ = _gowa(boom)
    with pytest.raises(TransientChannelError):
        await gowa.download("3EB0ABC", phone="+972521234567", user_id=None)


async def test_no_file_url_is_unavailable() -> None:
    gowa, _ = _gowa(_ok(file_url=None))
    with pytest.raises(MediaUnavailableError):
        await gowa.download("3EB0ABC", phone="+972521234567", user_id=None)


async def test_a_file_too_big_is_never_fetched() -> None:
    gowa, seen = _gowa(_ok(size=MAX_AUDIO_BYTES + 1))
    with pytest.raises(MediaUnavailableError):
        await gowa.download("3EB0ABC", phone="+972521234567", user_id=None)
    assert len(seen) == 1


async def test_no_sender_to_name_is_unavailable() -> None:
    gowa, seen = _gowa(_ok())
    with pytest.raises(MediaUnavailableError):
        await gowa.download("3EB0ABC", phone=None, user_id=None)
    assert seen == []
