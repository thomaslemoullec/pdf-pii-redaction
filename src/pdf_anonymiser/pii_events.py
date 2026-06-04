"""Pub/Sub lifecycle events for batch PII jobs.

External systems can subscribe to a topic to know when a batch job **starts** and
**finishes** — each message carries the job id, dataset, source/output URIs, counts, a
verdict breakdown (on finish), and clickable **logs** + **dashboard** URLs. Publishing is
best-effort and no-op when no topic is configured (local/dev), so it never blocks a run.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .obs import EVENT_JOB, log_event

if TYPE_CHECKING:
    from .pii_result_store import PiiResultStore

_log = logging.getLogger(__name__)


def dashboard_url() -> str:
    """The per-deployment Cloud Monitoring dashboard URL (set by Terraform), or ""."""
    return os.environ.get("PII_DASHBOARD_URL", "")


def batch_logs_url(project: str, region: str, job_name: str, job_id: str = "") -> str:
    """A Cloud Logging Explorer URL streaming a batch job's logs (newest first).

    Scoped to the Cloud Run **job** (all of its live processing logs — Gemini/DLP calls,
    progress, per-document records). We intentionally do NOT narrow by ``job_id`` as a
    free-text term: only the app's per-document summary lines carry that id, so adding it
    would hide the bulk of the useful processing logs. ``job_id`` is accepted for call
    compatibility but not used as a filter.
    """
    _ = job_id  # not used as a filter (see docstring)
    terms = [
        'resource.type="cloud_run_job"',
        f'resource.labels.job_name="{job_name}"',
        f'resource.labels.location="{region}"',
    ]
    query = quote("\n".join(terms), safe="")
    return f"https://console.cloud.google.com/logs/query;query={query}?project={project}"


def publish_event(project: str, topic: str, payload: dict[str, Any]) -> None:
    """Publish one JSON event to ``topic`` (no-op if ``topic`` is empty).

    Best-effort: a publish failure is logged, never raised — telemetry must not break
    the job. ``topic`` is a short id (the publisher resolves the full path).
    """
    if not topic:
        return
    try:
        from google.cloud import pubsub_v1

        publisher = pubsub_v1.PublisherClient()
        path = publisher.topic_path(project, topic)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        publisher.publish(path, data, event=str(payload.get("event", ""))).result(timeout=30)
    except Exception as exc:
        _log.warning("pii batch: failed to publish %s event: %s", payload.get("event"), exc)


def publish_started(
    project: str,
    topic: str,
    *,
    job_id: str,
    label: str,
    source: str,
    output: str,
    total: int,
    region: str,
    job_name: str,
) -> None:
    """Publish the ``started`` lifecycle event (best-effort), with logs + dashboard links."""
    publish_event(project, topic, {
        "event": "started", "job_id": job_id, "label": label,
        "source": source, "output": output, "total": total,
        "logs_url": batch_logs_url(project, region, job_name, job_id),
        "dashboard_url": dashboard_url(),
    })


def publish_finished(
    store: PiiResultStore,
    project: str,
    topic: str,
    *,
    job_id: str,
    region: str,
    job_name: str,
) -> None:
    """Publish the ``finished`` event for a completed job, read from the store.

    Carries the verdict breakdown (failed/leaked counts) and the logs + dashboard links.
    Called by whichever caller wins the completion latch (a worker task or the web-tier
    backstop), so the notification fires exactly once regardless of who finalised. Also
    emits a structured ``pii.job`` finished log (feeds the dashboard metrics).
    """
    job = store.get_job(job_id)
    if job is None:
        return
    docs, _ = store.list_documents(job_id, size=1_000_000)
    verdicts = Counter(d.verdict for d in docs)
    failed = verdicts.get("error", 0)
    leaked = verdicts.get("fail", 0)
    publish_event(project, topic, {
        "event": "finished", "job_id": job_id, "label": job.dataset,
        "source": job.source_uri, "output": job.output_uri,
        "total": job.total, "completed": job.completed,
        "failed": failed, "leaked": leaked, "verdicts": dict(verdicts),
        "logs_url": batch_logs_url(project, region, job_name, job_id),
        "dashboard_url": dashboard_url(),
    })
    log_event(
        EVENT_JOB, phase="finished", job_id=job_id, total=job.total,
        failed=failed, leaked=leaked,
    )
