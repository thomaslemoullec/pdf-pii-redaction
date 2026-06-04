"""Run/result storage for Batch PII — GCS-only, no database.

Behind the :class:`PiiResultStore` protocol so the rest of the app never knows where
results live. Two implementations: :class:`InMemoryPiiResultStore` (tests) and
:class:`GcsPiiResultStore` (one immutable object per document — parallel-safe — plus a
generation-match completion marker, so N concurrent Cloud Run tasks need no locks and
no database). A Firestore implementation can drop in later with zero changes elsewhere.

Layout under a control prefix (``gs://bucket/_pii_runs``):

    <control>/<job_id>/job.json              # header + total + status + review policy
    <control>/<job_id>/results/<doc>.json    # per-doc result — immutable, one per task
    <control>/<job_id>/index.json            # compacted rows, written once at completion
    <control>/<job_id>/report.json           # cost/timing/totals at completion
    <control>/<job_id>/_complete.marker      # ifGenerationMatch=0 → exactly-once finish

The actual PII-free output (pages + PDF) lives in the operator's output bucket, not
here — this store only holds the PII-minimal run metadata.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from .pii_batch import ObjectStore


# --- Review routing policy --------------------------------------------------


@dataclass(frozen=True)
class ReviewPolicy:
    """Which documents a human must review.

    ``mode="all"`` (default) queues every document. ``mode="flagged"`` queues only
    documents that aren't a clean ``pass`` — i.e. a leak (``fail``), low fidelity
    (``review``), a processing ``error``, OR a score below ``threshold`` — so a
    hundreds-of-docs run only surfaces the ones that actually need eyes.
    """

    mode: str = "all"  # all | flagged
    threshold: float = 0.0  # for 'flagged': also queue any pass below this score

    def needs_review(self, *, verdict: str, score: float) -> bool:
        if self.mode != "flagged":
            return True
        return verdict != "pass" or score < self.threshold


# --- Records ----------------------------------------------------------------


@dataclass
class PiiJob:
    """A batch run header + progress."""

    job_id: str
    dataset: str
    source_uri: str
    output_uri: str
    pii_types: list[str]
    status: str  # running | done | error
    total: int
    completed: int
    created_at: str
    review_mode: str = "all"
    review_threshold: float = 0.0


@dataclass
class PiiDoc:
    """One document's PII-minimal result + the human verdict (no values, ever)."""

    job_id: str
    document_id: str
    source_uri: str
    output_prefix: str
    routing: str
    max_sensitivity: str
    verdict: str  # pass | review | fail | error
    score: float
    pages: list[dict[str, Any]]
    validation_status: str = "pending"  # pending | validated | rejected
    note: str = ""
    processing_seconds: float = 0.0
    needs_review: bool = True
    created_at: str = ""
    # Set to a timestamp while this document is being re-processed (relaunch). The UI
    # shows it as "processing"; a completed re-run writes a fresh result which clears it.
    rerunning: str = ""

    @property
    def doc_pk(self) -> str:  # the document id is unique within a job
        return self.document_id


@dataclass
class Progress:
    """Cheap job progress for the polling UI."""

    total: int
    completed: int
    status: str


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


_VERDICT_ORDER = {"fail": 3, "error": 3, "review": 2, "pass": 1}


def _sorted_docs(docs: list[PiiDoc], sort: str, direction: str) -> list[PiiDoc]:
    """Sort docs by a column (server-side, over the whole set)."""
    keys = {
        "document": lambda d: d.document_id,
        "score": lambda d: d.score,
        "verdict": lambda d: _VERDICT_ORDER.get(d.verdict, 0),
        "time": lambda d: d.processing_seconds,
        "pages": lambda d: len(d.pages),
        "validation": lambda d: d.validation_status,
        "review": lambda d: (0 if d.needs_review else 1),
    }
    key = keys.get(sort, keys["document"])
    return sorted(docs, key=key, reverse=(direction == "desc"))


def _filter_docs(
    docs: list[PiiDoc], *, only_review: bool = False, verdict: str = "", validation: str = "",
) -> list[PiiDoc]:
    """Apply the report's filters (needs-review / AI verdict / human validation)."""
    if only_review:
        docs = [d for d in docs if d.needs_review]
    if verdict:
        docs = [d for d in docs if d.verdict == verdict]
    if validation:
        docs = [d for d in docs if d.validation_status == validation]
    return docs


class PiiResultStore(Protocol):
    """Where batch PII run + document results live (GCS today; swappable)."""

    def create_job(
        self, *, dataset: str, source_uri: str, output_uri: str, pii_types: list[str],
        total: int, review_mode: str = "all", review_threshold: float = 0.0,
    ) -> str: ...
    def get_job(self, job_id: str) -> PiiJob | None: ...
    def list_jobs(self) -> list[PiiJob]: ...
    def set_job_status(
        self, job_id: str, *, status: str | None = None, total: int | None = None
    ) -> None: ...
    def save_document(self, doc: PiiDoc, *, task_index: int = 0) -> None: ...
    def get_progress(self, job_id: str) -> Progress | None: ...
    def list_documents(
        self, job_id: str, *, sort: str = "document", direction: str = "asc",
        page: int = 1, size: int = 50, only_review: bool = False,
        verdict: str = "", validation: str = "",
    ) -> tuple[list[PiiDoc], int]: ...
    def get_document(self, job_id: str, document_id: str) -> PiiDoc | None: ...
    def set_validation(
        self, job_id: str, document_id: str, *, status: str, note: str,
        output_prefix: str | None = None, pages: list[dict[str, Any]] | None = None,
    ) -> None: ...
    def mark_rerunning(self, job_id: str, document_id: str) -> None: ...
    def finalize_if_complete(self, job_id: str) -> bool: ...


def _page(docs: list[PiiDoc], page: int, size: int) -> list[PiiDoc]:
    start = max(0, (page - 1) * size)
    return docs[start:start + size]


# --- In-memory (tests) ------------------------------------------------------


class InMemoryPiiResultStore:
    """Dict-backed store for unit tests — mirrors the GCS semantics."""

    def __init__(self) -> None:
        self._jobs: dict[str, PiiJob] = {}
        self._docs: dict[str, dict[str, PiiDoc]] = {}
        self._markers: set[str] = set()

    def create_job(
        self, *, dataset: str, source_uri: str, output_uri: str, pii_types: list[str],
        total: int, review_mode: str = "all", review_threshold: float = 0.0,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        self._jobs[job_id] = PiiJob(
            job_id=job_id, dataset=dataset, source_uri=source_uri, output_uri=output_uri,
            pii_types=pii_types, status="running", total=total, completed=0,
            created_at=_now(), review_mode=review_mode, review_threshold=review_threshold,
        )
        self._docs[job_id] = {}
        return job_id

    def get_job(self, job_id: str) -> PiiJob | None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.completed = len(self._docs.get(job_id, {}))
        return job

    def list_jobs(self) -> list[PiiJob]:
        return sorted(
            (self.get_job(j) for j in self._jobs),  # type: ignore[misc]
            key=lambda j: j.created_at, reverse=True,
        )

    def set_job_status(
        self, job_id: str, *, status: str | None = None, total: int | None = None
    ) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        if status is not None:
            job.status = status
        if total is not None:
            job.total = total

    def save_document(self, doc: PiiDoc, *, task_index: int = 0) -> None:
        doc.created_at = doc.created_at or _now()
        self._docs.setdefault(doc.job_id, {})[doc.document_id] = doc

    def get_progress(self, job_id: str) -> Progress | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        return Progress(total=job.total, completed=job.completed, status=job.status)

    def list_documents(
        self, job_id: str, *, sort: str = "document", direction: str = "asc",
        page: int = 1, size: int = 50, only_review: bool = False,
        verdict: str = "", validation: str = "",
    ) -> tuple[list[PiiDoc], int]:
        docs = _filter_docs(
            list(self._docs.get(job_id, {}).values()),
            only_review=only_review, verdict=verdict, validation=validation,
        )
        total = len(docs)
        return _page(_sorted_docs(docs, sort, direction), page, size), total

    def get_document(self, job_id: str, document_id: str) -> PiiDoc | None:
        return self._docs.get(job_id, {}).get(document_id)

    def set_validation(
        self, job_id: str, document_id: str, *, status: str, note: str,
        output_prefix: str | None = None, pages: list[dict[str, Any]] | None = None,
    ) -> None:
        doc = self.get_document(job_id, document_id)
        if doc is None:
            return
        doc.validation_status = status
        doc.note = note
        if output_prefix is not None:
            doc.output_prefix = output_prefix
        if pages is not None:
            doc.pages = pages

    def mark_rerunning(self, job_id: str, document_id: str) -> None:
        doc = self.get_document(job_id, document_id)
        if doc is not None:
            doc.rerunning = _now()

    def finalize_if_complete(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if job is None or job.total <= 0 or job.completed < job.total:
            return False
        if job_id in self._markers:
            return False
        self._markers.add(job_id)
        self.set_job_status(job_id, status="done")
        return True


# --- GCS implementation (no database) ---------------------------------------


class GcsPiiResultStore:
    """A :class:`PiiResultStore` over plain GCS objects (see module docstring).

    One immutable object per document → N parallel tasks never contend. Completion is
    latched with a generation-match marker (exactly once). Reads are TTL-cached and,
    for finished jobs, served from a single compacted ``index.json``.
    """

    def __init__(
        self, object_store: ObjectStore, control_uri: str, *, cache_ttl: float = 3.0
    ) -> None:
        self._os = object_store
        self._base = control_uri.rstrip("/")
        self._ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        # Per-document read cache keyed by GCS generation: a result object is immutable
        # once written (only validation/rerun rewrites it, which bumps the generation), so
        # an unchanged document is served from here instead of re-fetched every poll.
        self._doc_cache: dict[str, tuple[int, PiiDoc]] = {}

    # -- object helpers --
    def _job_dir(self, job_id: str) -> str:
        return f"{self._base}/{job_id}"

    def _read_json(self, uri: str) -> Any:
        try:
            return json.loads(self._os.read(uri))
        except Exception:
            return None

    def _write_json(self, uri: str, obj: Any) -> None:
        self._os.write(uri, json.dumps(obj).encode("utf-8"), content_type="application/json")

    def _cached(self, key: str, build: Any) -> Any:
        hit = self._cache.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < self._ttl:
            return hit[1]
        value = build()
        self._cache[key] = (time.monotonic(), value)
        return value

    def _invalidate(self, job_id: str) -> None:
        # Clear the (briefly-cached) results listing so the next read re-lists and picks up
        # the new generation of any just-written doc; the per-doc cache self-corrects by
        # generation, so individual docs need no explicit eviction.
        self._cache.pop(f"gens:{job_id}", None)

    def _result_gens(self, job_id: str) -> dict[str, int]:
        """``{uri: generation}`` for this job's result objects (one LIST, TTL-cached)."""
        return cast("dict[str, int]", self._cached(
            f"gens:{job_id}",
            lambda: self._os.list_with_generations(f"{self._job_dir(job_id)}/results/"),
        ))

    # -- jobs --
    def create_job(
        self, *, dataset: str, source_uri: str, output_uri: str, pii_types: list[str],
        total: int, review_mode: str = "all", review_threshold: float = 0.0,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = PiiJob(
            job_id=job_id, dataset=dataset, source_uri=source_uri, output_uri=output_uri,
            pii_types=pii_types, status="running", total=total, completed=0,
            created_at=_now(), review_mode=review_mode, review_threshold=review_threshold,
        )
        self._write_json(f"{self._job_dir(job_id)}/job.json", asdict(job))
        return job_id

    def _count_results(self, job_id: str) -> int:
        return sum(1 for u in self._result_gens(job_id) if u.endswith(".json"))

    def get_job(self, job_id: str) -> PiiJob | None:
        raw = self._read_json(f"{self._job_dir(job_id)}/job.json")
        if raw is None:
            return None
        job = PiiJob(**raw)
        job.completed = job.total if job.status == "done" else self._count_results(job_id)
        return job

    def list_jobs(self) -> list[PiiJob]:
        jobs = []
        for uri in self._os.list(f"{self._base}/"):
            if uri.endswith("/job.json"):
                raw = self._read_json(uri)
                if raw:
                    job = PiiJob(**raw)
                    # `completed` is never persisted (always 0 in job.json) — recompute it
                    # live, exactly as get_job does, so the job list shows real progress.
                    job.completed = (
                        job.total if job.status == "done" else self._count_results(job.job_id)
                    )
                    jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def set_job_status(
        self, job_id: str, *, status: str | None = None, total: int | None = None
    ) -> None:
        uri = f"{self._job_dir(job_id)}/job.json"
        raw = self._read_json(uri)
        if raw is None:
            return
        if status is not None:
            raw["status"] = status
        if total is not None:
            raw["total"] = total
        self._write_json(uri, raw)

    # -- documents --
    def save_document(self, doc: PiiDoc, *, task_index: int = 0) -> None:
        doc.created_at = doc.created_at or _now()
        self._write_json(
            f"{self._job_dir(doc.job_id)}/results/{doc.document_id}.json", asdict(doc)
        )
        self._invalidate(doc.job_id)

    def get_progress(self, job_id: str) -> Progress | None:
        raw = self._read_json(f"{self._job_dir(job_id)}/job.json")
        if raw is None:
            return None
        completed = raw["total"] if raw["status"] == "done" else self._count_results(job_id)
        return Progress(total=raw["total"], completed=completed, status=raw["status"])

    def _all_docs(self, job_id: str) -> list[PiiDoc]:
        # Read the LIVE per-doc results (never index.json — that snapshot would hide later
        # human validations). Each doc is cached by its GCS generation, so an unchanged
        # document (already-processed, pending, or validated) is NOT re-fetched on every
        # poll — only new docs and ones whose generation changed (validation / rerun) are
        # read. The result objects are immutable apart from those writes, so this is exact.
        if len(self._doc_cache) > 4096:  # bound memory on a long-lived warm instance
            self._doc_cache.clear()
        docs: list[PiiDoc] = []
        for uri, gen in self._result_gens(job_id).items():
            if not uri.endswith(".json"):
                continue
            hit = self._doc_cache.get(uri)
            if hit is not None and hit[0] == gen:
                docs.append(hit[1])
                continue
            raw = self._read_json(uri)
            if raw:
                doc = PiiDoc(**raw)
                self._doc_cache[uri] = (gen, doc)
                docs.append(doc)
        return docs

    def list_documents(
        self, job_id: str, *, sort: str = "document", direction: str = "asc",
        page: int = 1, size: int = 50, only_review: bool = False,
        verdict: str = "", validation: str = "",
    ) -> tuple[list[PiiDoc], int]:
        docs = _filter_docs(
            self._all_docs(job_id),
            only_review=only_review, verdict=verdict, validation=validation,
        )
        total = len(docs)
        return _page(_sorted_docs(docs, sort, direction), page, size), total

    def get_document(self, job_id: str, document_id: str) -> PiiDoc | None:
        raw = self._read_json(f"{self._job_dir(job_id)}/results/{document_id}.json")
        return PiiDoc(**raw) if raw else None

    def set_validation(
        self, job_id: str, document_id: str, *, status: str, note: str,
        output_prefix: str | None = None, pages: list[dict[str, Any]] | None = None,
    ) -> None:
        uri = f"{self._job_dir(job_id)}/results/{document_id}.json"
        raw = self._read_json(uri)
        if raw is None:
            return
        raw["validation_status"] = status
        raw["note"] = note
        if output_prefix is not None:
            raw["output_prefix"] = output_prefix
        if pages is not None:
            raw["pages"] = pages
        self._write_json(uri, raw)
        self._invalidate(job_id)

    def mark_rerunning(self, job_id: str, document_id: str) -> None:
        uri = f"{self._job_dir(job_id)}/results/{document_id}.json"
        raw = self._read_json(uri)
        if raw is None:
            return
        raw["rerunning"] = _now()
        self._write_json(uri, raw)
        self._invalidate(job_id)

    def finalize_if_complete(self, job_id: str) -> bool:
        raw = self._read_json(f"{self._job_dir(job_id)}/job.json")
        if raw is None or raw["status"] == "done" or raw["total"] <= 0:
            return False
        if self._count_results(job_id) < raw["total"]:
            return False
        # Generation-match latch: exactly one caller (worker task or the polling web
        # tier) wins and finalises — no locks, no database.
        if not self._os.create_if_absent(
            f"{self._job_dir(job_id)}/_complete.marker", _now().encode()
        ):
            return False
        docs = [asdict(d) for d in self._all_docs(job_id)]
        self._write_json(f"{self._job_dir(job_id)}/index.json", docs)  # compact for fast reads
        self.set_job_status(job_id, status="done")
        return True


def result_store_from_env(object_store: ObjectStore | None = None) -> PiiResultStore:
    """Build the GCS result store from ``PII_CONTROL_URI`` (or in-memory if unset)."""
    import os

    control = os.environ.get("PII_CONTROL_URI", "")
    if not control:
        return InMemoryPiiResultStore()
    if object_store is None:
        from .pii_batch import GcsObjectStore

        object_store = GcsObjectStore()
    return GcsPiiResultStore(object_store, control)
