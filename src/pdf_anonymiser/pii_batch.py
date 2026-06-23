"""Batch PII anonymisation over a GCS bucket → a validated/unvalidated dataset.

Point this at a bucket prefix of PDFs; it renders each document, runs the PII review
(scan → synthesise PII-free pages → judge, with the DLP ensemble), writes the
PII-free output to a dataset bucket, and records a per-document + per-page result.

The output lands under ``<output>/unvalidated/<doc>/`` first. A human then
reviews each document in the UI and **promotes** it to ``…/validated/<doc>/`` (or
leaves it unvalidated with a note saying why) — so the corpus self-segregates into a
trustworthy ``validated`` half and an ``unvalidated`` half.

Designed for parallel execution: a Cloud Run Job fans out N tasks, each processing a
shard of the document list (:func:`shard`). Storage is behind the :class:`ObjectStore`
protocol so the processor + promotion are unit-tested with an in-memory fake — no GCS.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .obs import EVENT_DOCUMENT, log_event
from .pii_result_store import (
    PiiDoc,
    PiiResultStore,
    ReviewPolicy,
    classification_metadata,
)

if TYPE_CHECKING:
    from PIL.Image import Image

    from .pii_review import PiiReviewService

_log = logging.getLogger(__name__)

# (pdf_bytes, dpi) -> one PIL image per page.
PageRenderer = Callable[[bytes, int], list["Image"]]


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """``gs://bucket/path`` → ``(bucket, path)``; ``path`` may be empty (a prefix)."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    bucket, _, key = uri.removeprefix("gs://").partition("/")
    if not bucket:
        raise ValueError(f"gs:// URI must include a bucket: {uri!r}")
    return bucket, key


class ObjectStore(Protocol):
    """The minimal blob storage the batch needs — listing, read, write, copy, delete."""

    def list(self, prefix_uri: str) -> list[str]: ...
    def list_with_generations(self, prefix_uri: str) -> dict[str, int]:
        """``{uri: generation}`` for each object under ``prefix_uri`` — the generation
        changes whenever an object is (over)written, so callers can cache by it."""
        ...
    def read(self, uri: str) -> bytes: ...
    def write(
        self, uri: str, data: bytes, *, content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Write an object; ``metadata`` becomes its custom (user) metadata, if any."""
        ...
    def copy(self, src_uri: str, dst_uri: str) -> None: ...
    def delete(self, uri: str) -> None: ...
    def create_if_absent(self, uri: str, data: bytes) -> bool: ...


class InMemoryObjectStore:
    """A dict-backed :class:`ObjectStore` for tests (keys are full ``gs://`` URIs)."""

    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self._blobs: dict[str, bytes] = dict(blobs or {})
        self._gen: dict[str, int] = dict.fromkeys(self._blobs, 1)  # bump on each write
        self._meta: dict[str, dict[str, str]] = {}  # per-object custom metadata

    def list(self, prefix_uri: str) -> list[str]:
        return sorted(uri for uri in self._blobs if uri.startswith(prefix_uri))

    def list_with_generations(self, prefix_uri: str) -> dict[str, int]:
        return {u: self._gen.get(u, 1) for u in sorted(self._blobs) if u.startswith(prefix_uri)}

    def read(self, uri: str) -> bytes:
        return self._blobs[uri]

    def write(
        self, uri: str, data: bytes, *, content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._blobs[uri] = data
        self._gen[uri] = self._gen.get(uri, 0) + 1
        if metadata is not None:
            self._meta[uri] = dict(metadata)

    def copy(self, src_uri: str, dst_uri: str) -> None:
        self._blobs[dst_uri] = self._blobs[src_uri]
        self._gen[dst_uri] = self._gen.get(dst_uri, 0) + 1
        if src_uri in self._meta:  # custom metadata travels with the object on copy (as GCS does)
            self._meta[dst_uri] = dict(self._meta[src_uri])

    def delete(self, uri: str) -> None:
        self._blobs.pop(uri, None)
        self._gen.pop(uri, None)
        self._meta.pop(uri, None)

    def create_if_absent(self, uri: str, data: bytes) -> bool:
        if uri in self._blobs:
            return False
        self._blobs[uri] = data
        self._gen[uri] = self._gen.get(uri, 0) + 1
        return True

    @property
    def blobs(self) -> dict[str, bytes]:
        return self._blobs

    @property
    def meta(self) -> dict[str, dict[str, str]]:
        """Per-object custom metadata recorded by :meth:`write` (for assertions)."""
        return self._meta


class GcsObjectStore:
    """A Cloud Storage–backed :class:`ObjectStore` (lazy client; injectable for tests)."""

    def __init__(self, *, client: Any = None) -> None:
        self._client = client

    def _gcs(self) -> Any:
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client()
        return self._client

    def list(self, prefix_uri: str) -> list[str]:
        bucket, prefix = parse_gs_uri(prefix_uri)
        blobs = self._gcs().list_blobs(bucket, prefix=prefix)
        return [f"gs://{bucket}/{b.name}" for b in blobs]

    def list_with_generations(self, prefix_uri: str) -> dict[str, int]:
        bucket, prefix = parse_gs_uri(prefix_uri)
        # list_blobs returns each blob's generation for free — no extra round-trips.
        return {
            f"gs://{bucket}/{b.name}": int(b.generation)
            for b in self._gcs().list_blobs(bucket, prefix=prefix)
        }

    def read(self, uri: str) -> bytes:
        bucket, key = parse_gs_uri(uri)
        data: bytes = self._gcs().bucket(bucket).blob(key).download_as_bytes()
        return data

    def write(
        self, uri: str, data: bytes, *, content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        bucket, key = parse_gs_uri(uri)
        blob = self._gcs().bucket(bucket).blob(key)
        if metadata:  # custom (user) metadata — set on the blob before upload
            blob.metadata = metadata
        blob.upload_from_string(data, content_type=content_type)

    def copy(self, src_uri: str, dst_uri: str) -> None:
        sb, sk = parse_gs_uri(src_uri)
        db, dk = parse_gs_uri(dst_uri)
        src_bucket = self._gcs().bucket(sb)
        src_bucket.copy_blob(src_bucket.blob(sk), self._gcs().bucket(db), dk)

    def delete(self, uri: str) -> None:
        bucket, key = parse_gs_uri(uri)
        self._gcs().bucket(bucket).blob(key).delete()

    def create_if_absent(self, uri: str, data: bytes) -> bool:
        # if_generation_match=0 = "create only if the object does not exist" — GCS
        # serialises concurrent attempts so EXACTLY ONE wins (our completion latch).
        bucket, key = parse_gs_uri(uri)
        try:
            self._gcs().bucket(bucket).blob(key).upload_from_string(
                data, if_generation_match=0
            )
            return True
        except Exception as exc:
            if "412" in str(exc) or "PreconditionFailed" in type(exc).__name__:
                return False
            raise


@dataclass
class PageResult:
    """Per-page outcome — PII-minimal (types + counts, never values).

    Carries the same measured detail the interactive review shows: the composite
    score plus its two factors (removal recall, fidelity) and the LLM rationale, so
    the batch validation screen is as informative as the single-doc one.
    """

    page_no: int  # 1-based
    by_type: dict[str, int]
    by_detector: dict[str, int]
    verdict: str
    score: float
    leaked_types: list[str]
    dlp_leaks: list[str]
    anon_uri: str  # the synthetic page PNG in the (unvalidated) output
    removal_recall: float = 1.0  # fraction of source PII removed
    fidelity: float = 1.0  # fraction of non-PII content preserved
    pii_leaked: int = 0
    pii_total: int = 0
    nonpii_changed: int = 0
    rationale: str = ""  # the judge's human explanation
    # Input provenance: which detector found each type ({"name": {"gemini": 1}}).
    by_type_detector: dict[str, dict[str, int]] = field(default_factory=dict)
    # The Gemini judge's structured assessment (alongside the deterministic metrics).
    judge_all_removed: bool = True
    judge_layout_ok: bool = True
    attempts: int = 1  # anonymise→judge rounds for this page (cost driver)


@dataclass
class DocumentResult:
    """Per-document outcome of the batch — what the validator reviews + what we persist."""

    document_id: str
    source_uri: str
    output_prefix: str  # gs://…/<output>/unvalidated/<doc>
    routing: str
    max_sensitivity: str
    verdict: str  # worst per-page verdict
    score: float  # mean per-page score
    pages: list[PageResult] = field(default_factory=list)

    @property
    def pdf_uri(self) -> str:
        return f"{self.output_prefix}/{self.document_id}.pdf"


def shard(items: list[str], *, index: int, count: int) -> list[str]:
    """The slice of ``items`` this task owns (round-robin by position).

    Round-robin (not contiguous blocks) so a run of large/slow documents is spread
    across tasks rather than piling onto one — better wall-clock balance.
    """
    if count <= 1:
        return list(items)
    return [item for i, item in enumerate(items) if i % count == index]


def _doc_id_from_uri(uri: str) -> str:
    """``gs://b/k/acct-0001.pdf`` → ``acct-0001`` (the object basename, sans .pdf)."""
    name = uri.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.lower().endswith(".pdf") else name


def _pages_to_pdf(pages: list[Image]) -> bytes:
    """Combine page images into one multi-page PDF (the PII-free deliverable)."""
    rgb = [p.convert("RGB") for p in pages]
    buffer = io.BytesIO()
    rgb[0].save(buffer, format="PDF", save_all=True, append_images=rgb[1:])
    return buffer.getvalue()


def _png(image: Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


class PiiBatchProcessor:
    """Render → review → write PII-free output for one document, via an ObjectStore."""

    def __init__(
        self,
        *,
        review_service: PiiReviewService,
        renderer: PageRenderer,
        store: ObjectStore,
        dpi: int = 150,
    ) -> None:
        self._review = review_service
        self._renderer = renderer
        self._store = store
        self._dpi = dpi

    def process_document(self, source_uri: str, *, dataset_base_uri: str) -> DocumentResult:
        """Anonymise one PDF and write its PII-free pages + PDF under ``unvalidated/``.

        ``dataset_base_uri`` is the job's output root, e.g. ``gs://bucket/anonymised``; output
        goes to ``<base>/unvalidated/<doc>/page-N.png`` and ``<doc>.pdf``.
        """
        doc_id = _doc_id_from_uri(source_uri)
        pdf_bytes = self._store.read(source_uri)
        pages = self._renderer(pdf_bytes, self._dpi)
        out_prefix = f"{dataset_base_uri.rstrip('/')}/unvalidated/{doc_id}"

        label = None
        # Custom object metadata recording the computed sensitivity tier — attached to
        # every output write so the classification travels with the object (informative
        # only; see docs/DATA_CLASSIFICATION.md). Resolved once the scan yields a label.
        class_meta = classification_metadata(routing="", max_sensitivity="")
        anon: list[Image | None] = [None] * len(pages)
        page_results: list[PageResult] = []
        for ev in self._review.review_events(doc_id, pages):
            if ev.kind == "scan_done":
                label = ev.payload["label"]
                class_meta = classification_metadata(
                    routing=label.routing.value, max_sensitivity=label.max_sensitivity.name
                )
            elif ev.kind == "page_done" and ev.payload.get("report") is None:
                # A page that failed to process (e.g. a persistent 504) — recorded as an
                # error page so the rest of the document still completes (no synthetic
                # image to write). The doc's verdict becomes "error" → routed to review.
                i = ev.payload["page"]
                page_results.append(
                    PageResult(
                        page_no=i + 1,
                        by_type=ev.payload.get("by_type", {}),
                        by_detector=ev.payload.get("by_detector", {}),
                        verdict="error", score=0.0, leaked_types=[], dlp_leaks=[],
                        anon_uri="", removal_recall=0.0, fidelity=0.0,
                        pii_leaked=0, pii_total=0, nonpii_changed=0,
                        rationale=(ev.payload.get("error", "") or "")[:300],
                        by_type_detector=ev.payload.get("by_type_detector", {}),
                        judge_all_removed=False, judge_layout_ok=False, attempts=0,
                    )
                )
            elif ev.kind == "page_done":
                i = ev.payload["page"]
                out_img = ev.payload["anon"]
                anon[i] = out_img
                report = ev.payload["report"]
                anon_uri = f"{out_prefix}/page-{i + 1}.png"
                self._store.write(
                    anon_uri, _png(out_img), content_type="image/png", metadata=class_meta
                )
                m = report.metrics
                page_results.append(
                    PageResult(
                        page_no=i + 1,
                        by_type=ev.payload.get("by_type", {}),
                        by_detector=ev.payload.get("by_detector", {}),
                        verdict=report.verdict,
                        score=m.score,
                        leaked_types=list(m.leaked_types),
                        dlp_leaks=list(getattr(report, "dlp_leaks", ())),
                        anon_uri=anon_uri,
                        removal_recall=m.removal_recall,
                        fidelity=m.fidelity,
                        pii_leaked=m.pii_leaked,
                        pii_total=m.pii_total,
                        nonpii_changed=m.nonpii_changed,
                        rationale=report.rationale,
                        by_type_detector=ev.payload.get("by_type_detector", {}),
                        judge_all_removed=getattr(report, "judge_all_removed", True),
                        judge_layout_ok=getattr(report, "judge_layout_ok", True),
                        attempts=getattr(report, "attempts", 1),
                    )
                )

        page_results.sort(key=lambda p: p.page_no)
        ordered = [a for a in anon if a is not None]
        if ordered:
            self._store.write(
                f"{out_prefix}/{doc_id}.pdf", _pages_to_pdf(ordered),
                content_type="application/pdf", metadata=class_meta,
            )
        order = {"error": 3, "fail": 2, "review": 1, "pass": 0}
        if not page_results:
            # An empty render (0-page / unreadable PDF) is NOT a clean anonymisation —
            # flag it for a human instead of silently recording a 'pass'.
            worst, mean = "review", 0.0
        else:
            worst = max(page_results, key=lambda p: order.get(p.verdict, 0)).verdict
            mean = sum(p.score for p in page_results) / len(page_results)
        return DocumentResult(
            document_id=doc_id,
            source_uri=source_uri,
            output_prefix=out_prefix,
            routing=label.routing.value if label else "ok_for_global",
            max_sensitivity=label.max_sensitivity.name if label else "NONE",
            verdict=worst,
            score=round(mean, 3),
            pages=page_results,
        )


def list_source_pdfs(
    store: ObjectStore, source_uri: str, *, limit: int | None = None
) -> list[str]:
    """The ``.pdf`` objects under a prefix, sorted — optionally capped to ``limit``.

    The list is sorted (deterministic), so every task applying the same ``limit``
    sees the same capped work-list before round-robin sharding — the subset is
    consistent across the fan-out. ``limit`` ≤ 0 or ``None`` means no cap.
    """
    pdfs = sorted(u for u in store.list(source_uri) if u.lower().endswith(".pdf"))
    return pdfs[:limit] if limit and limit > 0 else pdfs


@dataclass
class BatchRunSummary:
    """Outcome of one task's shard — what processed, what failed (per-doc isolated)."""

    processed: list[DocumentResult]
    failed: list[tuple[str, str]]  # (source_uri, error message)


def run_batch(
    store: PiiResultStore,
    object_store: ObjectStore,
    processor: PiiBatchProcessor,
    *,
    job_id: str,
    source: str,
    output: str,
    index: int = 0,
    count: int = 1,
    limit: int | None = None,
    review_policy: ReviewPolicy | None = None,
    on_complete: Callable[[], None] | None = None,
    rerun: bool = False,
) -> BatchRunSummary:
    """Process this task's shard of a batch job (the library core behind the CLI).

    Lists the source (optionally capped to ``limit``), has task 0 record the
    authoritative ``total`` (and finalise an empty job), processes its round-robin
    shard, and writes one immutable result object per document. A single document's
    failure is isolated (logged, recorded as an ``error`` doc) so one bad PDF never
    sinks the shard. The review policy decides which docs a human must look at.

    Completion is latched once via the store's generation-match marker (whichever task
    or poll observes the last document wins) → ``on_complete`` fires exactly once.

    ``rerun`` = re-process a subset (e.g. one error document) of an already-finished job:
    it overwrites those result objects but does NOT reset the job ``total`` or re-finalise,
    so the existing job/progress is left intact and the UI just picks up the fresh result.
    """
    policy = review_policy or ReviewPolicy()
    all_pdfs = list_source_pdfs(object_store, source, limit=limit)
    if index == 0 and not rerun:
        store.set_job_status(job_id, total=len(all_pdfs))
        if not all_pdfs:  # empty source: nothing to process → finalise now
            store.set_job_status(job_id, status="done")
            _fire(on_complete)
            return BatchRunSummary(processed=[], failed=[])
    mine = shard(all_pdfs, index=index, count=count)

    processed: list[DocumentResult] = []
    failed: list[tuple[str, str]] = []
    for uri in mine:
        started = time.perf_counter()
        try:
            result = processor.process_document(uri, dataset_base_uri=output)
            store.save_document(_to_doc(job_id, result, policy, started), task_index=index)
            processed.append(result)
            log_event(
                EVENT_DOCUMENT, job_id=job_id, document_id=result.document_id,
                verdict=result.verdict, score=result.score, page_count=len(result.pages),
                attempts=sum(p.attempts for p in result.pages),
                pii_leaked=sum(p.pii_leaked for p in result.pages),
                dlp_leaked=int(any(p.dlp_leaks for p in result.pages)),
                processing_seconds=round(time.perf_counter() - started, 2),
            )
        except Exception as exc:
            _log.warning("pii batch: document %s failed: %s", uri, exc)
            failed.append((uri, str(exc)))
            log_event(
                EVENT_DOCUMENT, job_id=job_id, document_id=_doc_id_from_uri(uri),
                verdict="error", error=str(exc),
                processing_seconds=round(time.perf_counter() - started, 2),
            )
            # Record a FAILED doc (verdict 'error', always queued for review) so the
            # job still completes and the failure is visible, not silently dropped.
            try:
                store.save_document(PiiDoc(
                    job_id=job_id, document_id=_doc_id_from_uri(uri), source_uri=uri,
                    output_prefix="", routing="", max_sensitivity="", verdict="error",
                    score=0.0, pages=[{"page_no": 0, "error": str(exc)}],
                    processing_seconds=round(time.perf_counter() - started, 2),
                ), task_index=index)
            except Exception as save_exc:
                _log.warning("pii batch: could not record failure for %s: %s", uri, save_exc)
    # This task finished its shard — try to latch completion (whoever observes the
    # final document wins exactly once). A rerun never re-finalises (the job is done).
    if not rerun and store.finalize_if_complete(job_id):
        _fire(on_complete)
    return BatchRunSummary(processed=processed, failed=failed)


def _to_doc(
    job_id: str, result: DocumentResult, policy: ReviewPolicy, started: float
) -> PiiDoc:
    from dataclasses import asdict
    return PiiDoc(
        job_id=job_id, document_id=result.document_id, source_uri=result.source_uri,
        output_prefix=result.output_prefix, routing=result.routing,
        max_sensitivity=result.max_sensitivity, verdict=result.verdict, score=result.score,
        pages=[asdict(p) for p in result.pages],
        processing_seconds=round(time.perf_counter() - started, 2),
        needs_review=policy.needs_review(verdict=result.verdict, score=result.score),
    )


def _fire(hook: Callable[[], None] | None) -> None:
    if hook is None:
        return
    try:
        hook()
    except Exception as exc:
        _log.warning("pii batch: on_complete hook failed: %s", exc)


def promote_document(
    store: ObjectStore, result_pages: Iterable[str], *, output_prefix: str, validate: bool
) -> str:
    """Move a document's output between ``unvalidated/`` and ``validated/``.

    Copies every page (and the PDF) from the current prefix to the sibling
    ``validated/`` (or back to ``unvalidated/``) and deletes the originals — so the
    document lives in exactly one half of the corpus. Returns the new prefix.
    """
    src_seg, dst_seg = ("unvalidated", "validated") if validate else ("validated", "unvalidated")
    if f"/{src_seg}/" not in output_prefix:
        return output_prefix  # already where it should be
    new_prefix = output_prefix.replace(f"/{src_seg}/", f"/{dst_seg}/")
    for uri in result_pages:
        store.copy(uri, uri.replace(f"/{src_seg}/", f"/{dst_seg}/"))
        store.delete(uri)
    return new_prefix
