"""Unit tests for the deterministic redaction metrics (ADR-0019)."""

from __future__ import annotations

from pdf_anonymiser.redaction_metrics import (
    RedactionMetrics,
    certified_value_leaks,
    compute_redaction_metrics,
)
from pdf_anonymiser.pii import PiiType


def test_perfect_anonymisation_passes() -> None:
    src_pii = [("name", "John Smith"), ("iban", "DE89 3704 0044")]
    source = "KYC Form Name John Smith IBAN DE89 3704 0044 Branch Frankfurt"
    output = "KYC Form Name Anna Mueller IBAN DE45 5001 0517 Branch Frankfurt"
    m = compute_redaction_metrics(src_pii, source, output)
    assert m.pii_leaked == 0 and m.removal_recall == 1.0
    assert m.fidelity == 1.0  # KYC/Form/Name/IBAN/Branch/Frankfurt all preserved
    assert m.verdict == "pass" and m.score == 1.0


def test_leak_is_a_false_negative_and_fails() -> None:
    src_pii = [("name", "John Smith"), ("iban", "DE89 3704 0044")]
    source = "Name John Smith IBAN DE89 3704 0044"
    output = "Name John Smith IBAN DE45 5001 0517"  # name leaked
    m = compute_redaction_metrics(src_pii, source, output)
    assert m.pii_leaked == 1 and "name" in m.leaked_types
    assert m.removal_recall == 0.5
    assert m.verdict == "fail"


def test_collateral_edit_is_a_false_positive_and_reviews() -> None:
    # All PII removed, but a non-PII number was altered → fidelity drops.
    src_pii = [("name", "John Smith")]
    source = "Name John Smith Project 12345 Weight 67 Branch Frankfurt Office Tower"
    output = "Name Anna Mueller Project 99999 Weight 88 Branch Frankfurt Office Tower"
    m = compute_redaction_metrics(src_pii, source, output, fidelity_threshold=0.95)
    assert m.pii_leaked == 0
    assert m.nonpii_changed == 2  # 12345, 67 changed
    assert m.fidelity < 1.0 and m.verdict == "review"


def test_no_pii_is_clean() -> None:
    m = compute_redaction_metrics([], "Just a heading and a table", "Just a heading and a table")
    assert m.removal_recall == 1.0 and m.fidelity == 1.0 and m.verdict == "pass"


def test_summary_is_human_readable() -> None:
    m = compute_redaction_metrics([("name", "John Smith")], "Name John Smith", "Name Anna Mueller")
    s = m.summary()
    assert "score" in s and "removal recall" in s and "fidelity" in s


# --- certified value-carryover leak check (DLP values: source vs synthetic) ----


def test_carryover_flags_a_value_that_survived() -> None:
    src = [(PiiType.IBAN_ACCOUNT, "DE89 3704 0044 0532 0130 00"), (PiiType.NAME, "Hans Müller")]
    out = [(PiiType.IBAN_ACCOUNT, "DE89 3704 0044 0532 0130 00")]  # IBAN survived
    assert certified_value_leaks(src, out) == ("iban_account",)


def test_carryover_ignores_synthetic_replacements() -> None:
    # different values of the same types → no leak (the whole point vs type-presence)
    src = [(PiiType.IBAN_ACCOUNT, "DE89 3704 0044"), (PiiType.NAME, "Hans Müller")]
    out = [(PiiType.IBAN_ACCOUNT, "GB29 NWBK 6016"), (PiiType.NAME, "Anna Schmidt")]
    assert certified_value_leaks(src, out) == ()


def test_carryover_is_formatting_insensitive() -> None:
    # a survivor that was merely reformatted (spaces/punctuation/case) is still caught
    src = [(PiiType.IBAN_ACCOUNT, "DE89 3704 0044 0532 0130 00")]
    out = [(PiiType.IBAN_ACCOUNT, "de89-3704-0044-0532-0130-00")]
    assert certified_value_leaks(src, out) == ("iban_account",)


def test_carryover_returns_multiple_sorted_types() -> None:
    src = [(PiiType.IBAN_ACCOUNT, "DE89 3704 0044"), (PiiType.PHONE, "+49 151 12345678")]
    out = [(PiiType.PHONE, "+4915112345678"), (PiiType.IBAN_ACCOUNT, "DE89-3704-0044")]
    assert certified_value_leaks(src, out) == ("iban_account", "phone")


def test_carryover_ignores_too_short_values() -> None:
    # below the min length a squashed value is too collision-prone to trust
    assert certified_value_leaks([(PiiType.PHONE, "12")], [(PiiType.PHONE, "12")]) == ()


def test_carryover_handles_empty_inputs() -> None:
    assert certified_value_leaks([], []) == ()
    assert certified_value_leaks([(PiiType.IBAN_ACCOUNT, "DE89 3704 0044")], []) == ()
    assert certified_value_leaks([], [(PiiType.IBAN_ACCOUNT, "DE89 3704 0044")]) == ()


def test_carryover_accepts_plain_string_types() -> None:
    # works whether the type is a PiiType (StrEnum) or a plain string
    assert certified_value_leaks([("iban_account", "DE89 3704 0044")],
                                 [("iban_account", "DE89 3704 0044")]) == ("iban_account",)


def test_carryover_name_requires_corroboration() -> None:
    # DLP's PERSON_NAME detector is noisy; a surviving name is only a leak when an
    # independent extractor (Gemini) also saw that exact value as source PII. The
    # validated IBAN always fails on survival regardless.
    src = [(PiiType.NAME, "Hans Müller"), (PiiType.IBAN_ACCOUNT, "DE89 3704 0044")]
    out = [(PiiType.NAME, "Hans Müller"), (PiiType.IBAN_ACCOUNT, "DE89 3704 0044")]
    # uncorroborated: DLP's name hit is ignored; the IBAN still fails
    assert certified_value_leaks(src, out) == ("iban_account",)
    # corroborated by the independent extractor → the surviving name IS a real leak
    assert certified_value_leaks(
        src, out, corroborate=[("name", "Hans Müller")]
    ) == ("iban_account", "name")
    # corroboration of a DIFFERENT name does not flag this one
    assert certified_value_leaks(
        src, out, corroborate=[("name", "Anna Schmidt")]
    ) == ("iban_account",)


# --- fidelity noise tolerance (fuzzy match shrugs off OCR variance) ------------


def test_fidelity_tolerates_ocr_misreads() -> None:
    # The synthetic page's transcription reads one non-PII word slightly differently
    # (frankfurt → frankfvrt). That's OCR noise, not a real edit → fidelity stays 1.0.
    m = compute_redaction_metrics(
        [], "Invoice Reference Frankfurt Office", "Invoice Reference Frankfvrt Office"
    )
    assert m.nonpii_changed == 0 and m.fidelity == 1.0


def test_fidelity_still_flags_a_real_change() -> None:
    # A genuinely different amount must NOT be fuzzed away.
    m = compute_redaction_metrics([], "Total Due 12345", "Total Due 12845")
    assert m.nonpii_changed == 1 and m.fidelity < 1.0


# --- score is the F1 (harmonic mean) of removal recall and fidelity -----------


def _metrics(*, leaked: int, total: int, nonpii_total: int, nonpii_changed: int):  # type: ignore[no-untyped-def]
    return RedactionMetrics(
        pii_total=total, pii_leaked=leaked, leaked_types=(),
        nonpii_total=nonpii_total, nonpii_changed=nonpii_changed,
    )


def test_score_is_harmonic_mean_not_product() -> None:
    # removal 1.0, fidelity 0.74  →  F1 = 2·1·0.74/(1.74) ≈ 0.851 (product would be 0.74)
    m = _metrics(leaked=0, total=6, nonpii_total=100, nonpii_changed=26)
    assert abs(m.removal_recall - 1.0) < 1e-9
    assert abs(m.fidelity - 0.74) < 1e-9
    assert m.score == 0.851


def test_score_perfect_is_one() -> None:
    assert _metrics(leaked=0, total=2, nonpii_total=10, nonpii_changed=0).score == 1.0


def test_score_total_leak_is_zero() -> None:
    # everything leaked (removal 0) → F1 = 0 regardless of fidelity
    assert _metrics(leaked=4, total=4, nonpii_total=10, nonpii_changed=0).score == 0.0
