"""PII review service — scan + anonymise + judge one document, for the UI.

Composes the three Tier-A privacy tools into one call the review UI drives: scan
the PII (the central label + routing), synthesise an anonymised version, and have
the LLM judge compare source vs anonymised. The result is everything a human needs
to review the redaction in one screen. Injectable parts so the UI is unit-tested
with fakes (no Gemini); production wires the Gemini-backed implementations.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

_log = logging.getLogger(__name__)

from .pii import (
    DlpPiiDetector,
    EnsemblePiiDetector,
    GeminiPiiDetector,
    PiiDetector,
    PiiFinding,
    PiiLabel,
    PiiType,
    SensitivityPolicy,
    scan_document,
)
from .redaction_agent import (
    DlpLeakTool,
    JudgeTool,
    MetricsTool,
    NoLeakTool,
    RedactionAgent,
)
from .redaction_judge import (
    GeminiRedactionAnalyzer,
    GeminiRedactionJudge,
    RedactionAnalyzer,
    RedactionJudge,
    RedactionReport,
)
from .synthesize import DocumentAnonymizer, GeminiImageAnonymizer

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL.Image import Image

    from .config import Settings


def _findings_by_page(
    findings: tuple[PiiFinding, ...], page_count: int
) -> list[list[PiiFinding]]:
    """Bucket findings by their page index (each finding carries ``.page``)."""
    buckets: list[list[PiiFinding]] = [[] for _ in range(page_count)]
    for f in findings:
        if 0 <= f.page < page_count:
            buckets[f.page].append(f)
    return buckets


def _count_by(findings: list[PiiFinding], key: Callable[[PiiFinding], str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        k = key(f)
        counts[k] = counts.get(k, 0) + 1
    return counts


def _page_hint(findings: list[PiiFinding]) -> str | None:
    """A per-page editor hint: 'remove 2 name, 1 iban_account' — only this page's PII."""
    by_type = _count_by(findings, lambda f: f.type.value)
    return ", ".join(f"{n} {t}" for t, n in by_type.items()) or None


def _by_type_detector(findings: list[PiiFinding]) -> dict[str, dict[str, int]]:
    """Per type, which detector(s) found it: ``{"name": {"gemini": 1, "dlp": 1}}``.

    This is the input-side provenance the human sees — Gemini-vision vs Cloud DLP.
    """
    out: dict[str, dict[str, int]] = {}
    for f in findings:
        per = out.setdefault(f.type.value, {})
        per[f.detector] = per.get(f.detector, 0) + 1
    return out


@dataclass
class PiiReviewResult:
    """Everything the review screen shows for one document."""

    pii: PiiLabel
    source_pages: list[Image]
    anonymized_pages: list[Image]
    reports: list[RedactionReport]  # per-page metrics + LLM explanation

    @property
    def worst_verdict(self) -> str:
        """The most severe per-page verdict (fail > review > pass)."""
        order = {"fail": 2, "review": 1, "pass": 0}
        verdicts = [r.verdict for r in self.reports] or ["pass"]
        return max(verdicts, key=lambda v: order.get(v, 0))


@dataclass
class ReviewEvent:
    """A progress event the UI renders step-by-step as the review runs.

    ``kind`` is one of ``"scan_done"``, ``"page_done"``, ``"complete"``. The
    payload carries the freshly-finished artefact (the PII label, one page's
    images + report, or the assembled result) so the caller can render it live.
    """

    kind: str
    payload: dict[str, Any]


class PiiReviewService(Protocol):
    def review(self, document_id: str, pages: list[Image]) -> PiiReviewResult: ...

    def review_events(
        self, document_id: str, pages: list[Image]
    ) -> Iterator[ReviewEvent]: ...


class GeminiPiiReviewService:
    """Compose the Gemini-backed scan + anonymise + judge (parts injectable for tests)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: object | None = None,
        detector: PiiDetector | None = None,
        anonymizer: DocumentAnonymizer | None = None,
        judge: RedactionJudge | None = None,
        analyzer: RedactionAnalyzer | None = None,
        policy: SensitivityPolicy | None = None,
        pii_types: Iterable[PiiType] | None = None,
        dlp_rescan: PiiDetector | None = None,
    ) -> None:
        # ONE shared genai client across all four Gemini components. Per-call
        # throwaway clients let the image-gen client's GC close a shared httpx
        # transport, breaking the next call ("client has been closed"). A single
        # long-lived client avoids that churn entirely.
        shared = client
        needs_client = any(c is None for c in (detector, anonymizer, judge, analyzer))
        if shared is None and needs_client:
            from .gemini_client import make_client

            shared = make_client(settings)
        # Empty scope == None ("scan for everything"); a blank-description plan
        # yields [] and must NOT collapse DLP to an empty info_types list.
        self._pii_types = tuple(pii_types) if pii_types else None
        self._detector = detector or self._default_detector(settings, shared, self._pii_types)
        self._anonymizer = anonymizer or GeminiImageAnonymizer(settings, client=shared)
        self._judge = judge or GeminiRedactionJudge(settings, client=shared)
        self._analyzer = analyzer or GeminiRedactionAnalyzer(settings, client=shared)
        self._policy = policy or SensitivityPolicy()
        # The judge's certified tool: a DLP detector used for the VALUE-CARRYOVER leak
        # check. It reads the real values on the source page and on each generated page;
        # any source value that survives is a hard, certified leak. ON by default — and
        # safe to leave on because it compares VALUES, so the synthetic fakes (different
        # values) never false-fire (the flaw that made a type-presence re-scan unusable).
        # Needs DLP (roles/dlp.user); values are used in-memory only. Disable with
        # PII_DLP_LEAK_CHECK=0; inject a fake via dlp_rescan= in tests.
        self._dlp_carryover: Any = dlp_rescan
        if self._dlp_carryover is None and settings.pii_dlp_leak_check:
            self._dlp_carryover = DlpPiiDetector(settings, pii_types=self._pii_types)
        # The redaction agent owns the anonymise→evaluate→decide loop. It calls each
        # signal + the decision as an explicit tool: MetricsTool, JudgeTool (the LLM
        # subagent), DlpLeakTool (certified value-carryover), a deterministic policy,
        # and a feedback tool. Stateless across runs, so it's shared by the page workers.
        leak_tool = DlpLeakTool(self._dlp_carryover) if self._dlp_carryover is not None else NoLeakTool()
        self._agent = RedactionAgent(
            synthesize=self._anonymizer,
            metrics=MetricsTool(self._analyzer),
            judge=JudgeTool(self._judge),
            leak_tool=leak_tool,
        )
        # Pages are independent, so anonymise+judge them concurrently. The slow leg
        # is Pro image generation (tens of seconds); processing pages in parallel
        # turns an N-page wait into roughly one page's wait. Capped to stay within
        # Vertex quota — the cap doesn't change WHAT each page produces, only when.
        self._max_parallel = max(1, settings.pii_max_parallel)

    def _process_page(
        self, src: Image, page_index: int, hint: str | None
    ) -> tuple[Image, RedactionReport]:
        """The per-page work unit (run on a worker thread): hand the page to the agent,
        which runs the anonymise → (metrics ∪ DLP ∪ judge) → decide → retry loop."""
        return self._agent.run(src, hint=hint, page_index=page_index)

    @staticmethod
    def _default_detector(
        settings: Settings, shared: object | None, pii_types: tuple[PiiType, ...] | None
    ) -> PiiDetector:
        """Gemini alone, or the Gemini+DLP ensemble when ``PII_USE_DLP`` is set."""
        gemini = GeminiPiiDetector(settings, client=shared)
        if not settings.pii_use_dlp:
            return gemini
        # DLP runs in-perimeter (no genai client); the ensemble unions both, keeping
        # each finding's detector tag so the by-detector view shows who spotted what.
        # Scope DLP to the job's expected types when given (dynamic configuration).
        return EnsemblePiiDetector([gemini, DlpPiiDetector(settings, pii_types=pii_types)])

    def review_events(
        self, document_id: str, pages: list[Image]
    ) -> Iterator[ReviewEvent]:
        """Run the review, yielding progress as each step finishes (for the live UI).

        Step 1: scan the whole document (one ``scan_done``). Step 2: anonymise +
        judge every page concurrently, emitting a ``page_done`` the moment each one
        lands (out of order — keyed by page index). Step 3: a final ``complete`` with
        the assembled, page-ordered result.
        """
        label = scan_document(document_id, pages, self._detector, policy=self._policy)
        yield ReviewEvent("scan_done", {"label": label})

        # Group findings by page so each page's prompt is grounded in ONLY its own PII
        # (sharper than one document-level hint shared across every page).
        per_page = _findings_by_page(label.findings, len(pages))
        anonymized: list[Image | None] = [None] * len(pages)
        reports: list[RedactionReport | None] = [None] * len(pages)

        workers = min(self._max_parallel, len(pages)) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._process_page, src, i, _page_hint(per_page[i])): i
                for i, src in enumerate(pages)
            }
            for future in as_completed(futures):
                i = futures[future]
                try:
                    out, report = future.result()
                except Exception as exc:  # noqa: BLE001 — isolate per-page failures
                    # One page failing (e.g. a persistent 504 on image-gen) must NOT sink
                    # the whole multi-page document: record it as an error page and carry
                    # on with the rest. The doc's verdict becomes "error" (worst-of-pages)
                    # so a human reviews it, but the other pages are still anonymised.
                    _log.warning("page %d failed, recording as error page: %s", i, exc)
                    yield ReviewEvent(
                        "page_done",
                        {
                            "page": i, "source": pages[i], "anon": None, "report": None,
                            "error": str(exc),
                            "by_type": _count_by(per_page[i], lambda f: f.type.value),
                            "by_detector": _count_by(per_page[i], lambda f: f.detector),
                            "by_type_detector": _by_type_detector(per_page[i]),
                        },
                    )
                    continue
                anonymized[i] = out
                reports[i] = report
                yield ReviewEvent(
                    "page_done",
                    {
                        "page": i, "source": pages[i], "anon": out, "report": report,
                        # the per-page list of PII the scan flagged on THIS page
                        "by_type": _count_by(per_page[i], lambda f: f.type.value),
                        "by_detector": _count_by(per_page[i], lambda f: f.detector),
                        # input provenance: which detector found each type
                        "by_type_detector": _by_type_detector(per_page[i]),
                    },
                )

        result = PiiReviewResult(
            pii=label,
            source_pages=pages,
            anonymized_pages=[a for a in anonymized if a is not None],
            reports=[r for r in reports if r is not None],
        )
        yield ReviewEvent("complete", {"result": result})

    def review(self, document_id: str, pages: list[Image]) -> PiiReviewResult:
        """Blocking convenience wrapper — drains :meth:`review_events`."""
        result: PiiReviewResult | None = None
        for event in self.review_events(document_id, pages):
            if event.kind == "complete":
                result = event.payload["result"]
        assert result is not None  # review_events always ends with a complete event
        return result
