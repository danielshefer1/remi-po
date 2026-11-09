"""Does this Anthropic account accept the hosted web tools? (#74): ``pytest -m eval`` with
AGENT__API_KEY.

A turned-off tool would make every agent turn a 400, so these run first. A ``BadRequestError``
is left to propagate: its message (for instance "not enabled") is what the CI log must show.
"""

from __future__ import annotations

import os
from typing import Any, NamedTuple

import anthropic
import pytest

pytestmark = pytest.mark.eval

WEB_TOOLS: list[dict[str, Any]] = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 3,
        "user_location": {"type": "approximate", "timezone": "Asia/Jerusalem"},
    },
    {
        "type": "web_fetch_20250910",
        "name": "web_fetch",
        "max_uses": 2,
        "max_content_tokens": 16000,
        "url_sources": {
            "client_tool_results": {"type": "none"},
            "server_tool_results": {
                "type": "only",
                "tools": [{"type": "tool_reference", "name": "web_search"}],
            },
        },
    },
]


class Probe(NamedTuple):
    client: anthropic.AsyncAnthropic
    model: str

    async def ask(self, messages: list[dict[str, Any]]) -> Any:
        # The SDK's typed params can't express these tools, hence Any.
        create: Any = self.client.messages.create
        return await create(model=self.model, max_tokens=1024, messages=messages, tools=WEB_TOOLS)


@pytest.fixture
def llm() -> Probe:
    client = anthropic.AsyncAnthropic(api_key=os.environ["AGENT__API_KEY"], max_retries=2)
    return Probe(client, os.environ.get("MODELS__CHAT_MODEL", "claude-haiku-5-5"))


@pytest.fixture
def strong() -> Probe:
    client = anthropic.AsyncAnthropic(api_key=os.environ["AGENT__API_KEY"], max_retries=0)
    return Probe(client, os.environ.get("MODELS__STRONG_MODEL", "claude-sonnet-5-5"))


@pytest.mark.parametrize("which", ["llm", "strong"])
async def test_web_probe_accepted(which: str, request: pytest.FixtureRequest) -> None:
    probe: Probe = request.getfixturevalue(which)
    reply = await probe.ask([{"role": "user", "content": "Reply with the word OK."}])
    assert reply.stop_reason == "end_turn"


async def test_web_probe_searches(llm: Probe) -> None:
    reply = await llm.ask(
        [
            {
                "role": "user",
                "content": "Search the web for today's weather in Tel Aviv and answer in one line.",
            }
        ]
    )
    types = [block.type for block in reply.content]
    assert types.count("web_search_tool_result") == 1, (types, reply.stop_reason)


def _earlier_turn(result: dict[str, Any], use: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "Look something up."},
        {
            "role": "assistant",
            "content": [{"type": "server_tool_use", "id": "srvtoolu_01probe", **use}, result],
        },
        {"role": "user", "content": "What does it say?"},
    ]


async def test_web_probe_fake_page(llm: Probe) -> None:
    result = {
        "type": "web_fetch_tool_result",
        "tool_use_id": "srvtoolu_01probe",
        "content": {
            "type": "web_fetch_result",
            "url": "https://example.com/",
            "content": {
                "type": "document",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": "Example Domain. This domain is for use in examples.",
                },
            },
        },
    }
    use = {"name": "web_fetch", "input": {"url": "https://example.com/"}}
    await llm.ask(_earlier_turn(result, use))


async def test_web_probe_fake_error(llm: Probe) -> None:
    result = {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_01probe",
        "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"},
    }
    use = {"name": "web_search", "input": {"query": "weather in Tel Aviv"}}
    await llm.ask(_earlier_turn(result, use))
