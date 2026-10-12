"""Evals call the real model, so they cost money and need a key.

They run only when asked for (``pytest -m eval``) *and* ``AGENT__API_KEY`` is set; otherwise
they are skipped, so the default run and CI never reach the provider.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    markexpr: str = config.option.markexpr or ""
    if "eval" in markexpr and "not eval" not in markexpr and os.environ.get("AGENT__API_KEY"):
        return
    skip = pytest.mark.skip(reason="evals call the real model: pytest -m eval, AGENT__API_KEY")
    for item in items:
        if item.get_closest_marker("eval") is not None:
            item.add_marker(skip)
