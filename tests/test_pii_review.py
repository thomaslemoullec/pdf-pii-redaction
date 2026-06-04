"""Unit test for the PII review composition (scan + anonymise + judge), with fakes."""

from __future__ import annotations

from dataclasses import replace

from PIL import Image

from pdf_anonymiser.config import Settings
from pdf_anonymiser.pii import _TYPE_SENSITIVITY, PiiFinding, PiiType
from pdf_anonymiser.pii_review import GeminiPiiReviewService
from pdf_anonymiser.redaction_judge import RedactionJudgement


class _Detector:
    def scan_page(self, image, page_index):  # type: ignore[no-untyped-def]
        t = PiiType.NAME
        return [PiiFinding(t, page_index, _TYPE_SENSITIVITY[t])] if page_index == 0 else []


class _Anonymizer:
    def anonymize_page(self, image, *, hint=None, feedback=None):  # type: ignore[no-untyped-def]
        return Image.new("RGB", image.size, "white")


class _Judge:
    def judge(self, source, anonymized):  # type: ignore[no-untyped-def]
        return RedactionJudgement(
            leaked=False, leaked_types=(), all_pii_removed=True,
            layout_preserved=True, rationale="ok",
        )


class _Analyzer:
    def analyze_source(self, image):  # type: ignore[no-untyped-def]
        return ("Name John Smith", [("name", "John Smith")])

    def transcribe(self, image):  # type: ignore[no-untyped-def]
        return "Name Anna Mueller"  # no leak


def test_review_composes_scan_anonymise_judge() -> None:
    svc = GeminiPiiReviewService(
        Settings.from_env(), detector=_Detector(), anonymizer=_Anonymizer(),
        judge=_Judge(), analyzer=_Analyzer(),
    )
    pages = [Image.new("RGB", (50, 60), "black") for _ in range(2)]
    result = svc.review("doc-1", pages)
    assert result.pii.contains_pii  # name found on page 0
    assert len(result.anonymized_pages) == 2
    assert len(result.reports) == 2
    assert result.worst_verdict == "pass"


def test_service_shares_one_client_across_components() -> None:
    # The fix for "client has been closed": all four Gemini components must share
    # ONE client, not build throwaway ones (whose GC closes a shared transport).
    sentinel = object()
    svc = GeminiPiiReviewService(Settings.from_env(), client=sentinel)
    assert svc._detector._client is sentinel
    assert svc._anonymizer._client is sentinel
    assert svc._judge._client is sentinel
    assert svc._analyzer._client is sentinel


def test_review_events_stream_scan_then_pages_then_complete() -> None:
    svc = GeminiPiiReviewService(
        Settings.from_env(), detector=_Detector(), anonymizer=_Anonymizer(),
        judge=_Judge(), analyzer=_Analyzer(),
    )
    pages = [Image.new("RGB", (40, 50), "black") for _ in range(3)]
    events = list(svc.review_events("doc-stream", pages))

    kinds = [e.kind for e in events]
    assert kinds[0] == "scan_done"  # scan is always first
    assert kinds[-1] == "complete"  # complete is always last
    # one page_done per page (order may vary — they run concurrently)
    page_events = [e for e in events if e.kind == "page_done"]
    assert sorted(e.payload["page"] for e in page_events) == [0, 1, 2]
    # the final result is page-ordered and complete
    result = events[-1].payload["result"]
    assert len(result.anonymized_pages) == 3
    assert len(result.reports) == 3


def test_review_events_run_pages_concurrently() -> None:
    # Prove parallelism: a barrier that only releases once all pages are in flight
    # would deadlock if pages ran one-at-a-time with max_parallel >= page count.
    import threading
    from dataclasses import replace

    started = threading.Barrier(3, timeout=5)

    class _SlowAnonymizer:
        def anonymize_page(self, image, *, hint=None, feedback=None):  # type: ignore[no-untyped-def]
            started.wait()  # all three must be running at once or this times out
            return Image.new("RGB", image.size, "white")

    svc = GeminiPiiReviewService(
        replace(Settings.from_env(), pii_max_parallel=3),
        detector=_Detector(), anonymizer=_SlowAnonymizer(),
        judge=_Judge(), analyzer=_Analyzer(),
    )
    pages = [Image.new("RGB", (40, 50), "black") for _ in range(3)]
    events = list(svc.review_events("doc-par", pages))  # no BrokenBarrierError ⇒ concurrent
    assert len([e for e in events if e.kind == "page_done"]) == 3


def test_default_detector_is_ensemble_when_dlp_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pdf_anonymiser.pii import EnsemblePiiDetector, GeminiPiiDetector

    base = Settings.from_env()
    gemini_only = GeminiPiiReviewService._default_detector(base, shared=object(), pii_types=None)
    assert isinstance(gemini_only, GeminiPiiDetector)

    from dataclasses import replace

    with_dlp = GeminiPiiReviewService._default_detector(
        replace(base, pii_use_dlp=True), shared=object(), pii_types=None
    )
    assert isinstance(with_dlp, EnsemblePiiDetector)


def test_per_page_hint_uses_only_that_pages_findings() -> None:
    # page 0 has a NAME, page 1 has an ID_NUMBER → each page's editor hint should
    # mention ONLY its own type, not the document-level union.
    from pdf_anonymiser.pii import _TYPE_SENSITIVITY, PiiFinding, PiiType

    class _PerPageDetector:
        def scan_page(self, image, page_index):  # type: ignore[no-untyped-def]
            t = PiiType.NAME if page_index == 0 else PiiType.ID_NUMBER
            return [PiiFinding(t, page_index, _TYPE_SENSITIVITY[t])]

    seen_hints: dict[int, str | None] = {}

    class _RecordingAnonymizer:
        def __init__(self) -> None:
            self._n = 0

        def anonymize_page(self, image, *, hint=None, feedback=None):  # type: ignore[no-untyped-def]
            # pages run concurrently; record by call order via the image's tag is hard,
            # so stash every hint we see and assert the SET below.
            seen_hints[len(seen_hints)] = hint
            return Image.new("RGB", image.size, "white")

    svc = GeminiPiiReviewService(
        Settings.from_env(), detector=_PerPageDetector(),
        anonymizer=_RecordingAnonymizer(), judge=_Judge(), analyzer=_Analyzer(),
    )
    pages = [Image.new("RGB", (40, 50), "black") for _ in range(2)]
    list(svc.review_events("doc-pp", pages))

    hints = set(seen_hints.values())
    assert "1 name" in hints
    assert "1 id_number" in hints
    # critically, neither page got BOTH types in one hint
    assert not any("name" in h and "id_number" in h for h in hints if h)


def test_page_done_carries_per_page_redacted_types() -> None:
    from pdf_anonymiser.pii import _TYPE_SENSITIVITY, PiiFinding, PiiType

    class _PerPageDetector:
        def scan_page(self, image, page_index):  # type: ignore[no-untyped-def]
            t = PiiType.NAME if page_index == 0 else PiiType.IBAN_ACCOUNT
            return [PiiFinding(t, page_index, _TYPE_SENSITIVITY[t], detector="dlp")]

    svc = GeminiPiiReviewService(
        Settings.from_env(), detector=_PerPageDetector(),
        anonymizer=_Anonymizer(), judge=_Judge(), analyzer=_Analyzer(),
    )
    pages = [Image.new("RGB", (40, 50), "black") for _ in range(2)]
    by_page = {
        e.payload["page"]: e.payload
        for e in svc.review_events("doc-rt", pages) if e.kind == "page_done"
    }
    assert by_page[0]["by_type"] == {"name": 1}
    assert by_page[1]["by_type"] == {"iban_account": 1}
    assert by_page[0]["by_detector"] == {"dlp": 1}
    # input provenance: which detector found each type, per page
    assert by_page[0]["by_type_detector"] == {"name": {"dlp": 1}}
    assert by_page[1]["by_type_detector"] == {"iban_account": {"dlp": 1}}


def test_by_type_detector_merges_multiple_detectors_for_a_type() -> None:
    from pdf_anonymiser.pii import _TYPE_SENSITIVITY, PiiFinding, PiiType
    from pdf_anonymiser.pii_review import _by_type_detector

    t = PiiType.NAME
    findings = [
        PiiFinding(t, 0, _TYPE_SENSITIVITY[t], detector="gemini"),
        PiiFinding(t, 0, _TYPE_SENSITIVITY[t], detector="dlp"),
        PiiFinding(
            PiiType.IBAN_ACCOUNT, 0, _TYPE_SENSITIVITY[PiiType.IBAN_ACCOUNT], detector="dlp"
        ),
    ]
    assert _by_type_detector(findings) == {
        "name": {"gemini": 1, "dlp": 1}, "iban_account": {"dlp": 1}
    }


# --- DLP value-carryover leak check ---------------------------------------
#
# The injected DLP scanner returns (type, value) pairs from scan_values(). Our fakes
# distinguish the source page (black) from the synthetic page (white the anonymizer
# returns) by the top-left pixel, so a test can model "the real value survived" vs "the
# fake replaced it" without any live DLP.


def _is_source(image) -> bool:  # type: ignore[no-untyped-def]
    """Black page = source (the test feeds black sources); white = synthetic output."""
    return sum(image.getpixel((0, 0))[:3]) < 200


class _CarryoverDLP:
    """A fake DlpPiiDetector: real values on the source, configurable on the output."""

    def __init__(self, source_values, output_values):  # type: ignore[no-untyped-def]
        self._source = source_values
        self._output = output_values
        self.seen_sizes: list = []  # type: ignore[type-arg]

    def scan_values(self, image, page_index):  # type: ignore[no-untyped-def]
        self.seen_sizes.append(image.size)
        return self._source if _is_source(image) else self._output


def test_dlp_carryover_forces_fail_and_feeds_correction() -> None:
    # The SAME real IBAN appears on the source AND survives (reformatted) into the
    # output → certified leak → fail on every attempt + a correction naming it.
    real_iban = "DE89 3704 0044 0532 0130 00"
    survived = "de89370400440532013000"  # reformatted but the same value
    dlp = _CarryoverDLP(
        source_values=[(PiiType.IBAN_ACCOUNT, real_iban)],
        output_values=[(PiiType.IBAN_ACCOUNT, survived)],
    )

    feedbacks: list[str | None] = []

    class _FeedbackAnonymizer:
        def anonymize_page(self, image, *, hint=None, feedback=None):  # type: ignore[no-untyped-def]
            feedbacks.append(feedback)
            return Image.new("RGB", image.size, "white")

    svc = GeminiPiiReviewService(
        Settings.from_env(), detector=_Detector(), anonymizer=_FeedbackAnonymizer(),
        judge=_Judge(), analyzer=_Analyzer(), dlp_rescan=dlp,
    )
    pages = [Image.new("RGB", (40, 50), "black")]
    events = list(svc.review_events("doc-leak", pages))
    report = next(e.payload["report"] for e in events if e.kind == "page_done")

    assert report.dlp_leaks == ("iban_account",)
    assert report.verdict == "fail"  # certified leak overrides metrics
    assert any(f and "Cloud DLP" in f and "iban_account" in f for f in feedbacks)


def test_dlp_carryover_passes_when_only_synthetic_fakes_present() -> None:
    # The crucial property: a SYNTHETIC replacement (different value of the same type)
    # must NOT be flagged — this is exactly why a type-presence re-scan was unusable.
    dlp = _CarryoverDLP(
        source_values=[(PiiType.IBAN_ACCOUNT, "DE89 3704 0044 0532 0130 00")],
        output_values=[(PiiType.IBAN_ACCOUNT, "GB29 NWBK 6016 1331 9268 19")],  # a fake
    )
    svc = GeminiPiiReviewService(
        Settings.from_env(), detector=_Detector(), anonymizer=_Anonymizer(),
        judge=_Judge(), analyzer=_Analyzer(), dlp_rescan=dlp,
    )
    pages = [Image.new("RGB", (40, 50), "black")]
    events = list(svc.review_events("doc-clean", pages))
    report = next(e.payload["report"] for e in events if e.kind == "page_done")

    assert report.dlp_leaks == ()  # no false leak on the synthetic IBAN
    assert report.verdict == "pass"


def test_dlp_carryover_reads_source_once_and_each_generated_page() -> None:
    # Source values are read ONCE (the source doesn't change); each generated attempt
    # is re-scanned. One source page + one output page (single passing attempt) = 2.
    dlp = _CarryoverDLP(source_values=[], output_values=[])  # nothing found → no leak
    svc = GeminiPiiReviewService(
        Settings.from_env(), detector=_Detector(), anonymizer=_Anonymizer(),
        judge=_Judge(), analyzer=_Analyzer(), dlp_rescan=dlp,
    )
    pages = [Image.new("RGB", (33, 44), "black")]
    list(svc.review_events("doc-tool", pages))
    # one black source scan + one white output scan
    assert dlp.seen_sizes == [(33, 44), (33, 44)]


# --- configuration: the leak check is on by default, needs DLP, off when disabled ---


def test_dlp_leak_check_on_by_default() -> None:
    from pdf_anonymiser.pii import DlpPiiDetector

    # conftest forces PII_DLP_LEAK_CHECK=0 for the suite; turn it back on for this case.
    svc = GeminiPiiReviewService(
        replace(Settings.from_env(), pii_dlp_leak_check=True),
        detector=_Detector(), anonymizer=_Anonymizer(), judge=_Judge(), analyzer=_Analyzer(),
    )
    assert isinstance(svc._dlp_carryover, DlpPiiDetector)


def test_dlp_leak_check_absent_when_disabled() -> None:
    svc = GeminiPiiReviewService(
        replace(Settings.from_env(), pii_dlp_leak_check=False),
        detector=_Detector(), anonymizer=_Anonymizer(), judge=_Judge(), analyzer=_Analyzer(),
    )
    assert svc._dlp_carryover is None


def test_dlp_carryover_degrades_when_source_scan_errors() -> None:
    # A DLP hiccup on the source must not sink the page — the check is skipped and the
    # metrics/judge still decide.
    class _BrokenDLP:
        def scan_values(self, image, page_index):  # type: ignore[no-untyped-def]
            raise RuntimeError("DLP unavailable")

    svc = GeminiPiiReviewService(
        Settings.from_env(), detector=_Detector(), anonymizer=_Anonymizer(),
        judge=_Judge(), analyzer=_Analyzer(), dlp_rescan=_BrokenDLP(),
    )
    pages = [Image.new("RGB", (40, 50), "black")]
    events = list(svc.review_events("doc-degrade", pages))
    report = next(e.payload["report"] for e in events if e.kind == "page_done")
    assert report.dlp_leaks == ()  # skipped, not a phantom leak
    assert report.verdict == "pass"
