"""Tests for the GCS-only PII result store (both implementations) + review policy."""

from __future__ import annotations

import pytest

from pdf_anonymiser.pii_batch import InMemoryObjectStore
from pdf_anonymiser.pii_result_store import (
    GcsPiiResultStore,
    InMemoryPiiResultStore,
    PiiDoc,
    ReviewPolicy,
    result_store_from_env,
)


def _doc(job_id, doc_id, *, verdict="pass", score=0.95, needs_review=True):  # type: ignore[no-untyped-def]
    return PiiDoc(
        job_id=job_id, document_id=doc_id, source_uri=f"gs://s/{doc_id}.pdf",
        output_prefix=f"gs://o/unvalidated/{doc_id}", routing="ok_for_global",
        max_sensitivity="LOW", verdict=verdict, score=score,
        pages=[{"page_no": 1, "by_type": {"name": 1}}], needs_review=needs_review,
    )


@pytest.fixture(params=["memory", "gcs"])
def store(request):  # type: ignore[no-untyped-def]
    # a FRESH store per test (no state leaking across the parametrised cases)
    if request.param == "memory":
        return InMemoryPiiResultStore()
    return GcsPiiResultStore(InMemoryObjectStore(), "gs://b/_pii_runs", cache_ttl=0.0)


# --- ReviewPolicy (pure) ----------------------------------------------------


def test_review_policy_non_pass_always_queued() -> None:
    p = ReviewPolicy(score_threshold=0.0)
    assert not p.needs_review(verdict="pass", score=0.99)  # clean pass → skip
    assert p.needs_review(verdict="review", score=0.99)    # low fidelity / drift → queue
    assert p.needs_review(verdict="fail", score=0.99)      # leak → queue
    assert p.needs_review(verdict="error", score=0.0)      # processing error → queue


def test_review_policy_score_threshold_queues_low_passes() -> None:
    p = ReviewPolicy(score_threshold=0.95)
    assert p.needs_review(verdict="pass", score=0.90)      # below the bar → queue
    assert not p.needs_review(verdict="pass", score=0.97)  # above the bar → skip


# --- store CRUD (both implementations) --------------------------------------


def test_create_save_progress_and_get(store) -> None:  # type: ignore[no-untyped-def]
    job_id = store.create_job(
        dataset="kyc", source_uri="gs://s/", output_uri="gs://o", pii_types=["name"], total=2,
    )
    job = store.get_job(job_id)
    assert job.status == "running" and job.total == 2 and job.completed == 0
    assert job.pii_types == ["name"]

    store.save_document(_doc(job_id, "a"))
    prog = store.get_progress(job_id)
    assert prog.completed == 1 and prog.total == 2 and prog.status == "running"
    got = store.get_document(job_id, "a")
    assert got.document_id == "a" and got.verdict == "pass"


def test_finalize_is_exactly_once(store) -> None:  # type: ignore[no-untyped-def]
    job_id = store.create_job(
        dataset="k", source_uri="gs://s/", output_uri="gs://o", pii_types=[], total=2,
    )
    store.save_document(_doc(job_id, "a"))
    assert store.finalize_if_complete(job_id) is False  # 1/2 → not yet
    store.save_document(_doc(job_id, "b"))
    assert store.finalize_if_complete(job_id) is True   # 2/2 → first caller wins
    assert store.finalize_if_complete(job_id) is False  # second caller loses (idempotent)
    assert store.get_job(job_id).status == "done"


def test_list_documents_sort_and_paginate(store) -> None:  # type: ignore[no-untyped-def]
    job_id = store.create_job(
        dataset="k", source_uri="gs://s/", output_uri="gs://o", pii_types=[], total=3,
    )
    store.save_document(_doc(job_id, "a", score=0.99))
    store.save_document(_doc(job_id, "b", score=0.50))
    store.save_document(_doc(job_id, "c", score=0.75))

    rows, total = store.list_documents(job_id, sort="score", direction="asc", page=1, size=2)
    assert total == 3
    assert [d.document_id for d in rows] == ["b", "c"]  # lowest two, ascending
    page2, _ = store.list_documents(job_id, sort="score", direction="asc", page=2, size=2)
    assert [d.document_id for d in page2] == ["a"]


def test_list_documents_only_review_filter(store) -> None:  # type: ignore[no-untyped-def]
    job_id = store.create_job(
        dataset="k", source_uri="gs://s/", output_uri="gs://o", pii_types=[], total=3,
    )
    store.save_document(_doc(job_id, "a", needs_review=True))
    store.save_document(_doc(job_id, "b", needs_review=False))
    store.save_document(_doc(job_id, "c", needs_review=True))
    rows, total = store.list_documents(job_id, only_review=True)
    assert {d.document_id for d in rows} == {"a", "c"} and total == 2


def test_set_validation_persists(store) -> None:  # type: ignore[no-untyped-def]
    job_id = store.create_job(
        dataset="k", source_uri="gs://s/", output_uri="gs://o", pii_types=[], total=1,
    )
    store.save_document(_doc(job_id, "a"))
    store.set_validation(
        job_id, "a", status="validated", note="clean",
        output_prefix="gs://o/validated/a",
    )
    d = store.get_document(job_id, "a")
    assert d.validation_status == "validated" and d.note == "clean"
    assert d.output_prefix == "gs://o/validated/a"


def test_list_jobs_newest_first(store) -> None:  # type: ignore[no-untyped-def]
    j1 = store.create_job(dataset="a", source_uri="s", output_uri="o", pii_types=[], total=1)
    j2 = store.create_job(dataset="b", source_uri="s", output_uri="o", pii_types=[], total=1)
    ids = [j.job_id for j in store.list_jobs()]
    assert set(ids) == {j1, j2}


def test_gcs_store_uses_marker_object_for_completion() -> None:
    obj = InMemoryObjectStore()
    store = GcsPiiResultStore(obj, "gs://b/_pii_runs", cache_ttl=0.0)
    job_id = store.create_job(
        dataset="k", source_uri="s", output_uri="o", pii_types=[], total=1,
    )
    store.save_document(_doc(job_id, "a"))
    assert store.finalize_if_complete(job_id) is True
    # the durable artifacts exist in GCS: marker + compacted index
    assert f"gs://b/_pii_runs/{job_id}/_complete.marker" in obj.blobs
    assert f"gs://b/_pii_runs/{job_id}/index.json" in obj.blobs
    # job.json + per-doc result are plain objects (portable, BQ-loadable)
    assert f"gs://b/_pii_runs/{job_id}/job.json" in obj.blobs
    assert f"gs://b/_pii_runs/{job_id}/results/a.json" in obj.blobs


def test_gcs_store_tags_result_with_classification_metadata() -> None:
    # The result object carries the sensitivity tier + routing as custom object
    # metadata (queryable without parsing the JSON body). Re-attached on rewrites
    # (validation / rerun) so it survives, and the per-doc JSON still holds the values.
    obj = InMemoryObjectStore()
    store = GcsPiiResultStore(obj, "gs://b/_pii_runs", cache_ttl=0.0)
    job_id = store.create_job(
        dataset="k", source_uri="s", output_uri="o", pii_types=[], total=1,
    )
    doc = _doc(job_id, "a")
    doc.routing, doc.max_sensitivity = "in_perimeter_only", "HIGH"
    store.save_document(doc)

    uri = f"gs://b/_pii_runs/{job_id}/results/a.json"
    expected = {
        "sensitivity": "HIGH",
        "routing": "in_perimeter_only",
        "classified_by": "sensitivity_policy",
    }
    assert obj.meta[uri] == expected
    # a validation rewrite preserves the classification metadata
    store.set_validation(job_id, "a", status="validated", note="ok")
    assert obj.meta[uri] == expected
    # and the values remain queryable from the record itself
    assert store.get_document(job_id, "a").max_sensitivity == "HIGH"


def test_classification_metadata_falls_back_for_unclassified() -> None:
    from pdf_anonymiser.pii_result_store import classification_metadata

    # an error document never got classified (empty routing/sensitivity) → lowest tier
    assert classification_metadata(routing="", max_sensitivity="") == {
        "sensitivity": "NONE",
        "routing": "ok_for_global",
        "classified_by": "sensitivity_policy",
    }


def test_result_store_from_env_defaults_to_memory(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("PII_CONTROL_URI", raising=False)
    assert isinstance(result_store_from_env(), InMemoryPiiResultStore)


def test_gcs_two_tasks_race_completion_marker_exactly_one_wins() -> None:
    # Two GcsPiiResultStore instances over ONE bucket = two Cloud Run tasks. Both see
    # the job complete and call finalize — the generation-match marker lets exactly one win.
    obj = InMemoryObjectStore()
    a = GcsPiiResultStore(obj, "gs://b/_pii_runs", cache_ttl=0.0)
    b = GcsPiiResultStore(obj, "gs://b/_pii_runs", cache_ttl=0.0)
    job_id = a.create_job(dataset="k", source_uri="s", output_uri="o", pii_types=[], total=2)
    a.save_document(_doc(job_id, "d0"))
    b.save_document(_doc(job_id, "d1"))
    results = [a.finalize_if_complete(job_id), b.finalize_if_complete(job_id)]
    assert results.count(True) == 1  # exactly one task finalises
    assert a.get_job(job_id).status == "done"


def test_get_progress_and_get_job_none_for_unknown() -> None:
    store = GcsPiiResultStore(InMemoryObjectStore(), "gs://b/_pii_runs", cache_ttl=0.0)
    assert store.get_job("nope") is None
    assert store.get_progress("nope") is None
    assert store.get_document("nope", "x") is None
    assert store.list_documents("nope") == ([], 0)


def test_list_jobs_reports_live_completed(store) -> None:  # type: ignore[no-untyped-def]
    # Regression: list_jobs must recompute `completed` live (it's never persisted — the
    # stored value is always 0), so the job list shows real progress, not 0/N.
    job_id = store.create_job(dataset="a", source_uri="s", output_uri="o", pii_types=[], total=3)
    store.save_document(_doc(job_id, "a"))
    store.save_document(_doc(job_id, "b"))
    job = next(j for j in store.list_jobs() if j.job_id == job_id)
    assert job.completed == 2  # running: counted from the result objects
    store.set_job_status(job_id, status="done")
    job = next(j for j in store.list_jobs() if j.job_id == job_id)
    assert job.completed == 3  # done: equals total
