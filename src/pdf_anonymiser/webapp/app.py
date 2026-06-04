"""The PDF-anonymiser web app — launch a batch, watch progress, review each document.

A small FastAPI app over the GCS-only result store. Everything I/O-bound is injectable
(the object store, the result store, the Cloud Run launcher, the PII-type planner) so
the whole surface is unit-tested with in-memory fakes — no GCS, no Gemini, no network.

Routes (all server-rendered HTML, HTMX for live progress):
    GET  /                                  launcher + jobs list
    POST /jobs/suggest                      free-form description → suggested PII types
    POST /jobs                              create + launch a batch job
    GET  /jobs/{job_id}                     job detail — live progress + (once done) the run report
    GET  /jobs/{job_id}/review              jump to the next document awaiting review
    GET  /jobs/{job_id}/report              redirect to the job page (report is merged there now)
    GET  /jobs/{job_id}/progress            HTMX polling fragment (progress + report)
    GET  /jobs/{job_id}/doc/{doc_id}        one document — source vs synthetic + metrics
    POST /jobs/{job_id}/doc/{doc_id}/validate   validate/reject → advance to next
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..pii import PiiType
from ..pii_batch import ObjectStore

_HERE = Path(__file__).resolve().parent

# Cloud Run Jobs cap taskCount; bound the fan-out (documents beyond this share tasks).
_MAX_TASKS = 100

_ROUTING_LABELS = {
    "ok_for_global": "Low sensitivity",
    "redact_first": "Sensitive (localised)",
    "in_perimeter_only": "Highly sensitive",
}


def _routing_label(routing: str) -> str:
    return _ROUTING_LABELS.get(routing, (routing or "").replace("_", " ").capitalize())


# --- pure view helpers (no I/O) ---------------------------------------------


def _doc_summary(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a document's per-page data into a doc-level overview for the human."""
    by_type: dict[str, int] = {}
    by_detector: dict[str, int] = {}
    by_type_detector: dict[str, dict[str, int]] = {}
    leak_pages = 0
    for p in pages:
        for t, n in (p.get("by_type") or {}).items():
            by_type[t] = by_type.get(t, 0) + n
        for d, n in (p.get("by_detector") or {}).items():
            by_detector[d] = by_detector.get(d, 0) + n
        for t, per in (p.get("by_type_detector") or {}).items():
            dst = by_type_detector.setdefault(t, {})
            for d, n in per.items():
                dst[d] = dst.get(d, 0) + n
        if p.get("leaked_types"):
            leak_pages += 1
    return {
        "by_type": by_type, "by_detector": by_detector,
        "by_type_detector": by_type_detector, "total_pii": sum(by_type.values()),
        "page_count": len(pages), "leak_pages": leak_pages,
    }


def _batch_stats(documents: list[Any]) -> dict[str, Any]:
    """Job-level rollup for the progress view (cheap — from the document records)."""
    verdicts = {"pass": 0, "review": 0, "fail": 0, "error": 0}
    validation = {"validated": 0, "rejected": 0, "pending": 0}
    scores = []
    for d in documents:
        verdicts[d.verdict] = verdicts.get(d.verdict, 0) + 1
        validation[d.validation_status] = validation.get(d.validation_status, 0) + 1
        if d.verdict != "error":
            scores.append(d.score)
    avg = round(sum(scores) / len(scores), 3) if scores else None
    return {"verdicts": verdicts, "validation": validation, "avg_score": avg}


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _batch_report(job: Any, documents: list[Any]) -> dict[str, Any]:
    """Full run report: counts, PII totals, timing, and an estimated cost breakdown."""
    from ..pii_pricing import estimate_cost

    by_type: dict[str, int] = {}
    by_detector: dict[str, int] = {}
    pages = attempts_total = leaked = 0
    dlp_seen = False
    for d in documents:
        if d.verdict == "error":
            continue
        for p in d.pages:
            pages += 1
            attempts_total += int(p.get("attempts", 1) or 1)
            leaked += int(p.get("pii_leaked", 0) or 0)
            for t, n in (p.get("by_type") or {}).items():
                by_type[t] = by_type.get(t, 0) + n
            for det, n in (p.get("by_detector") or {}).items():
                by_detector[det] = by_detector.get(det, 0) + n
                if det == "dlp":
                    dlp_seen = True

    scanned = sum(by_type.values())
    # DLP cost has two parts: the input-scan ensemble (1/page, when DLP found anything)
    # and the certified value-carryover check (source page once + 1 per synthesis attempt,
    # on by default). Plus one Flash planner call for the job.
    leak_check_on = os.environ.get("PII_DLP_LEAK_CHECK", "1") != "0"
    cost = estimate_cost(
        pages=pages,
        attempts_total=attempts_total,
        dlp_input_pages=pages if dlp_seen else 0,
        dlp_carryover_inspects=(pages + attempts_total) if leak_check_on else 0,
        planner_calls=1,
    )
    proc_times = [d.processing_seconds for d in documents if d.processing_seconds]
    total_proc = round(sum(proc_times), 1)
    avg_proc = round(total_proc / len(proc_times), 1) if proc_times else 0.0
    # total elapsed: launch → the last document landing (includes job cold-start)
    starts = _parse_ts(job.created_at)
    ends = [t for t in (_parse_ts(d.created_at) for d in documents) if t]
    wall = round((max(ends) - starts).total_seconds(), 1) if (starts and ends) else None

    return {
        "job": job, "stats": _batch_stats(documents), "doc_count": len(documents),
        "error_count": sum(1 for d in documents if d.verdict == "error"),
        "pii_scanned": scanned, "pii_leaked": leaked,
        "pii_redacted": scanned - leaked, "by_type": by_type, "by_detector": by_detector,
        "attempts_total": attempts_total, "cost": cost, "wall_seconds": wall,
        "total_proc_seconds": total_proc, "avg_proc_seconds": avg_proc,
        "page_count": pages,  # pages processed (distinct from the table's pagination "pages")
    }


def _derive_status(status: str, review_pending: int) -> tuple[str, str]:
    """A friendly lifecycle label + badge css: running → pending review → validated."""
    if status == "error":
        return "error", "bad"
    if status != "done":
        return "running", ""
    return ("pending review", "") if review_pending else ("validated", "ok")


# --- the default Cloud Run launcher (no-op locally) -------------------------


def _default_launcher() -> Callable[..., None]:
    """Trigger the batch as a Cloud Run Job fan-out when configured, else no-op.

    When ``PII_BATCH_JOB_NAME`` is set (deployed), run the Job over the Cloud Run
    Admin REST API with ``taskCount`` = the document count, so N tasks each process a
    round-robin shard, and publish a "started" event. Locally (no job name) it is a
    no-op — run ``scripts/pii_batch.py`` directly, or inject a launcher in tests.
    """
    def launch(
        job_id: str, *, source: str, output: str, label: str, pii_types: str,
        limit: int | None = None, total: int = 0,
        fidelity_threshold: float = 0.85, score_threshold: float = 0.85, rerun: bool = False,
    ) -> None:
        job_name = os.environ.get("PII_BATCH_JOB_NAME")
        if not job_name:
            return
        project = os.environ.get("GCP_PROJECT", "")
        region = os.environ.get("GCP_REGION", "")
        task_count = max(1, min(total or 1, _MAX_TASKS))

        import google.auth
        import google.auth.transport.requests
        import requests

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        url = (
            f"https://run.googleapis.com/v2/projects/{project}/locations/"
            f"{region}/jobs/{job_name}:run"
        )
        args = ["--job-id", job_id, "--source", source, "--output", output,
                "--label", label, "--pii-types", pii_types,
                "--fidelity-threshold", str(fidelity_threshold),
                "--score-threshold", str(score_threshold)]
        if limit and limit > 0:
            args += ["--limit", str(limit)]
        if rerun:
            args += ["--rerun"]  # re-process this source without resetting the job
        body: dict[str, object] = {
            "overrides": {"containerOverrides": [{"args": args}], "taskCount": task_count}
        }

        from ..retry import retry_call

        def _post() -> None:
            resp = requests.post(
                url, json=body,
                headers={"Authorization": f"Bearer {creds.token}"}, timeout=30,
            )
            resp.raise_for_status()

        retry_call(_post)

        if rerun:  # a re-process of one document is not a new job → no "started" event
            return
        from ..pii_events import publish_started

        publish_started(
            project, os.environ.get("PII_EVENTS_TOPIC", ""),
            job_id=job_id, label=label, source=source, output=output, total=total,
            region=region, job_name=job_name,
        )

    return launch


def create_app(
    *,
    object_store: ObjectStore | None = None,
    result_store: Any | None = None,
    batch_launcher: Callable[..., None] | None = None,
    type_planner: Callable[[str], list[PiiType]] | None = None,
) -> FastAPI:
    """Build the app. All four collaborators are injectable for tests; production wires
    GCS + Gemini from the environment lazily."""
    from ..obs import configure_logging

    configure_logging()
    app = FastAPI(title="PDF Anonymiser")
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.globals["brand_logo"] = "/static/logo.svg"
    templates.env.globals["routing_label"] = _routing_label
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    def _object_store() -> ObjectStore:
        if object_store is not None:
            return object_store
        from ..pii_batch import GcsObjectStore

        return GcsObjectStore()

    _rs_cache: list[Any] = []

    def _result_store() -> Any:
        if result_store is not None:
            return result_store
        if not _rs_cache:
            from ..pii_result_store import result_store_from_env

            _rs_cache.append(result_store_from_env(_object_store()))
        return _rs_cache[0]

    def _plan_types(description: str) -> list[PiiType]:
        if type_planner is not None:
            return list(type_planner(description))
        from ..config import Settings
        from ..pii_type_agent import plan_pii_types

        return plan_pii_types(description, Settings.from_env())

    def _launch(job_id: str, **kw: Any) -> None:
        (batch_launcher or _default_launcher())(job_id, **kw)

    def _logs_url(job_id: str) -> str:
        job_name = os.environ.get("PII_BATCH_JOB_NAME")
        if not job_name:
            return ""
        from ..pii_events import batch_logs_url

        return batch_logs_url(
            os.environ.get("GCP_PROJECT", ""), os.environ.get("GCP_REGION", ""),
            job_name, job_id,
        )

    def _view_params(request: Request) -> dict[str, Any]:
        q = request.query_params
        return {
            "sort": q.get("sort", "review"), "direction": q.get("dir", "asc"),
            "page": max(1, int(q.get("page", "1") or 1)),
            "size": min(200, max(10, int(q.get("size", "50") or 50))),
            "only_review": q.get("review", "") == "1",
            "verdict": q.get("verdict", ""), "validation": q.get("validation", ""),
            "live": q.get("live", "1") != "0",  # auto-refresh on unless ?live=0
        }

    def _view_ctx(
        job: Any, *, sort: str, direction: str, page: int, size: int, only_review: bool,
        verdict: str = "", validation: str = "", live: bool = True,
    ) -> dict[str, Any]:
        rs = _result_store()
        if rs.finalize_if_complete(job.job_id):  # web-tier backstop for completion
            # If the web tier (not a worker) wins the latch, it owns the finished event,
            # so the notification still fires exactly once.
            from ..pii_events import publish_finished

            job_name = os.environ.get("PII_BATCH_JOB_NAME", "")
            if job_name:
                publish_finished(
                    rs, os.environ.get("GCP_PROJECT", ""),
                    os.environ.get("PII_EVENTS_TOPIC", ""),
                    job_id=job.job_id, region=os.environ.get("GCP_REGION", ""),
                    job_name=job_name,
                )
        all_docs, _ = rs.list_documents(job.job_id, size=10_000_000)  # full set for stats
        page_rows, total = rs.list_documents(
            job.job_id, sort=sort, direction=direction, page=page, size=size,
            only_review=only_review, verdict=verdict, validation=validation,
        )
        progress = rs.get_progress(job.job_id)
        done = progress.completed if progress else 0
        pct = int(done / job.total * 100) if job.total else (100 if job.status == "done" else 0)
        pages = max(1, (total + size - 1) // size)
        review_pending = sum(
            1 for d in all_docs if d.needs_review and d.validation_status == "pending"
        )
        rerunning_count = sum(1 for d in all_docs if getattr(d, "rerunning", ""))
        status_label, status_css = _derive_status(job.status, review_pending)
        # Merge the full run-report (PII totals, timing, cost) so the job page shows it
        # in place once the job is done — no separate report page to click through to.
        report = _batch_report(job, all_docs)
        return {
            **report,  # already includes "stats" computed from the same all_docs
            "job": job, "documents": page_rows, "done": done, "pct": pct,
            "review_count": sum(1 for d in all_docs if d.needs_review),
            "review_pending": review_pending,
            "status_label": status_label, "status_css": status_css,
            "sort": sort, "dir": direction, "page": page, "size": size, "pages": pages,
            "filtered_total": total, "only_review": only_review,
            "f_verdict": verdict, "f_validation": validation, "live": live,
            "rerunning_count": rerunning_count,
        }

    def _review_queue(job_id: str, *, exclude: str = "") -> list[Any]:
        """Documents still awaiting a human decision, worst-score first, minus ``exclude``."""
        docs, _ = _result_store().list_documents(
            job_id, sort="score", direction="asc", size=10_000_000, only_review=True,
        )
        return [d for d in docs
                if d.validation_status == "pending" and d.document_id != exclude]

    # --- routes -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        rs = _result_store()
        jobs = rs.list_jobs()
        statuses: dict[str, tuple[str, str]] = {}
        for j in jobs:
            pending = 0
            if j.status == "done":
                docs, _ = rs.list_documents(j.job_id, only_review=True, size=10_000_000)
                pending = sum(1 for d in docs if d.validation_status == "pending")
            statuses[j.job_id] = _derive_status(j.status, pending)
        return templates.TemplateResponse(
            request, "launch.html",
            {"jobs": jobs, "statuses": statuses, "pii_types": [t.value for t in PiiType]},
        )

    @app.post("/jobs/suggest", response_class=HTMLResponse)
    def suggest(request: Request, description: str = Form("")) -> Response:
        # A small LLM agent maps the operator's free-form description of the documents /
        # expected PII to the type vocabulary — robust to a diverse corpus (no sampling).
        # Empty description ⇒ no suggestion ⇒ scan for everything.
        ctx: dict[str, object] = {
            "pii_types": [t.value for t in PiiType],
            "described": bool(description.strip()), "error": None,
        }
        try:
            ctx["suggested"] = {t.value for t in _plan_types(description)}
        except Exception as exc:
            ctx["suggested"], ctx["error"] = set(), str(exc)
        return templates.TemplateResponse(request, "_type_picker.html", ctx)

    @app.post("/jobs")
    def launch_job(
        source: str = Form(...), output: str = Form(...), label: str = Form("documents"),
        types: list[str] = Form(default=[]), limit: int = Form(0),
        fidelity_threshold: float = Form(0.85), score_threshold: float = Form(0.85),
    ) -> Response:
        from ..pii_batch import list_source_pdfs

        capped = limit if limit > 0 else None  # ≤0 means "process the whole folder"
        try:
            total = len(list_source_pdfs(_object_store(), source, limit=capped))
        except Exception:
            total = 0
        job_id = _result_store().create_job(
            dataset=label, source_uri=source, output_uri=output,
            pii_types=list(types), total=total,
            fidelity_threshold=fidelity_threshold, score_threshold=score_threshold,
        )
        _launch(job_id, source=source, output=output, label=label,
                pii_types=",".join(types), limit=capped, total=total,
                fidelity_threshold=fidelity_threshold, score_threshold=score_threshold)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: str) -> Response:
        job = _result_store().get_job(job_id)
        if job is None:
            return PlainTextResponse("job not found", status_code=404)
        from ..pii_events import dashboard_url

        ctx = _view_ctx(job, **_view_params(request))
        ctx["logs_url"] = _logs_url(job_id)
        ctx["dashboard_url"] = dashboard_url()
        return templates.TemplateResponse(request, "job.html", ctx)

    @app.get("/jobs/{job_id}/review")
    def review_next(request: Request, job_id: str) -> Response:
        # Jump to the next document awaiting review (worst score first). When the queue
        # is empty, return to the job with a "review complete" flag.
        exclude = request.query_params.get("exclude", "")
        queue = _review_queue(job_id, exclude=exclude)
        if not queue:
            return RedirectResponse(f"/jobs/{job_id}?reviewed=1", status_code=303)
        return RedirectResponse(f"/jobs/{job_id}/doc/{queue[0].document_id}", status_code=303)

    @app.get("/jobs/{job_id}/report")
    def report(job_id: str) -> Response:
        # The run report is now merged into the job page (shown once the job is done).
        # Kept as a redirect so old links/bookmarks still land somewhere sensible.
        return RedirectResponse(f"/jobs/{job_id}", status_code=307)

    @app.get("/jobs/{job_id}/progress", response_class=HTMLResponse)
    def progress(request: Request, job_id: str) -> Response:
        # The HTMX polling target — re-rendered every few seconds until the job is done.
        job = _result_store().get_job(job_id)
        if job is None:
            return PlainTextResponse("job not found", status_code=404)
        return templates.TemplateResponse(
            request, "_progress.html", _view_ctx(job, **_view_params(request))
        )

    @app.get("/jobs/{job_id}/doc/{doc_id}", response_class=HTMLResponse)
    def document(request: Request, job_id: str, doc_id: str) -> Response:
        doc = _result_store().get_document(job_id, doc_id)
        if doc is None:
            return PlainTextResponse("document not found", status_code=404)
        # Page images are served LAZILY (see /src and /anon routes below) and loaded by the
        # browser on demand — so a 20-page document no longer renders + base64-inlines every
        # page into one response, which OOM'd the instance. The template just emits <img>
        # URLs; metadata is the raw per-page records.
        queue = _review_queue(job_id)
        position = next((i for i, d in enumerate(queue) if d.document_id == doc_id), None)
        return templates.TemplateResponse(
            request, "document.html",
            {"doc": doc, "pages": doc.pages, "summary": _doc_summary(doc.pages),
             "review_remaining": len(queue),
             "review_position": (position + 1) if position is not None else None},
        )

    def _png_response(data: bytes) -> Response:
        # Cache in the browser: page images are immutable for a given result.
        return Response(content=data, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/jobs/{job_id}/doc/{doc_id}/src/{idx}")
    def doc_source_page(job_id: str, doc_id: str, idx: int) -> Response:
        # Render ONE source page on demand (bounded memory). idx = 0-based PDF page index.
        doc = _result_store().get_document(job_id, doc_id)
        if doc is None:
            return Response(status_code=404)
        try:
            import io

            from ..gemini_client import render_pdf_page

            img = render_pdf_page(_object_store().read(doc.source_uri), idx, 150)
            if img is None:
                return Response(status_code=404)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return _png_response(buf.getvalue())
        except Exception:
            return Response(status_code=404)

    @app.get("/jobs/{job_id}/doc/{doc_id}/anon/{idx}")
    def doc_anon_page(job_id: str, doc_id: str, idx: int) -> Response:
        # Stream ONE stored synthetic page. idx = position in the document's pages list.
        doc = _result_store().get_document(job_id, doc_id)
        if doc is None or idx < 0 or idx >= len(doc.pages):
            return Response(status_code=404)
        uri = str(doc.pages[idx].get("anon_uri", ""))
        if not uri:
            return Response(status_code=404)
        try:
            return _png_response(_object_store().read(uri))
        except Exception:
            return Response(status_code=404)

    @app.post("/jobs/{job_id}/doc/{doc_id}/rerun")
    def rerun(job_id: str, doc_id: str) -> Response:
        # Re-process just this one document (e.g. after an intermittent 504). Re-triggers
        # the batch job pointed at the single source PDF, in rerun mode (no job reset / no
        # re-finalise) so it overwrites only this document's result.
        rs = _result_store()
        job, doc = rs.get_job(job_id), rs.get_document(job_id, doc_id)
        if job is None or doc is None:
            return PlainTextResponse("not found", status_code=404)
        # Flip the stored result to "processing" first, so the status/tags reset
        # immediately and the relaunch is obvious before the worker finishes.
        rs.mark_rerunning(job_id, doc_id)
        _launch(
            job_id, source=doc.source_uri, output=job.output_uri, label=job.dataset,
            pii_types=",".join(job.pii_types), total=1,
            fidelity_threshold=job.fidelity_threshold, score_threshold=job.score_threshold,
            rerun=True,
        )
        return RedirectResponse(f"/jobs/{job_id}/doc/{doc_id}?rerunning=1", status_code=303)

    @app.post("/jobs/{job_id}/rerun-errors")
    def rerun_errors(job_id: str) -> Response:
        # Relaunch every document currently in the error state — a simple filter over the
        # per-doc results we already store (verdict == "error"), then one re-run each.
        rs = _result_store()
        job = rs.get_job(job_id)
        if job is None:
            return PlainTextResponse("not found", status_code=404)
        errors, _ = rs.list_documents(job_id, verdict="error", size=10_000_000)
        for d in errors[:_MAX_TASKS]:  # bound the fan-out to the per-run task cap
            rs.mark_rerunning(job_id, d.document_id)
            _launch(
                job_id, source=d.source_uri, output=job.output_uri, label=job.dataset,
                pii_types=",".join(job.pii_types), total=1,
                fidelity_threshold=job.fidelity_threshold, score_threshold=job.score_threshold,
                rerun=True,
            )
        return RedirectResponse(f"/jobs/{job_id}?relaunched={len(errors[:_MAX_TASKS])}", status_code=303)

    @app.post("/jobs/{job_id}/doc/{doc_id}/validate")
    def validate(
        job_id: str, doc_id: str, decision: str = Form(...), note: str = Form(""),
    ) -> Response:
        from ..pii_batch import promote_document

        doc = _result_store().get_document(job_id, doc_id)
        if doc is None:
            return PlainTextResponse("document not found", status_code=404)
        approve = decision == "validate"
        src_seg, dst_seg = (
            ("unvalidated", "validated") if approve else ("validated", "unvalidated")
        )
        # Move the output to the validated/ (or unvalidated/) half of the corpus.
        page_uris = [p.get("anon_uri", "") for p in doc.pages if p.get("anon_uri")]
        page_uris.append(f"{doc.output_prefix}/{doc.document_id}.pdf")
        new_prefix, new_pages = doc.output_prefix, None
        try:
            new_prefix = promote_document(
                _object_store(), page_uris, output_prefix=doc.output_prefix, validate=approve,
            )
            if new_prefix != doc.output_prefix:
                new_pages = [
                    {**p, "anon_uri": str(p.get("anon_uri", "")).replace(
                        f"/{src_seg}/", f"/{dst_seg}/")}
                    for p in doc.pages
                ]
        except Exception:
            new_prefix, new_pages = doc.output_prefix, None
        _result_store().set_validation(
            job_id, doc_id, status="validated" if approve else "rejected", note=note,
            output_prefix=new_prefix, pages=new_pages,
        )
        # Advance to the next document awaiting review (skip the one just decided).
        return RedirectResponse(f"/jobs/{job_id}/review?exclude={doc_id}", status_code=303)

    return cast("FastAPI", app)
