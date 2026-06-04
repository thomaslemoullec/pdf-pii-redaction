"""The PII redaction agent — the anonymise → evaluate → decide loop, as tools.

Every signal *and the decision itself* is a small, explicit **tool** with a clear
interface, so the agent is a thin, readable orchestrator and each part is swappable and
unit-testable in isolation:

  - ``SynthesizeTool``  — regenerate the page with PII replaced (the image model). Here
    this is just any :class:`~pdf_anonymiser.synthesize.DocumentAnonymizer`.
  - ``MetricsTool``     — deterministic removal-recall / fidelity from the transcripts.
  - ``LeakTool``        — certified value-carryover leak check (:class:`DlpLeakTool`),
    or :class:`NoLeakTool` when DLP is off.
  - ``JudgeTool``       — the LLM-as-judge subagent (semantic verdict + rationale).
  - ``RedactionPolicy`` — the DECISION tool: retry or stop. **Deterministic on purpose**
    — it gates PII leaks, so it must be auditable and reliable, not probabilistic. It is
    a plain ``decide(report, …)`` interface, so an alternative policy (even an LLM one)
    is a drop-in replacement without touching the agent.
  - ``FeedbackTool``    — turn a failing report into a targeted correction for the next
    attempt (the "optimizer" half of the evaluator-optimizer loop).

The agent loop is bounded by ``max_attempts`` and returns the **best-scoring** attempt
(a certified leak sinks an attempt's score to zero, so a clean-but-imperfect page always
beats a leaking one).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from .redaction_judge import RedactionReport, build_report
from .redaction_metrics import certified_value_leaks, compute_redaction_metrics

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from PIL.Image import Image

    from .redaction_judge import RedactionAnalyzer, RedactionJudge, RedactionJudgement
    from .redaction_metrics import RedactionMetrics
    from .synthesize import DocumentAnonymizer


# --- the decision tool ------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """The policy tool's output: whether to stop, and why (for logging/audit)."""

    stop: bool
    reason: str  # "pass" | "exhausted" | "retry"


class RedactionPolicy:
    """Deterministic decision tool: stop on a clean pass, or when attempts run out.

    Pure and total — the safety gate of the pipeline, so it is intentionally NOT a model
    call. Swappable: anything with the same ``decide`` signature can replace it.
    """

    def decide(
        self, report: RedactionReport, *, attempt: int, max_attempts: int
    ) -> Decision:
        if report.verdict == "pass":
            return Decision(stop=True, reason="pass")
        if attempt >= max_attempts:
            return Decision(stop=True, reason="exhausted")
        return Decision(stop=False, reason="retry")


# --- the feedback tool ------------------------------------------------------


class FeedbackTool:
    """Turn a failing report into a targeted correction for the next synthesis attempt."""

    def correction(self, report: RedactionReport) -> str:
        m = report.metrics
        parts: list[str] = []
        if report.dlp_leaks:
            parts.append(
                f"Cloud DLP still detects these certified PII types in your output: "
                f"{', '.join(report.dlp_leaks)} — these are REAL values that survived; "
                "locate and replace every one with a synthetic equivalent of the same format"
            )
        if m.pii_leaked:
            parts.append(
                f"you LEAKED these PII types: {', '.join(m.leaked_types)} — find and "
                "replace EVERY occurrence, including salutations, headers, and the printed "
                "name under any signature"
            )
        if m.fidelity < m.fidelity_threshold:
            parts.append(
                "you altered non-personal content — change ONLY personal values and keep "
                "every number, code, amount, table value and ID exactly as the original"
            )
        if report.rationale:
            parts.append(f"reviewer note: {report.rationale}")
        return "; ".join(parts) or "remove all remaining personal data, preserve everything else"


# --- signal tools (wrap the existing Gemini/DLP components) -----------------


class MetricsTool:
    """Deterministic metrics from the vision analyzer's transcripts + source PII."""

    def __init__(self, analyzer: RedactionAnalyzer, *, fidelity_threshold: float = 0.9) -> None:
        self._analyzer = analyzer
        self._fidelity_threshold = fidelity_threshold

    def read_source(self, page: Image) -> tuple[str, list[tuple[str, str]]]:
        """Extract the source transcript + PII values once (it doesn't change on retry)."""
        return self._analyzer.analyze_source(page)

    def evaluate(
        self, output: Image, source_analysis: tuple[str, list[tuple[str, str]]]
    ) -> RedactionMetrics:
        source_text, source_pii = source_analysis
        output_text = self._analyzer.transcribe(output)
        return compute_redaction_metrics(
            source_pii, source_text, output_text, fidelity_threshold=self._fidelity_threshold
        )


class JudgeTool:
    """The LLM-as-judge subagent: a semantic verdict + rationale over the two pages."""

    def __init__(self, judge: RedactionJudge) -> None:
        self._judge = judge

    def evaluate(self, source: Image, output: Image) -> RedactionJudgement:
        return self._judge.judge(source, output)


class LeakTool(Protocol):
    """A certified leak tool: read the source values once, then test each output.

    ``corroborate`` is an independent extractor's source PII values (the Gemini analysis),
    used to confirm no-validator types (names) before treating them as a leak.
    """

    def read_source(self, page: Image, page_index: int) -> Any: ...
    def evaluate(
        self, source: Any, output: Image, page_index: int, corroborate: Any = None
    ) -> tuple[str, ...]: ...


class NoLeakTool:
    """The leak tool when DLP is off — always reports no certified leak."""

    def read_source(self, page: Image, page_index: int) -> Any:
        return None

    def evaluate(
        self, source: Any, output: Image, page_index: int, corroborate: Any = None
    ) -> tuple[str, ...]:
        return ()


class CallableLeakTool:
    """Adapt a legacy ``output_rescan(output) -> leak types`` closure to the LeakTool API."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def read_source(self, page: Image, page_index: int) -> Any:
        return None

    def evaluate(
        self, source: Any, output: Image, page_index: int, corroborate: Any = None
    ) -> tuple[str, ...]:
        return tuple(self._fn(output))


class DlpLeakTool:
    """Certified value-carryover leak check via Cloud DLP — the real leak tool.

    Reads the real values on the source page once, then for each generated page reports
    which of those values survived (value-carryover; a synthetic fake of the same type is
    a different value and never trips it). Degrades safely: a DLP error reading the source
    skips the check for the page (the metrics still gate); an error on an attempt reports
    no leak rather than a phantom one. ``detector`` is any object with
    ``scan_values(image, page_index) -> [(type, value), …]``.
    """

    def __init__(self, detector: Any) -> None:
        self._detector = detector

    def read_source(self, page: Image, page_index: int) -> Any:
        try:
            return self._detector.scan_values(page, page_index)
        except Exception as exc:  # noqa: BLE001 — never sink a page on a DLP hiccup
            _log.warning("DLP source value scan failed on page %d: %s", page_index, exc)
            return None

    def evaluate(
        self, source: Any, output: Image, page_index: int, corroborate: Any = None
    ) -> tuple[str, ...]:
        if source is None:  # the source read was skipped (disabled or errored)
            return ()
        try:
            output_values = self._detector.scan_values(output, page_index)
        except Exception as exc:  # noqa: BLE001
            _log.warning("DLP output value scan failed on page %d: %s", page_index, exc)
            return ()
        return certified_value_leaks(source, output_values, corroborate=corroborate)


# --- the agent --------------------------------------------------------------


def _score(report: RedactionReport) -> float:
    """Ranking key for 'best attempt': a certified DLP leak sinks the score to 0."""
    if report.dlp_leaks:
        return 0.0
    return float(report.metrics.score)


class RedactionAgent:
    """Orchestrate synthesise → (metrics ∪ DLP ∪ judge) → policy → retry, via tools."""

    def __init__(
        self,
        *,
        synthesize: DocumentAnonymizer,
        metrics: MetricsTool,
        judge: JudgeTool,
        leak_tool: LeakTool | None = None,
        policy: RedactionPolicy | None = None,
        feedback: FeedbackTool | None = None,
        max_attempts: int = 2,
    ) -> None:
        self._synthesize = synthesize
        self._metrics = metrics
        self._judge = judge
        self._leak: LeakTool = leak_tool or NoLeakTool()
        self._policy = policy or RedactionPolicy()
        self._feedback = feedback or FeedbackTool()
        self._max_attempts = max(1, max_attempts)

    def run(
        self, page: Image, *, hint: str | None = None, page_index: int = 0
    ) -> tuple[Image, RedactionReport]:
        """Anonymise the page, evaluating + retrying with feedback until the policy stops.

        Returns ``(best_image, report)`` — the best-scoring attempt and its report (with
        ``attempts`` set to how many rounds it took).
        """
        source_analysis = self._metrics.read_source(page)
        leak_source = self._leak.read_source(page, page_index)
        # The Gemini-extracted source PII values corroborate DLP's no-validator hits
        # (names): a name "leak" only counts if both extractors saw that value as PII.
        corroborate = source_analysis[1]

        best: tuple[Image, RedactionReport] | None = None
        feedback: str | None = None
        attempt = 0
        for attempt in range(1, self._max_attempts + 1):
            out = self._synthesize.anonymize_page(page, hint=hint, feedback=feedback)
            dlp_leaks = self._leak.evaluate(leak_source, out, page_index, corroborate)
            metrics = self._metrics.evaluate(out, source_analysis)
            judgement = self._judge.evaluate(page, out)
            report = build_report(metrics, judgement, dlp_leaks)

            if best is None or _score(report) > _score(best[1]):
                best = (out, report)

            decision = self._policy.decide(
                report, attempt=attempt, max_attempts=self._max_attempts
            )
            if decision.stop:
                break
            feedback = self._feedback.correction(report)

        assert best is not None  # the loop runs at least once
        out, report = best
        return out, replace(report, attempts=attempt)
