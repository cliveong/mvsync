"""Shared test setup."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _keep_the_suite_attached(monkeypatch):
    """Never let a test spawn a real, console-less window."""
    from mvsync import cli

    monkeypatch.setenv(cli._NO_DETACH_ENV, "1")
