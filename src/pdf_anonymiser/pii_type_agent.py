"""Free-form description → PII type scope (a small LLM planner).

Sampling a few documents to guess a *diverse* corpus's PII is unreliable. Instead the
operator describes the documents or the PII they expect in plain language ("German KYC
account-opening forms — names, IBANs, dates of birth, signatures"); a constrained
Gemini call maps that to our :class:`PiiType` vocabulary, which scopes DLP for the
batch. Optional: an empty description returns ``[]`` (no scope → scan for everything).

The model output is filtered against the controlled vocabulary, so a hallucinated type
is dropped rather than trusted. The mapping/validation is a pure function
(:func:`types_from_names`), unit-tested without a model; only the single Gemini call is
live glue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .pii import PiiType
from .retry import retry_call

if TYPE_CHECKING:
    from .config import Settings

_VALID = {t.value for t in PiiType}


def types_from_names(names: list[str]) -> list[PiiType]:
    """Keep only valid :class:`PiiType` values, de-duplicated, in vocabulary order.

    Pure + total: unknown/hallucinated names are dropped, order is the canonical enum
    order (so the picker renders deterministically regardless of what the model emits).
    """
    chosen = {n.strip().lower() for n in names} & _VALID
    return [t for t in PiiType if t.value in chosen]


def plan_pii_types(
    description: str, settings: Settings, *, client: Any = None
) -> list[PiiType]:
    """Map a free-form description to a scoped list of PII types (empty ⇒ no scope).

    Returns ``[]`` for a blank description — the caller treats that as "scan for
    everything" (DLP unscoped). Otherwise a single structured Gemini call selects from
    the controlled vocabulary; the result is validated by :func:`types_from_names`.
    """
    text = (description or "").strip()
    if not text:
        return []
    return _plan_live(text, settings, client)


def _plan_live(text: str, settings: Settings, client: Any) -> list[PiiType]:
    from google.genai import types as genai_types
    from pydantic import BaseModel, Field

    class _Plan(BaseModel):
        model_config = {"frozen": True}

        types: list[str] = Field(
            default_factory=list,
            description="PII types plausibly present, each EXACTLY one of the allowed values",
        )

    if client is None:
        from .gemini_client import make_client

        client = make_client(settings)

    allowed = ", ".join(sorted(_VALID))
    prompt = (
        "You scope a PII-detection job. Given a description of a document set, list the "
        "PII types plausibly present, choosing ONLY from this exact set of values: "
        f"{allowed}. Be inclusive when the description is vague, but never invent values "
        "outside the set. Map natural language to the vocabulary (e.g. 'tax id', "
        "'passport number' → id_number; 'bank account', 'IBAN' → iban_account; "
        "'handwritten signature' → signature). Return strictly the JSON schema.\n\n"
        f"Description: {text}"
    )

    def _call() -> Any:
        return client.models.generate_content(
            model=settings.planner_model,  # lightweight Flash tier, temperature 0
            contents=[prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_Plan,
                temperature=0.0,
            ),
        )

    response = retry_call(
        _call,
        attempts=settings.gemini_max_attempts,
        base_delay=settings.gemini_retry_base_delay_s,
    )
    # Parse defensively: pull `.parsed.types` if present, else fall back to the raw
    # JSON `.text`. Either way types_from_names drops anything off-vocabulary, so a
    # malformed or partial response degrades to a safe (possibly empty) scope.
    parsed = getattr(response, "parsed", None)
    names = list(getattr(parsed, "types", None) or [])
    if not names:
        names = _names_from_text(getattr(response, "text", "") or "")
    return types_from_names(names)


def _names_from_text(text: str) -> list[str]:
    """Best-effort extraction of a ``types`` list from a raw JSON response body."""
    import json

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    value = data.get("types") if isinstance(data, dict) else None
    return [str(v) for v in value] if isinstance(value, list) else []
