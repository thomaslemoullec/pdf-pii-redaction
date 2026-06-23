"""Unit tests for the free-form description → PII type planner.

The model's *intelligence* (does it map English/German prose to the right types?) is a live
concern verified against the real model at deploy time. Here we cover the deterministic parts
with no model: the vocabulary filter and the response-parse + request-build glue, exercised
through an injected fake client.
"""

from __future__ import annotations

import types as _t
from dataclasses import replace

import pytest

from pdf_anonymiser.config import Settings
from pdf_anonymiser.pii import PiiType
from pdf_anonymiser.pii_type_agent import plan_pii_types, types_from_names

# --- types_from_names: the pure vocabulary filter ---------------------------


def test_types_from_names_filters_dedupes_and_orders() -> None:
    out = types_from_names(["iban_account", "NAME", "name", "not_a_type", "email"])
    assert out == [PiiType.NAME, PiiType.IBAN_ACCOUNT, PiiType.EMAIL]


def test_types_from_names_trims_whitespace_and_case() -> None:
    assert types_from_names(["  Name ", "EMAIL", "Iban_Account"]) == [
        PiiType.NAME, PiiType.IBAN_ACCOUNT, PiiType.EMAIL
    ]


def test_types_from_names_drops_everything_unknown() -> None:
    assert types_from_names([]) == []
    assert types_from_names(["nonsense", "", "  ", "passport"]) == []  # 'passport' isn't a value


def test_types_from_names_accepts_full_vocabulary() -> None:
    every = [t.value for t in PiiType]
    assert types_from_names(every) == list(PiiType)  # all valid, canonical order


# --- plan_pii_types: blank short-circuit (never calls a model) --------------


class _ExplodingClient:
    @property
    def models(self):  # type: ignore[no-untyped-def]
        raise AssertionError("the model must not be called for a blank description")


@pytest.mark.parametrize("blank", ["", "   ", "\n\t ", None])
def test_plan_blank_description_is_unscoped_without_a_model_call(blank) -> None:  # type: ignore[no-untyped-def]
    # blank/None short-circuits to [] and never touches a model
    assert plan_pii_types(blank, Settings.from_env(), client=_ExplodingClient()) == []


# --- plan_pii_types: the response-parse + request-build glue (fake client) --


class _FakePlanClient:
    """Mimics genai's client.models.generate_content for the planner."""

    def __init__(self, *, parsed_types=None, text="", raises=None):  # type: ignore[no-untyped-def]
        self._parsed_types = parsed_types
        self._text = text
        self._raises = raises
        self.calls: list[dict] = []  # type: ignore[type-arg]

    @property
    def models(self):  # type: ignore[no-untyped-def]
        return self

    def generate_content(self, *, model, contents, config):  # type: ignore[no-untyped-def]
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._raises is not None:
            raise self._raises
        parsed = (
            _t.SimpleNamespace(types=self._parsed_types)
            if self._parsed_types is not None else None
        )
        return _t.SimpleNamespace(parsed=parsed, text=self._text)


def test_plan_maps_structured_response_and_drops_hallucinations() -> None:
    client = _FakePlanClient(parsed_types=["name", "iban_account", "made_up_type", "email"])
    out = plan_pii_types("KYC forms", Settings.from_env(), client=client)
    assert out == [PiiType.NAME, PiiType.IBAN_ACCOUNT, PiiType.EMAIL]


def test_plan_falls_back_to_raw_text_when_parsed_missing() -> None:
    client = _FakePlanClient(parsed_types=None, text='{"types": ["email", "phone"]}')
    out = plan_pii_types("contact sheet", Settings.from_env(), client=client)
    assert out == [PiiType.PHONE, PiiType.EMAIL]  # canonical order (phone before email)


def test_plan_malformed_response_degrades_to_empty_scope() -> None:
    # neither a parsed object nor parseable text → safe empty scope (scan everything)
    client = _FakePlanClient(parsed_types=None, text="not json at all")
    assert plan_pii_types("something", Settings.from_env(), client=client) == []


def test_plan_builds_a_grounded_zero_temperature_request() -> None:
    client = _FakePlanClient(parsed_types=["name"])
    settings = Settings.from_env()
    plan_pii_types("passports", settings, client=client)
    call = client.calls[0]
    assert call["model"] == settings.planner_model  # the lightweight Flash planner
    assert call["config"].temperature == 0.0  # deterministic
    assert "passports" in call["contents"][0]  # the description is in the prompt


def test_plan_propagates_after_retries_exhausted() -> None:
    client = _FakePlanClient(raises=RuntimeError("vertex down"))
    settings = replace(Settings.from_env(), gemini_max_attempts=1, gemini_retry_base_delay_s=0.0)
    with pytest.raises(RuntimeError, match="vertex down"):
        plan_pii_types("anything", settings, client=client)
    assert len(client.calls) == 1  # one attempt, then surfaced
