"""Tests for the web app (GCS-only store): launch → review → validate/reject."""

from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from pdf_anonymiser.pii_batch import InMemoryObjectStore
from pdf_anonymiser.pii_result_store import InMemoryPiiResultStore, PiiDoc
from pdf_anonymiser.webapp import create_app


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def _pdf_bytes(pages: int = 1) -> bytes:
    imgs = [Image.new("RGB", (40, 50), "white") for _ in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def _client(tmp_path: Path, store_obj: InMemoryObjectStore, launches: list):  # type: ignore[no-untyped-def]
    rs = InMemoryPiiResultStore()

    def launcher(job_id, *, source, output, label, pii_types,  # type: ignore[no-untyped-def]
                 limit=None, total=0, fidelity_threshold=0.85, score_threshold=0.85, rerun=False):
        launches.append({"job_id": job_id, "types": pii_types, "limit": limit,
                         "total": total, "fidelity_threshold": fidelity_threshold,
                         "score_threshold": score_threshold, "rerun": rerun, "source": source})

    def planner(description):  # type: ignore[no-untyped-def]
        from pdf_anonymiser.pii import PiiType
        return [PiiType.NAME, PiiType.IBAN_ACCOUNT] if "iban" in description.lower() else []

    app = create_app(
        object_store=store_obj, result_store=rs,
        batch_launcher=launcher, type_planner=planner,
    )
    return TestClient(app), rs


def _run_job(rs, obj, job_id, **kw):  # type: ignore[no-untyped-def]
    from tests.test_pii_batch import _FakeReview, _renderer

    from pdf_anonymiser.pii_batch import PiiBatchProcessor, run_batch
    proc = PiiBatchProcessor(review_service=_FakeReview(), renderer=_renderer, store=obj)
    return run_batch(rs, obj, proc, job_id=job_id, source="gs://src/in/",
                     output="gs://out/kyc/pii_free", **kw)


def test_suggest_from_description_prechecks_planned_types(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client, _ = _client(tmp_path, InMemoryObjectStore(), [])
    r = client.post("/jobs/suggest", data={"description": "KYC forms with names and IBANs"})
    assert r.status_code == 200
    assert 'value="iban_account" checked' in r.text and 'value="name" checked' in r.text
    assert 'value="phone" checked' not in r.text


def test_suggest_empty_description_scans_everything(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client, _ = _client(tmp_path, InMemoryObjectStore(), [])
    r = client.post("/jobs/suggest", data={"description": ""})
    assert r.status_code == 200 and "checked" not in r.text


def test_launch_creates_job_and_calls_launcher(tmp_path) -> None:  # type: ignore[no-untyped-def]
    obj = InMemoryObjectStore({"gs://src/in/a.pdf": b"%PDF", "gs://src/in/b.pdf": b"%PDF"})
    launches: list = []  # type: ignore[type-arg]
    client, rs = _client(tmp_path, obj, launches)
    r = client.post("/jobs", data={
        "source": "gs://src/in/", "output": "gs://out/kyc/pii_free",
        "label": "kyc", "types": ["name", "iban_account"],
        "fidelity_threshold": "0.8", "score_threshold": "0.9",
    }, follow_redirects=False)
    assert r.status_code == 303
    jobs = rs.list_jobs()
    assert len(jobs) == 1 and jobs[0].total == 2 and jobs[0].pii_types == ["name", "iban_account"]
    assert jobs[0].fidelity_threshold == 0.8 and jobs[0].score_threshold == 0.9
    assert launches[0]["types"] == "name,iban_account" and launches[0]["total"] == 2
    assert launches[0]["fidelity_threshold"] == 0.8 and launches[0]["score_threshold"] == 0.9


def test_end_to_end_launch_process_accept_and_reject(tmp_path) -> None:  # type: ignore[no-untyped-def]
    obj = InMemoryObjectStore({
        "gs://src/in/a.pdf": _pdf_bytes(1), "gs://src/in/b.pdf": _pdf_bytes(1),
    })
    client, rs = _client(tmp_path, obj, [])
    client.post("/jobs", data={
        "source": "gs://src/in/", "output": "gs://out/kyc/pii_free",
        "label": "kyc", "types": ["name"],
    }, follow_redirects=False)
    job_id = rs.list_jobs()[0].job_id

    _run_job(rs, obj, job_id)
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "a" in detail.text and "b" in detail.text
    # job is done with docs needing review (policy "all") → lifecycle status, not "done"
    assert "pending review" in detail.text.lower()
    # the run report (cost + time + PII totals) is now merged into the job page once done
    assert "Estimated cost" in detail.text and "Nano Banana Pro" in detail.text
    assert "detected" in detail.text and "redacted" in detail.text

    # per-document page: source vs synthetic + rich metrics + judge flags + detection
    page = client.get(f"/jobs/{job_id}/doc/a")
    assert "Source (original)" in page.text and "Synthetic (PII-free)" in page.text
    assert "score" in page.text and "rationale:" in page.text
    assert "Detection" in page.text and "all PII replaced" in page.text

    # the old /report link still works — it redirects to the merged job page
    r = client.get(f"/jobs/{job_id}/report", follow_redirects=False)
    assert r.status_code in (303, 307) and f"/jobs/{job_id}" in r.headers["location"]

    # accept a → promoted to validated/
    client.post(f"/jobs/{job_id}/doc/a/validate",
                data={"decision": "validate", "note": ""}, follow_redirects=False)
    a = rs.get_document(job_id, "a")
    assert a.validation_status == "validated"
    assert a.output_prefix == "gs://out/kyc/pii_free/validated/a"
    assert "gs://out/kyc/pii_free/validated/a/page-1.png" in obj.blobs

    # reject b with a note → stays unvalidated
    client.post(f"/jobs/{job_id}/doc/b/validate",
                data={"decision": "reject", "note": "signature legible"}, follow_redirects=False)
    b = rs.get_document(job_id, "b")
    assert b.validation_status == "rejected" and b.note == "signature legible"
    assert b.output_prefix == "gs://out/kyc/pii_free/unvalidated/b"


def test_review_policy_flagged_auto_approves_clean_docs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # 'flagged' mode: a clean pass is auto-approved (not queued), a review/fail is queued.
    obj = InMemoryObjectStore({
        "gs://src/in/a.pdf": _pdf_bytes(1), "gs://src/in/b.pdf": _pdf_bytes(1),
    })
    client, rs = _client(tmp_path, obj, [])
    job_id = rs.create_job(
        dataset="k", source_uri="gs://src/in/", output_uri="gs://out/kyc/pii_free",
        pii_types=[], total=0, fidelity_threshold=0.85, score_threshold=0.85,
    )
    from pdf_anonymiser.pii_result_store import ReviewPolicy
    _run_job(rs, obj, job_id, review_policy=ReviewPolicy(score_threshold=0.0))
    # the fake review returns a clean 'pass' → auto-approved → not in the review queue
    queue, n = rs.list_documents(job_id, only_review=True)
    assert n == 0
    # but the "all" view shows them
    every, total = rs.list_documents(job_id)
    assert total == 2

    # the UI review filter reflects this
    detail = client.get(f"/jobs/{job_id}?review=1")
    assert "No documents need review" in detail.text


def test_validation_decision_can_be_toggled_back(tmp_path) -> None:  # type: ignore[no-untyped-def]
    obj = InMemoryObjectStore({"gs://src/in/a.pdf": _pdf_bytes(1)})
    client, rs = _client(tmp_path, obj, [])
    job_id = rs.create_job(
        dataset="k", source_uri="gs://src/in/", output_uri="gs://out/kyc/pii_free",
        pii_types=[], total=0,
    )
    _run_job(rs, obj, job_id)
    client.post(f"/jobs/{job_id}/doc/a/validate",
                data={"decision": "validate", "note": ""}, follow_redirects=False)
    assert "validated/a/page-1.png" in str(obj.blobs.keys())
    client.post(f"/jobs/{job_id}/doc/a/validate",
                data={"decision": "reject", "note": "on reflection"}, follow_redirects=False)
    doc = rs.get_document(job_id, "a")
    assert doc.validation_status == "rejected"
    assert doc.output_prefix == "gs://out/kyc/pii_free/unvalidated/a"
    assert doc.pages[0]["anon_uri"] == "gs://out/kyc/pii_free/unvalidated/a/page-1.png"


def test_guided_review_walks_the_queue_then_completes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # /review jumps to the worst-score pending doc; validating advances to the next;
    # when the queue empties it returns to the job with a "reviewed" flag.
    obj = InMemoryObjectStore()
    client, rs = _client(tmp_path, obj, [])
    job_id = rs.create_job(dataset="k", source_uri="s", output_uri="o", pii_types=[], total=2)
    for i, score in ((0, 0.9), (1, 0.4)):  # d01 is the worst → reviewed first
        rs.save_document(PiiDoc(
            job_id=job_id, document_id=f"d{i:02d}", source_uri="s", output_prefix="o",
            routing="ok_for_global", max_sensitivity="LOW", verdict="review",
            score=score, pages=[], needs_review=True,
        ))
    # the job view offers a "Start review (2 documents)" button
    detail = client.get(f"/jobs/{job_id}")
    assert "Start review (2 documents)" in detail.text

    # start → worst score first (d01)
    r = client.get(f"/jobs/{job_id}/review", follow_redirects=False)
    assert r.headers["location"] == f"/jobs/{job_id}/doc/d01"

    # validate d01 → advances to d00
    r = client.post(f"/jobs/{job_id}/doc/d01/validate",
                    data={"decision": "validate", "note": ""}, follow_redirects=False)
    assert r.headers["location"] == f"/jobs/{job_id}/review?exclude=d01"
    r = client.get(f"/jobs/{job_id}/review?exclude=d01", follow_redirects=False)
    assert r.headers["location"] == f"/jobs/{job_id}/doc/d00"

    # validate d00 → queue empty → back to job with reviewed flag
    r = client.post(f"/jobs/{job_id}/doc/d00/validate",
                    data={"decision": "validate", "note": ""}, follow_redirects=False)
    r = client.get(r.headers["location"], follow_redirects=False)
    assert r.headers["location"] == f"/jobs/{job_id}?reviewed=1"


def test_routing_label_humanised_on_doc_page(tmp_path) -> None:  # type: ignore[no-untyped-def]
    obj = InMemoryObjectStore()
    client, rs = _client(tmp_path, obj, [])
    job_id = rs.create_job(dataset="k", source_uri="s", output_uri="o", pii_types=[], total=1)
    rs.save_document(PiiDoc(
        job_id=job_id, document_id="a", source_uri="s", output_prefix="o",
        routing="in_perimeter_only", max_sensitivity="HIGH", verdict="review",
        score=0.5, pages=[], needs_review=True,
    ))
    page = client.get(f"/jobs/{job_id}/doc/a")
    assert "Highly sensitive" in page.text and "in_perimeter_only" not in page.text.split("title=")[0]


def test_launch_with_limit_caps_total(tmp_path) -> None:  # type: ignore[no-untyped-def]
    obj = InMemoryObjectStore({f"gs://src/in/d{i}.pdf": _pdf_bytes(1) for i in range(10)})
    launches: list = []  # type: ignore[type-arg]
    client, rs = _client(tmp_path, obj, launches)
    client.post("/jobs", data={
        "source": "gs://src/in/", "output": "gs://out/kyc/pii_free", "label": "kyc", "limit": 3,
    }, follow_redirects=False)
    assert rs.list_jobs()[0].total == 3 and launches[0]["limit"] == 3


def test_progress_partial_polls_then_stops(tmp_path) -> None:  # type: ignore[no-untyped-def]
    obj = InMemoryObjectStore({"gs://src/in/a.pdf": _pdf_bytes(1)})
    client, rs = _client(tmp_path, obj, [])
    job_id = rs.create_job(
        dataset="k", source_uri="gs://src/in/", output_uri="gs://out/kyc/pii_free",
        pii_types=[], total=1,
    )
    running = client.get(f"/jobs/{job_id}/progress")
    assert "every 10s" in running.text and "0/1" in running.text
    _run_job(rs, obj, job_id)
    done = client.get(f"/jobs/{job_id}/progress")
    assert "every 10s" not in done.text and "1/1" in done.text


def test_documents_sortable_and_paginated(tmp_path) -> None:  # type: ignore[no-untyped-def]
    obj = InMemoryObjectStore()
    client, rs = _client(tmp_path, obj, [])
    job_id = rs.create_job(dataset="k", source_uri="s", output_uri="o", pii_types=[], total=12)
    # 12 docs, ascending ids but DESCENDING score, so a score-asc sort reverses them
    for i in range(12):
        rs.save_document(PiiDoc(
            job_id=job_id, document_id=f"d{i:02d}", source_uri="s", output_prefix="o",
            routing="ok_for_global", max_sensitivity="LOW", verdict="pass",
            score=1.0 - i * 0.05, pages=[],
        ))
    # page size 10 → 2 pages; sorted by score asc, the lowest-score doc (d11) is first
    r = client.get(f"/jobs/{job_id}?sort=score&dir=asc&page=1&size=10")
    assert "page 1 of 2" in r.text
    assert "d11" in r.text and "d00" not in r.text  # lowest 10 on page 1; d00 (1.0) on page 2


def test_batch_routes_404_on_unknown_ids(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client, _ = _client(tmp_path, InMemoryObjectStore(), [])
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/progress").status_code == 404
    assert client.get("/jobs/nope/doc/x").status_code == 404
    r = client.post("/jobs/nope/doc/x/validate",
                    data={"decision": "validate", "note": ""}, follow_redirects=False)
    assert r.status_code == 404


def test_home_lists_jobs_and_offers_launcher(tmp_path) -> None:  # type: ignore[no-untyped-def]
    obj = InMemoryObjectStore()
    client, rs = _client(tmp_path, obj, [])
    rs.create_job(dataset="invoices", source_uri="gs://s/", output_uri="gs://o",
                  pii_types=[], total=0)
    r = client.get("/")
    assert r.status_code == 200
    assert "Anonymise a folder of PDFs" in r.text and "invoices" in r.text


def test_report_offers_start_review_and_done_banner(tmp_path) -> None:  # type: ignore[no-untyped-def]
    obj = InMemoryObjectStore()
    client, rs = _client(tmp_path, obj, [])
    job_id = rs.create_job(dataset="k", source_uri="s", output_uri="o", pii_types=[], total=1)
    rs.save_document(PiiDoc(
        job_id=job_id, document_id="a", source_uri="s", output_prefix="o",
        routing="ok_for_global", max_sensitivity="LOW", verdict="review",
        score=0.7, pages=[], needs_review=True,
    ))
    # the report offers to start the review
    report = client.get(f"/jobs/{job_id}/report")
    assert report.status_code == 200 and "Start review (1 document)" in report.text
    # once the only doc is decided, the job view shows the "review complete" banner
    client.post(f"/jobs/{job_id}/doc/a/validate",
                data={"decision": "validate", "note": ""}, follow_redirects=False)
    done = client.get(f"/jobs/{job_id}?reviewed=1")
    assert "Review complete" in done.text


def test_launch_records_total_zero_when_listing_fails(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ListFails(InMemoryObjectStore):
        def list(self, prefix):  # type: ignore[no-untyped-def]
            raise OSError("403")

    client, rs = _client(tmp_path, _ListFails(), [])
    client.post("/jobs", data={
        "source": "gs://src/in/", "output": "gs://out/kyc/pii_free", "label": "kyc",
    }, follow_redirects=False)
    job = rs.list_jobs()[0]
    assert job.total == 0 and job.status == "running"


def test_rerun_relaunches_a_single_document(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # An error document can be re-processed on its own: the launcher is re-triggered in
    # rerun mode, pointed at just that document's source PDF, without resetting the job.
    obj = InMemoryObjectStore()
    launches: list = []  # type: ignore[type-arg]
    client, rs = _client(tmp_path, obj, launches)
    job_id = rs.create_job(dataset="k", source_uri="gs://src/in/",
                           output_uri="gs://out/k/pii_free", pii_types=["name"], total=2)
    rs.save_document(PiiDoc(
        job_id=job_id, document_id="d1", source_uri="gs://src/in/d1.pdf",
        output_prefix="", routing="", max_sensitivity="", verdict="error",
        score=0.0, pages=[{"page_no": 0, "error": "504 DEADLINE_EXCEEDED"}],
    ))
    r = client.post(f"/jobs/{job_id}/doc/d1/rerun", follow_redirects=False)
    assert r.status_code == 303 and "rerunning=1" in r.headers["location"]
    assert launches and launches[-1]["rerun"] is True
    assert launches[-1]["source"] == "gs://src/in/d1.pdf"
    assert launches[-1]["total"] == 1  # single-doc rerun
    # the job's total is untouched (rerun doesn't reset it)
    assert rs.get_job(job_id).total == 2
