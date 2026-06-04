"""Unit tests for the PII redaction agent — tools + deterministic policy + the loop.

Everything here runs with fakes (no Gemini, no DLP): the agent and each tool are pure
orchestration over injected components.
"""

from __future__ import annotations

from PIL import Image

from pdf_anonymiser.pii import PiiType
from pdf_anonymiser.redaction_agent import (
    CallableLeakTool,
    Decision,
    DlpLeakTool,
    FeedbackTool,
    JudgeTool,
    MetricsTool,
    NoLeakTool,
    RedactionAgent,
    RedactionPolicy,
)
from pdf_anonymiser.redaction_judge import RedactionJudgement, build_report
from pdf_anonymiser.redaction_metrics import RedactionMetrics


def _img() -> Image.Image:
    return Image.new("RGB", (10, 10), "black")


# --- the decision tool (deterministic policy) ------------------------------


def _report(*, leaked: int = 0, fidelity_ok: bool = True, dlp: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    metrics = RedactionMetrics(
        pii_total=2, pii_leaked=leaked, leaked_types=("name",) if leaked else (),
        nonpii_total=10, nonpii_changed=0 if fidelity_ok else 5,
    )
    return build_report(metrics, RedactionJudgement(False, (), True, True, "r"), dlp)


def test_policy_stops_on_clean_pass() -> None:
    d = RedactionPolicy().decide(_report(), attempt=1, max_attempts=3)
    assert d == Decision(stop=True, reason="pass")


def test_policy_retries_on_a_leak_with_attempts_remaining() -> None:
    d = RedactionPolicy().decide(_report(leaked=1), attempt=1, max_attempts=3)
    assert d == Decision(stop=False, reason="retry")


def test_policy_retries_on_low_fidelity() -> None:
    d = RedactionPolicy().decide(_report(fidelity_ok=False), attempt=1, max_attempts=3)
    assert d.stop is False  # "review" verdict still triggers a retry


def test_policy_stops_when_attempts_exhausted_even_if_failing() -> None:
    d = RedactionPolicy().decide(_report(leaked=1), attempt=2, max_attempts=2)
    assert d == Decision(stop=True, reason="exhausted")


def test_policy_certified_dlp_leak_is_a_retry() -> None:
    d = RedactionPolicy().decide(_report(dlp=("iban_account",)), attempt=1, max_attempts=3)
    assert d.stop is False


# --- the feedback tool ------------------------------------------------------


def test_feedback_names_the_certified_dlp_leak() -> None:
    fb = FeedbackTool().correction(_report(dlp=("iban_account",)))
    assert "Cloud DLP" in fb and "iban_account" in fb


def test_feedback_names_metric_leaks_and_fidelity() -> None:
    fb = FeedbackTool().correction(_report(leaked=1, fidelity_ok=False))
    assert "LEAKED" in fb and "name" in fb
    assert "non-personal content" in fb


def test_feedback_has_a_safe_default_when_nothing_specific() -> None:
    metrics = RedactionMetrics(pii_total=0, pii_leaked=0, leaked_types=(), nonpii_total=0, nonpii_changed=0)
    fb = FeedbackTool().correction(build_report(metrics, RedactionJudgement(False, (), True, True, ""), ()))
    assert fb == "remove all remaining personal data, preserve everything else"


# --- leak tools -------------------------------------------------------------


def test_no_leak_tool_never_reports_a_leak() -> None:
    t = NoLeakTool()
    assert t.read_source(_img(), 0) is None
    assert t.evaluate(None, _img(), 0) == ()


def test_callable_leak_tool_adapts_a_closure() -> None:
    t = CallableLeakTool(lambda out: ("iban_account",))
    assert t.read_source(_img(), 0) is None
    assert t.evaluate(None, _img(), 0) == ("iban_account",)


class _FakeDetector:
    def __init__(self, source, output, raise_on=None):  # type: ignore[no-untyped-def]
        self._source, self._output, self._raise_on = source, output, raise_on
        self.calls = 0

    def scan_values(self, image, page_index):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self._raise_on == self.calls:
            raise RuntimeError("DLP down")
        # first call is the source read, later calls are outputs
        return self._source if self.calls == 1 else self._output


def test_dlp_leak_tool_flags_a_surviving_value() -> None:
    det = _FakeDetector(
        source=[(PiiType.IBAN_ACCOUNT, "DE89 3704 0044")],
        output=[(PiiType.IBAN_ACCOUNT, "de89370400 44")],  # same value, reformatted
    )
    t = DlpLeakTool(det)
    src = t.read_source(_img(), 0)
    assert t.evaluate(src, _img(), 0) == ("iban_account",)


def test_dlp_leak_tool_ignores_synthetic_replacements() -> None:
    det = _FakeDetector(
        source=[(PiiType.IBAN_ACCOUNT, "DE89 3704 0044")],
        output=[(PiiType.IBAN_ACCOUNT, "GB29 NWBK 6016")],
    )
    t = DlpLeakTool(det)
    assert t.evaluate(t.read_source(_img(), 0), _img(), 0) == ()


def test_dlp_leak_tool_degrades_when_source_scan_errors() -> None:
    det = _FakeDetector(source=[], output=[], raise_on=1)  # source read raises
    t = DlpLeakTool(det)
    src = t.read_source(_img(), 0)
    assert src is None
    assert t.evaluate(src, _img(), 0) == ()  # skipped, no phantom leak


def test_dlp_leak_tool_degrades_when_output_scan_errors() -> None:
    det = _FakeDetector(source=[(PiiType.NAME, "Hans Müller")], output=[], raise_on=2)
    t = DlpLeakTool(det)
    src = t.read_source(_img(), 0)  # ok
    assert t.evaluate(src, _img(), 0) == ()  # output raises → no phantom leak


# --- the agent loop ---------------------------------------------------------


class _Anon:
    def __init__(self) -> None:
        self.calls = 0
        self.feedbacks: list = []  # type: ignore[type-arg]

    def anonymize_page(self, image, *, hint=None, feedback=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.feedbacks.append(feedback)
        return image


class _Analyzer:
    def __init__(self, transcripts) -> None:  # type: ignore[no-untyped-def]
        self._transcripts = list(transcripts)
        self.t = 0

    def analyze_source(self, image):  # type: ignore[no-untyped-def]
        return ("Name John Smith", [("name", "John Smith")])

    def transcribe(self, image):  # type: ignore[no-untyped-def]
        out = self._transcripts[min(self.t, len(self._transcripts) - 1)]
        self.t += 1
        return out


class _Judge:
    def judge(self, source, output):  # type: ignore[no-untyped-def]
        return RedactionJudgement(False, (), True, True, "r")


def _agent(anon, analyzer, *, leak_tool=None, max_attempts=3):  # type: ignore[no-untyped-def]
    return RedactionAgent(
        synthesize=anon, metrics=MetricsTool(analyzer), judge=JudgeTool(_Judge()),
        leak_tool=leak_tool, max_attempts=max_attempts,
    )


def test_agent_passes_on_first_clean_attempt() -> None:
    anon = _Anon()
    out, report = _agent(anon, _Analyzer(["Name Anna Mueller"])).run(_img())
    assert anon.calls == 1
    assert report.verdict == "pass" and report.attempts == 1
    assert anon.feedbacks == [None]  # no correction needed


def test_agent_retries_with_feedback_then_passes() -> None:
    anon = _Anon()
    # attempt 1 leaks "John Smith", attempt 2 is clean
    out, report = _agent(anon, _Analyzer(["Name John Smith", "Name Anna Mueller"])).run(_img())
    assert anon.calls == 2 and report.verdict == "pass" and report.attempts == 2
    assert anon.feedbacks[0] is None and anon.feedbacks[1] is not None  # correction fed back


def test_agent_exhausts_attempts_and_returns_failing_report() -> None:
    anon = _Anon()
    out, report = _agent(
        anon, _Analyzer(["Name John Smith"]),  # always leaks
        max_attempts=2,
    ).run(_img())
    assert anon.calls == 2 and report.verdict == "fail" and report.attempts == 2


def test_agent_certified_dlp_leak_forces_fail_and_feedback() -> None:
    anon = _Anon()
    out, report = _agent(
        anon, _Analyzer(["Name Anna Mueller"]),  # metrics clean
        leak_tool=CallableLeakTool(lambda o: ("iban_account",)),  # DLP always leaks
        max_attempts=2,
    ).run(_img())
    assert report.verdict == "fail" and report.dlp_leaks == ("iban_account",)
    assert any(f and "Cloud DLP" in f for f in anon.feedbacks)


def test_agent_keeps_the_best_scoring_attempt() -> None:
    # attempt 1: clean (score 1.0). attempt 2 would leak — but we already passed, so the
    # loop stops at 1 and returns the clean one.
    anon = _Anon()
    out, report = _agent(anon, _Analyzer(["Name Anna Mueller", "Name John Smith"])).run(_img())
    assert report.metrics.pii_leaked == 0 and report.attempts == 1
