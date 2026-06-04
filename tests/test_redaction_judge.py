"""Unit tests for the redaction judge's verdict logic (ADR-0019)."""

from __future__ import annotations

from pdf_anonymiser.redaction_judge import RedactionJudgement


def _j(**kw) -> RedactionJudgement:  # type: ignore[no-untyped-def]
    base = {
        "leaked": False, "leaked_types": (), "all_pii_removed": True,
        "layout_preserved": True, "rationale": "",
    }
    base.update(kw)
    return RedactionJudgement(**base)  # type: ignore[arg-type]


def test_leak_is_fail() -> None:
    assert _j(leaked=True, leaked_types=("name",)).verdict == "fail"


def test_incomplete_or_drift_is_review() -> None:
    assert _j(all_pii_removed=False).verdict == "review"
    assert _j(layout_preserved=False).verdict == "review"


def test_clean_is_pass() -> None:
    assert _j().verdict == "pass"


def test_combined_verdict_reconciles_metric_and_judge() -> None:
    from pdf_anonymiser.redaction_judge import RedactionReport
    from pdf_anonymiser.redaction_metrics import RedactionMetrics

    clean = RedactionMetrics(pii_total=2, pii_leaked=0, leaked_types=(),
                             nonpii_total=10, nonpii_changed=0)
    # all signals agree → pass
    assert RedactionReport(metrics=clean, rationale="").verdict == "pass"
    # judge doubts completeness (no leak) → review, not a silent pass
    assert RedactionReport(metrics=clean, rationale="", judge_all_removed=False).verdict == "review"
    # the judge alone calling a leak → fail
    assert RedactionReport(metrics=clean, rationale="", judge_leaked=True).verdict == "fail"
    # a metric value-match leak → fail regardless of an optimistic judge
    leaked = RedactionMetrics(pii_total=3, pii_leaked=1, leaked_types=("id_number",),
                              nonpii_total=10, nonpii_changed=0)
    assert RedactionReport(metrics=leaked, rationale="", judge_all_removed=True).verdict == "fail"


# --- RedactionReport: the DLP certified-leak override (judge's tool) ---------


def test_report_verdict_uses_metrics_when_no_dlp_leaks() -> None:
    from pdf_anonymiser.redaction_judge import RedactionReport
    from pdf_anonymiser.redaction_metrics import RedactionMetrics

    clean = RedactionMetrics(pii_total=2, pii_leaked=0, leaked_types=(),
                             nonpii_total=10, nonpii_changed=0)
    assert RedactionReport(metrics=clean, rationale="ok").verdict == "pass"


def test_report_dlp_leak_overrides_clean_metrics_to_fail() -> None:
    from pdf_anonymiser.redaction_judge import RedactionReport
    from pdf_anonymiser.redaction_metrics import RedactionMetrics

    # metrics say clean (no text leak), but the certified DLP tool found an IBAN in
    # the OUTPUT → the report must fail regardless.
    clean = RedactionMetrics(pii_total=1, pii_leaked=0, leaked_types=(),
                             nonpii_total=5, nonpii_changed=0)
    report = RedactionReport(metrics=clean, rationale="looks ok to the eye",
                             dlp_leaks=("iban_account",))
    assert clean.verdict == "pass"  # the metric layer alone would pass
    assert report.verdict == "fail"  # the certified tool overrides
    assert report.dlp_leaks == ("iban_account",)


def test_build_report_assembles_signals_and_dlp_override() -> None:
    # build_report is the single seam the agent uses to combine the three signals.
    from pdf_anonymiser.redaction_judge import build_report
    from pdf_anonymiser.redaction_metrics import RedactionMetrics

    clean = RedactionMetrics(pii_total=1, pii_leaked=0, leaked_types=(),
                             nonpii_total=5, nonpii_changed=0)
    judgement = _j(layout_preserved=False, rationale="layout drifted")

    # no leak, but the judge flagged a layout drift → the combined verdict is "review"
    # (never a silent "pass" while a signal disagrees); booleans + rationale captured.
    report = build_report(clean, judgement)
    assert report.verdict == "review" and report.rationale == "layout drifted"
    assert report.judge_all_removed is True
    assert report.judge_layout_ok is False
    assert report.judge_leaked is False

    # a certified DLP leak overrides clean metrics → fail
    report2 = build_report(clean, judgement, ("name",))
    assert report2.dlp_leaks == ("name",) and report2.verdict == "fail"
