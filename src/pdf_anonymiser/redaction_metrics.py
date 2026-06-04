"""Deterministic redaction metrics — measure the anonymisation, don't just describe it.

The LLM judge explains *why* a redaction passed or failed; these tools put **numbers**
on it. Two error modes, two metrics:

- **leak (false negative)** — a real PII value from the source still appears in the
  output → lowers **removal recall** (did we remove all the PII?).
- **collateral edit (false positive)** — non-personal source content was changed or
  dropped → lowers **fidelity** (did we leave everything else alone?).

A composite **score** (recall × fidelity) and a metric-driven **verdict** make the
result comparable run-to-run and explainable, with the LLM rationale alongside.
Pure string maths — no model, fully unit-testable; the model only supplies the
inputs (the source PII values + the two transcriptions).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

# How close an output token must be to a source token to count as "preserved" despite
# transcription noise (1.0 = identical). 0.85 tolerates single-character OCR misreads in
# real words (Müller↔Mueller, O↔0, a dropped accent) but still flags genuine changes — a
# changed amount like 12345→12845 scores ~0.80 and stays "changed". Tune here.
_FIDELITY_FUZZ = 0.85


def _norm(text: str) -> str:
    """Lowercase + collapse whitespace — for value-presence matching."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _tokens(text: str) -> list[str]:
    """Alphanumeric tokens (punctuation stripped), lowercased, length ≥ 2."""
    return [t for t in (re.sub(r"[^\w]", "", w) for w in _norm(text).split()) if len(t) >= 2]


def _is_preserved(token: str, out_tokens: set[str]) -> bool:
    """Did a non-PII source token survive into the output — exactly, or close enough?

    Exact set hit first (fast); otherwise a fuzzy near-match (difflib ratio ≥
    :data:`_FIDELITY_FUZZ`) counts as preserved, so two independent OCR passes that read a
    word slightly differently don't register as a collateral edit. Only invoked for the
    minority of tokens that miss exactly, so it stays cheap. Used ONLY for the fidelity
    (non-PII) side — leak detection stays strict/exact, never fuzzy.
    """
    if token in out_tokens:
        return True
    return bool(difflib.get_close_matches(token, out_tokens, n=1, cutoff=_FIDELITY_FUZZ))


@dataclass(frozen=True)
class RedactionMetrics:
    """Measured outcome of one source→anonymised comparison."""

    pii_total: int  # PII values found in the source
    pii_leaked: int  # of those, how many survived verbatim in the output (false negatives)
    leaked_types: tuple[str, ...]
    nonpii_total: int  # non-PII source tokens
    nonpii_changed: int  # of those, how many are missing from the output (false positives)
    fidelity_threshold: float = 0.9

    @property
    def removal_recall(self) -> float:
        """Fraction of source PII that was removed (1.0 = nothing leaked)."""
        return 1.0 if self.pii_total == 0 else (self.pii_total - self.pii_leaked) / self.pii_total

    @property
    def fidelity(self) -> float:
        """Fraction of non-PII source content preserved (1.0 = no collateral edits)."""
        if self.nonpii_total == 0:
            return 1.0
        return (self.nonpii_total - self.nonpii_changed) / self.nonpii_total

    @property
    def score(self) -> float:
        """Composite in [0,1] — the **F1 (harmonic mean)** of removal recall and fidelity.

        Removal recall and fidelity are a recall/precision pair, so F1 is the principled
        combination: it punishes an imbalance (you can't offset a poor removal with great
        fidelity), but is less harsh than a raw product on otherwise-clean documents. A
        total leak (removal 0) still yields 0. The verdict — not the score — is the hard
        leak gate (any leak ⇒ fail).
        """
        r, f = self.removal_recall, self.fidelity
        return 0.0 if (r + f) == 0 else round(2 * r * f / (r + f), 3)

    @property
    def verdict(self) -> str:
        """Metric-driven: any leak → fail; low fidelity → review; else pass."""
        if self.pii_leaked > 0:
            return "fail"
        if self.fidelity < self.fidelity_threshold:
            return "review"
        return "pass"

    def summary(self) -> str:
        """A one-line numeric headline to sit next to the LLM's explanation."""
        return (
            f"score {self.score:.2f} · removal recall {self.removal_recall:.0%} "
            f"({self.pii_leaked}/{self.pii_total} leaked) · fidelity {self.fidelity:.0%} "
            f"({self.nonpii_changed}/{self.nonpii_total} non-PII tokens changed)"
        )


def compute_redaction_metrics(
    source_pii_values: list[tuple[str, str]],
    source_text: str,
    output_text: str,
    *,
    fidelity_threshold: float = 0.9,
) -> RedactionMetrics:
    """Compute redaction metrics from source PII values + the two transcriptions.

    ``source_pii_values`` is ``[(type, value), …]`` extracted from the source (used
    locally for measurement, never persisted). A value counts as **leaked** when its
    normalised form still appears in the output text. **Collateral** is the fraction
    of non-PII source tokens absent from the output.
    """
    out_norm = _norm(output_text)
    leaked = [(t, v) for t, v in source_pii_values if v.strip() and _norm(v) in out_norm]

    pii_token_set: set[str] = set()
    for _, value in source_pii_values:
        pii_token_set.update(_tokens(value))

    source_tokens = _tokens(source_text)
    nonpii = [tok for tok in source_tokens if tok not in pii_token_set]
    out_token_set = set(_tokens(output_text))
    # A non-PII token is "changed" only if it's neither an exact nor a fuzzy match in the
    # output — so OCR variance between the two transcriptions doesn't deflate fidelity.
    nonpii_changed = sum(1 for tok in nonpii if not _is_preserved(tok, out_token_set))

    return RedactionMetrics(
        pii_total=len(source_pii_values),
        pii_leaked=len(leaked),
        leaked_types=tuple(sorted({t for t, _ in leaked})),
        nonpii_total=len(nonpii),
        nonpii_changed=nonpii_changed,
        fidelity_threshold=fidelity_threshold,
    )


def _squash(text: str) -> str:
    """Aggressively normalise a value for carryover comparison: lowercase, keep only
    alphanumerics. So ``"DE89 3704 0044 0532 0130 00"`` and ``"de89370400440532013000"``
    compare equal — a survivor that was merely *reformatted* is still caught."""
    return re.sub(r"[^0-9a-z]", "", text.lower())


# Below this length a squashed value is too short to compare safely (incidental
# collisions), so the carryover check ignores it.
_MIN_CARRYOVER_LEN = 3

# DLP infotypes with NO validator (PERSON_NAME etc.) over-fire on scanned letterheads,
# labels and signatures, so a survivor of one of these is only treated as a leak when an
# INDEPENDENT extractor (the Gemini source analysis, passed as ``corroborate``) also
# identified that exact value as real PII — i.e. two signals agree it's a real value that
# survived. Validated types (IBAN/credit-card checksums) are trusted on their own.
_CARRYOVER_CORROBORATE_TYPES = frozenset({"name", "other"})


def certified_value_leaks(
    source_values: list[tuple[Any, str]],
    output_values: list[tuple[Any, str]],
    *,
    corroborate: list[tuple[Any, str]] | None = None,
) -> tuple[str, ...]:
    """Value-carryover leak check: which source values *survived* into the output.

    ``source_values`` / ``output_values`` are ``[(type, value), …]`` from a **certified**
    detector (Cloud DLP) over the source and synthetic pages. A source value counts as a
    real leak when its formatting-insensitive form (see :func:`_squash`) appears among the
    output's values — so a SYNTHETIC replacement never trips it, while a survivor that was
    only reformatted is still caught.

    ``corroborate`` is an *independent* extractor's source PII values (the Gemini analysis).
    For no-validator types (see :data:`_CARRYOVER_CORROBORATE_TYPES`) a survivor is only
    flagged when its value also appears here — killing DLP's false "name leaked" verdicts
    on preserved boilerplate while still catching a real name that two signals agree
    survived. Validated types are flagged on survival alone.

    Pure + total: no DLP, no model — fully unit-testable. ``type`` may be a ``PiiType``
    (a ``StrEnum``) or a plain string; the returned types are their string values.
    """
    out = {
        s for _, v in output_values if v and len(s := _squash(v)) >= _MIN_CARRYOVER_LEN
    }
    corr = {_squash(v) for _, v in (corroborate or []) if v}
    leaked: set[str] = set()
    for t, v in source_values:
        if not v or len(s := _squash(v)) < _MIN_CARRYOVER_LEN or s not in out:
            continue
        tv = str(getattr(t, "value", t))
        if tv in _CARRYOVER_CORROBORATE_TYPES and s not in corr:
            continue  # no-validator type the independent extractor didn't confirm
        leaked.add(tv)
    return tuple(sorted(leaked))
