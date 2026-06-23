"""Tests for the batch PII engine — object store, processor, sharding, promotion."""

from __future__ import annotations

import io

from PIL import Image

from pdf_anonymiser.pii import _TYPE_SENSITIVITY, PiiFinding, PiiLabel, PiiType, SensitivityPolicy
from pdf_anonymiser.pii_batch import (
    DocumentResult,
    InMemoryObjectStore,
    PiiBatchProcessor,
    list_source_pdfs,
    parse_gs_uri,
    promote_document,
    run_batch,
    shard,
)
from pdf_anonymiser.pii_result_store import InMemoryPiiResultStore
from pdf_anonymiser.pii_review import PiiReviewResult, ReviewEvent
from pdf_anonymiser.redaction_judge import RedactionReport
from pdf_anonymiser.redaction_metrics import compute_redaction_metrics


def _pdf_bytes(pages: int = 2) -> bytes:
    imgs = [Image.new("RGB", (40, 50), "white") for _ in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


class _FakeReview:
    """Yields a scan + one page_done per page + complete (mirrors the real service)."""

    def review_events(self, document_id, pages):  # type: ignore[no-untyped-def]
        findings = tuple(
            PiiFinding(PiiType.NAME, i, _TYPE_SENSITIVITY[PiiType.NAME], detector="gemini")
            for i in range(len(pages))
        )
        label = PiiLabel.from_findings(document_id, findings, SensitivityPolicy())
        yield ReviewEvent("scan_done", {"label": label})
        metrics = compute_redaction_metrics(
            [("name", "John Smith")], "Name John Smith", "Name Anna Mueller"
        )
        anon, reports = [], []
        for i, p in enumerate(pages):
            out = Image.new("RGB", p.size, "white")
            rep = RedactionReport(metrics=metrics, rationale="ok")
            anon.append(out)
            reports.append(rep)
            yield ReviewEvent("page_done", {
                "page": i, "source": p, "anon": out, "report": rep,
                "by_type": {"name": 1}, "by_detector": {"gemini": 1},
                "by_type_detector": {"name": {"gemini": 1}},
            })
        yield ReviewEvent("complete", {"result": PiiReviewResult(label, pages, anon, reports)})


def _renderer(pdf_bytes, dpi):  # type: ignore[no-untyped-def]
    return [Image.new("RGB", (40, 50), "white") for _ in range(2)]


class _FakeBlob:
    def __init__(self, store, bucket, name):  # type: ignore[no-untyped-def]
        self._store, self._bucket, self.name = store, bucket, name

    def download_as_bytes(self):  # type: ignore[no-untyped-def]
        return self._store[(self._bucket, self.name)]

    def upload_from_string(self, data, content_type=None):  # type: ignore[no-untyped-def]
        self._store[(self._bucket, self.name)] = data

    def delete(self):  # type: ignore[no-untyped-def]
        self._store.pop((self._bucket, self.name), None)


class _FakeBucket:
    def __init__(self, store, name):  # type: ignore[no-untyped-def]
        self._store, self._name = store, name

    def blob(self, name):  # type: ignore[no-untyped-def]
        return _FakeBlob(self._store, self._name, name)

    def copy_blob(self, src_blob, dst_bucket_obj, dst_name):  # type: ignore[no-untyped-def]
        data = self._store[(src_blob._bucket, src_blob.name)]
        self._store[(dst_bucket_obj._name, dst_name)] = data


class _FakeGcs:
    def __init__(self):  # type: ignore[no-untyped-def]
        self._store: dict = {}

    def bucket(self, name):  # type: ignore[no-untyped-def]
        return _FakeBucket(self._store, name)

    def list_blobs(self, bucket, prefix=""):  # type: ignore[no-untyped-def]
        return [_FakeBlob(self._store, bucket, n)
                for (b, n) in sorted(self._store) if b == bucket and n.startswith(prefix)]


def test_gcs_object_store_roundtrip() -> None:
    from pdf_anonymiser.pii_batch import GcsObjectStore

    store = GcsObjectStore(client=_FakeGcs())
    store.write("gs://b/in/a.pdf", b"PDF1", content_type="application/pdf")
    store.write("gs://b/in/sub/c.pdf", b"PDF2")
    assert store.read("gs://b/in/a.pdf") == b"PDF1"
    assert store.list("gs://b/in/") == ["gs://b/in/a.pdf", "gs://b/in/sub/c.pdf"]
    store.copy("gs://b/in/a.pdf", "gs://b/out/a.pdf")
    assert store.read("gs://b/out/a.pdf") == b"PDF1"
    store.delete("gs://b/in/a.pdf")
    assert store.list("gs://b/in/") == ["gs://b/in/sub/c.pdf"]


def test_parse_gs_uri() -> None:
    assert parse_gs_uri("gs://bucket/a/b.pdf") == ("bucket", "a/b.pdf")
    assert parse_gs_uri("gs://bucket") == ("bucket", "")


def test_shard_is_round_robin() -> None:
    items = [f"f{i}" for i in range(7)]
    assert shard(items, index=0, count=3) == ["f0", "f3", "f6"]
    assert shard(items, index=1, count=3) == ["f1", "f4"]
    assert shard(items, index=2, count=3) == ["f2", "f5"]
    assert shard(items, index=0, count=1) == items  # single task gets everything


def test_list_source_pdfs_filters_to_pdf() -> None:
    store = InMemoryObjectStore({
        "gs://src/in/a.pdf": b"1", "gs://src/in/b.PDF": b"2",
        "gs://src/in/notes.txt": b"x", "gs://src/in/sub/c.pdf": b"3",
    })
    found = list_source_pdfs(store, "gs://src/in/")
    assert found == ["gs://src/in/a.pdf", "gs://src/in/b.PDF", "gs://src/in/sub/c.pdf"]


def test_list_source_pdfs_limit_caps_the_sorted_work_list() -> None:
    store = InMemoryObjectStore({f"gs://src/in/d{i}.pdf": b"x" for i in range(10)})
    # limit takes the FIRST N of the sorted list (deterministic across tasks)
    assert list_source_pdfs(store, "gs://src/in/", limit=3) == [
        "gs://src/in/d0.pdf", "gs://src/in/d1.pdf", "gs://src/in/d2.pdf"
    ]
    assert len(list_source_pdfs(store, "gs://src/in/", limit=0)) == 10  # 0 = no cap
    assert len(list_source_pdfs(store, "gs://src/in/", limit=None)) == 10
    assert len(list_source_pdfs(store, "gs://src/in/", limit=99)) == 10  # cap > count


def test_run_batch_respects_the_limit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sources = [f"gs://src/in/d{i}.pdf" for i in range(6)]
    store, obj, job_id, proc = _job_with_sources(tmp_path, sources)
    summary = run_batch(store, obj, proc, job_id=job_id, source="gs://src/in/",
                        output="gs://out/kyc/pii_free", limit=2)
    assert {r.document_id for r in summary.processed} == {"d0", "d1"}
    job = store.get_job(job_id)
    assert job.total == 2 and job.status == "done"  # type: ignore[union-attr]


def test_process_document_writes_pii_free_output_and_records_pages() -> None:
    store = InMemoryObjectStore({"gs://src/in/acct-0001.pdf": _pdf_bytes(2)})
    proc = PiiBatchProcessor(review_service=_FakeReview(), renderer=_renderer, store=store)

    result = proc.process_document(
        "gs://src/in/acct-0001.pdf", dataset_base_uri="gs://out/kyc/pii_free"
    )

    assert result.document_id == "acct-0001"
    assert result.output_prefix == "gs://out/kyc/pii_free/unvalidated/acct-0001"
    assert result.verdict == "pass"
    assert len(result.pages) == 2
    assert result.pages[0].page_no == 1 and result.pages[0].by_type == {"name": 1}
    # the per-page record carries the full metrics + rationale (same as single-doc)
    p0 = result.pages[0]
    assert p0.removal_recall == 1.0 and p0.pii_total == 1 and p0.pii_leaked == 0
    assert 0.0 <= p0.fidelity <= 1.0 and p0.rationale == "ok"
    # synthetic pages + a combined PDF were written under unvalidated/
    assert "gs://out/kyc/pii_free/unvalidated/acct-0001/page-1.png" in store.blobs
    assert "gs://out/kyc/pii_free/unvalidated/acct-0001/page-2.png" in store.blobs
    assert result.pdf_uri in store.blobs
    assert store.blobs[result.pdf_uri].startswith(b"%PDF")


def test_process_document_tags_output_with_classification_metadata() -> None:
    # The computed sensitivity tier is persisted as custom object metadata on every
    # output write (pages + PDF), so the classification travels with the object.
    # _FakeReview reports a NAME finding (MEDIUM, unlocalised) → IN_PERIMETER_ONLY.
    store = InMemoryObjectStore({"gs://src/in/acct-0001.pdf": _pdf_bytes(2)})
    proc = PiiBatchProcessor(review_service=_FakeReview(), renderer=_renderer, store=store)

    result = proc.process_document(
        "gs://src/in/acct-0001.pdf", dataset_base_uri="gs://out/kyc/pii_free"
    )

    expected = {
        "sensitivity": "MEDIUM",
        "routing": "in_perimeter_only",
        "classified_by": "sensitivity_policy",
    }
    assert store.meta["gs://out/kyc/pii_free/unvalidated/acct-0001/page-1.png"] == expected
    assert store.meta[result.pdf_uri] == expected


def test_process_document_isolates_a_failed_page() -> None:
    # One page failing (e.g. a persistent 504) must NOT sink the whole multi-page doc:
    # the good pages are still written, the failed page is recorded as an error page,
    # and the doc verdict becomes "error" (worst-of-pages) → routed to review.
    class _PartialFailReview:
        def review_events(self, document_id, pages):  # type: ignore[no-untyped-def]
            label = PiiLabel.from_findings(document_id, (), SensitivityPolicy())
            yield ReviewEvent("scan_done", {"label": label})
            out = Image.new("RGB", pages[0].size, "white")
            rep = RedactionReport(metrics=compute_redaction_metrics([], "x", "x"), rationale="ok")
            yield ReviewEvent("page_done", {
                "page": 0, "source": pages[0], "anon": out, "report": rep,
                "by_type": {}, "by_detector": {}, "by_type_detector": {},
            })
            yield ReviewEvent("page_done", {
                "page": 1, "source": pages[1], "anon": None, "report": None,
                "error": "504 DEADLINE_EXCEEDED", "by_type": {}, "by_detector": {},
                "by_type_detector": {},
            })
            yield ReviewEvent("complete", {"result": PiiReviewResult(label, pages, [out], [rep])})

    store = InMemoryObjectStore({"gs://src/in/doc.pdf": _pdf_bytes(2)})
    proc = PiiBatchProcessor(review_service=_PartialFailReview(), renderer=_renderer, store=store)
    result = proc.process_document("gs://src/in/doc.pdf", dataset_base_uri="gs://out/k/pii_free")

    assert result.verdict == "error"  # worst-of-pages (one page failed)
    assert {p.page_no: p.verdict for p in result.pages} == {1: "pass", 2: "error"}
    # the good page's synthetic PNG was written; the failed page's was not
    assert "gs://out/k/pii_free/unvalidated/doc/page-1.png" in store.blobs
    assert "gs://out/k/pii_free/unvalidated/doc/page-2.png" not in store.blobs
    err = next(p for p in result.pages if p.verdict == "error")
    assert "504" in err.rationale


def test_zero_page_document_is_flagged_review_not_pass() -> None:
    # An empty render (unreadable/0-page PDF) must NOT record a clean 'pass'.
    store = InMemoryObjectStore({"gs://src/in/bad.pdf": b"not a real pdf"})

    def empty_renderer(pdf_bytes, dpi):  # type: ignore[no-untyped-def]
        return []

    proc = PiiBatchProcessor(
        review_service=_FakeReview(), renderer=empty_renderer, store=store
    )
    result = proc.process_document(
        "gs://src/in/bad.pdf", dataset_base_uri="gs://out/kyc/pii_free"
    )
    assert result.verdict == "review" and result.score == 0.0
    assert result.pages == []
    # no PDF written for an empty document
    assert result.pdf_uri not in store.blobs


def test_promote_moves_between_unvalidated_and_validated() -> None:
    store = InMemoryObjectStore({"gs://src/in/acct-0001.pdf": _pdf_bytes(1)})
    proc = PiiBatchProcessor(review_service=_FakeReview(), renderer=_renderer, store=store)
    result = proc.process_document(
        "gs://src/in/acct-0001.pdf", dataset_base_uri="gs://out/kyc/pii_free"
    )
    pages = [p.anon_uri for p in result.pages] + [result.pdf_uri]

    new_prefix = promote_document(store, pages, output_prefix=result.output_prefix, validate=True)

    assert new_prefix == "gs://out/kyc/pii_free/validated/acct-0001"
    # output now lives under validated/, and the unvalidated originals are gone
    assert "gs://out/kyc/pii_free/validated/acct-0001/page-1.png" in store.blobs
    assert "gs://out/kyc/pii_free/unvalidated/acct-0001/page-1.png" not in store.blobs


# --- run_batch: the shard runner behind the Cloud Run task (e2e + reliability) ---


def _job_with_sources(tmp_path, sources):  # type: ignore[no-untyped-def]
    store = InMemoryPiiResultStore()
    obj = InMemoryObjectStore({u: _pdf_bytes(1) for u in sources})
    job_id = store.create_job(
        dataset="kyc", source_uri="gs://src/in/", output_uri="gs://out/kyc/pii_free",
        pii_types=[], total=0,  # web tier didn't count; the worker will
    )
    proc = PiiBatchProcessor(review_service=_FakeReview(), renderer=_renderer, store=obj)
    return store, obj, job_id, proc


def test_run_batch_processes_all_and_autocompletes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sources = ["gs://src/in/a.pdf", "gs://src/in/b.pdf", "gs://src/in/c.pdf"]
    store, obj, job_id, proc = _job_with_sources(tmp_path, sources)

    summary = run_batch(store, obj, proc, job_id=job_id,
                        source="gs://src/in/", output="gs://out/kyc/pii_free")

    assert len(summary.processed) == 3 and summary.failed == []
    job = store.get_job(job_id)
    assert job.total == 3 and job.completed == 3 and job.status == "done"  # type: ignore[union-attr]
    docs = store.list_documents(job_id, size=1000)[0]
    assert {d.document_id for d in docs} == {"a", "b", "c"}
    # PII-free output actually written under unvalidated/
    assert "gs://out/kyc/pii_free/unvalidated/a/a.pdf" in obj.blobs


def test_run_batch_empty_source_marks_done(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store, obj, job_id, proc = _job_with_sources(tmp_path, [])
    fired = []
    summary = run_batch(store, obj, proc, job_id=job_id, source="gs://src/in/",
                        output="gs://out/kyc/pii_free", on_complete=lambda: fired.append(1))
    assert summary.processed == [] and summary.failed == []
    job = store.get_job(job_id)
    # an empty bucket must finalise, not hang 'running' forever
    assert job.status == "done" and job.total == 0  # type: ignore[union-attr]
    assert fired == [1]  # on_complete fires for the empty/finalised job


def test_run_batch_fires_on_complete_exactly_once_when_done(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sources = ["gs://src/in/a.pdf", "gs://src/in/b.pdf"]
    store, obj, job_id, proc = _job_with_sources(tmp_path, sources)
    fired = []
    run_batch(store, obj, proc, job_id=job_id, source="gs://src/in/",
              output="gs://out/kyc/pii_free", on_complete=lambda: fired.append(1))
    # single task processed both → it observed the job flip to done exactly once
    assert store.get_job(job_id).status == "done"  # type: ignore[union-attr]
    assert fired == [1]


def test_run_batch_does_not_fire_on_complete_while_running(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # task 0 of 2 processes only its shard (1 of 2 docs) → job still running → no fire
    sources = ["gs://src/in/a.pdf", "gs://src/in/b.pdf"]
    store, obj, job_id, proc = _job_with_sources(tmp_path, sources)
    fired = []
    run_batch(store, obj, proc, job_id=job_id, source="gs://src/in/",
              output="gs://out/kyc/pii_free", index=0, count=2,
              on_complete=lambda: fired.append(1))
    assert store.get_job(job_id).status == "running"  # type: ignore[union-attr]
    assert fired == []


class _FlakyProcessor:
    """Processes every doc except one, which raises (corrupt-PDF simulation)."""

    def __init__(self, bad_uri: str) -> None:
        self._bad = bad_uri

    def process_document(self, source_uri, *, dataset_base_uri):  # type: ignore[no-untyped-def]
        if source_uri == self._bad:
            raise RuntimeError("corrupt pdf")
        from pdf_anonymiser.pii_batch import _doc_id_from_uri
        doc_id = _doc_id_from_uri(source_uri)
        return DocumentResult(
            document_id=doc_id, source_uri=source_uri,
            output_prefix=f"{dataset_base_uri}/unvalidated/{doc_id}",
            routing="ok_for_global", max_sensitivity="LOW", verdict="pass", score=1.0, pages=[],
        )


def test_run_batch_isolates_a_single_document_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sources = ["gs://src/in/ok1.pdf", "gs://src/in/bad.pdf", "gs://src/in/ok2.pdf"]
    store, obj, job_id, _ = _job_with_sources(tmp_path, sources)
    flaky = _FlakyProcessor("gs://src/in/bad.pdf")

    summary = run_batch(store, obj, flaky, job_id=job_id,  # type: ignore[arg-type]
                        source="gs://src/in/", output="gs://out/kyc/pii_free")

    assert {r.document_id for r in summary.processed} == {"ok1", "ok2"}  # others survived
    assert len(summary.failed) == 1 and summary.failed[0][0] == "gs://src/in/bad.pdf"
    # the failed doc is RECORDED (verdict 'error') so the job still completes (3/3)
    # instead of hanging at 2/3, and the failure is visible to the human.
    job = store.get_job(job_id)
    assert job.total == 3 and job.completed == 3 and job.status == "done"  # type: ignore[union-attr]
    docs = {d.document_id: d for d in store.list_documents(job_id, size=1000)[0]}
    assert docs["bad"].verdict == "error"
    assert "corrupt pdf" in docs["bad"].pages[0]["error"]


def test_run_batch_two_tasks_cover_every_document(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sources = [f"gs://src/in/doc{i}.pdf" for i in range(4)]
    store, obj, job_id, proc = _job_with_sources(tmp_path, sources)

    s0 = run_batch(store, obj, proc, job_id=job_id, source="gs://src/in/",
                   output="gs://out/kyc/pii_free", index=0, count=2)
    s1 = run_batch(store, obj, proc, job_id=job_id, source="gs://src/in/",
                   output="gs://out/kyc/pii_free", index=1, count=2)

    # disjoint shards, union covers all 4
    ids0 = {r.document_id for r in s0.processed}
    ids1 = {r.document_id for r in s1.processed}
    assert ids0.isdisjoint(ids1) and ids0 | ids1 == {"doc0", "doc1", "doc2", "doc3"}
    job = store.get_job(job_id)
    assert job.completed == 4 and job.status == "done"  # type: ignore[union-attr]


class _ReadFailsStore:
    """Lists a key but fails to read it — a corrupt/permission read mid-batch."""

    def __init__(self, inner: InMemoryObjectStore, bad_uri: str) -> None:
        self._inner, self._bad = inner, bad_uri

    def list(self, prefix):  # type: ignore[no-untyped-def]
        return self._inner.list(prefix)

    def read(self, uri):  # type: ignore[no-untyped-def]
        if uri == self._bad:
            raise OSError("read failed")
        return self._inner.read(uri)

    def write(self, uri, data, *, content_type="application/octet-stream", metadata=None):  # type: ignore[no-untyped-def]
        self._inner.write(uri, data, content_type=content_type, metadata=metadata)

    def copy(self, s, d):  # type: ignore[no-untyped-def]
        self._inner.copy(s, d)

    def delete(self, uri):  # type: ignore[no-untyped-def]
        self._inner.delete(uri)


def test_run_batch_handles_a_read_error_as_a_doc_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = InMemoryPiiResultStore()
    inner = InMemoryObjectStore({
        "gs://src/in/good.pdf": _pdf_bytes(1), "gs://src/in/unreadable.pdf": _pdf_bytes(1),
    })
    flaky_store = _ReadFailsStore(inner, "gs://src/in/unreadable.pdf")
    job_id = store.create_job(
        dataset="k", source_uri="gs://src/in/", output_uri="gs://out/k/pii_free",
        pii_types=[], total=0,
    )
    proc = PiiBatchProcessor(review_service=_FakeReview(), renderer=_renderer, store=flaky_store)

    summary = run_batch(store, flaky_store, proc, job_id=job_id,  # type: ignore[arg-type]
                        source="gs://src/in/", output="gs://out/k/pii_free")

    assert {r.document_id for r in summary.processed} == {"good"}
    assert len(summary.failed) == 1 and "unreadable" in summary.failed[0][0]


def test_promote_missing_blob_raises_so_caller_can_stay_consistent() -> None:
    # promote of a non-existent page must raise (the UI route catches it and keeps
    # the DB ↔ storage consistent rather than claiming a move that didn't happen).
    import pytest
    store = InMemoryObjectStore()  # empty — the page doesn't exist
    with pytest.raises(KeyError):
        promote_document(
            store, ["gs://out/k/pii_free/unvalidated/a/page-1.png"],
            output_prefix="gs://out/k/pii_free/unvalidated/a", validate=True,
        )
