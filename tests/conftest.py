"""Shared test fixtures."""

from __future__ import annotations

import pytest

from pdf_anonymiser.config import Settings


@pytest.fixture(autouse=True)
def _dlp_leak_check_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The certified DLP value-carryover leak check is ON in production, but it needs
    live Cloud DLP. Default it OFF across the unit suite so review tests never reach the
    network; the tests that exercise it enable it explicitly with an injected fake DLP
    scanner (and one test verifies the production default is ON via a clean environment).
    """
    monkeypatch.setenv("PII_DLP_LEAK_CHECK", "0")


@pytest.fixture
def settings() -> Settings:
    """Default settings from the environment (local-dev defaults)."""
    return Settings.from_env()
