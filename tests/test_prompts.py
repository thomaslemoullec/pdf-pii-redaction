"""Tests for the versioned prompt registry."""

from __future__ import annotations

import pytest

from pdf_anonymiser.prompts import anonymize_prompt, pii_prompt


def test_pii_prompt_returns_text_and_forbids_values() -> None:
    text = pii_prompt()
    assert "type" in text
    # the scanner must never be asked to return the PII values themselves
    assert "never output the actual value" in text.lower()


def test_anonymize_prompt_keeps_non_personal_content() -> None:
    text = anonymize_prompt()
    assert "synthetic" in text.lower()
    assert "Change ONLY personal-data values" in text


def test_unknown_version_raises() -> None:
    with pytest.raises(KeyError):
        pii_prompt("nope")
    with pytest.raises(KeyError):
        anonymize_prompt("nope")
