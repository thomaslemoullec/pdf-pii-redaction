"""Batch anonymisation worker — one Cloud Run task processes a shard of the folder.

Each task lists the source prefix, takes its round-robin shard (by
``CLOUD_RUN_TASK_INDEX`` / ``CLOUD_RUN_TASK_COUNT``), anonymises every document it
owns, and persists one immutable per-document result (PII-free output under
``unvalidated/``). N tasks run in parallel; the job auto-completes — exactly once —
when every document has landed, firing a Pub/Sub "finished" event.

    python -m pdf_anonymiser.batch_runner --job-id JID \
        --source gs://bucket/in/ --output gs://bucket/anonymised \
        --label invoices --pii-types name,iban_account
"""

from __future__ import annotations

import argparse
import os


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - deployed fan-out glue
    parser = argparse.ArgumentParser(description="Batch PDF anonymisation (one shard).")
    parser.add_argument("--job-id", default=os.environ.get("PII_JOB_ID"))
    parser.add_argument("--source", default=os.environ.get("PII_SOURCE_URI"))
    parser.add_argument("--output", default=os.environ.get("PII_OUTPUT_URI"))
    parser.add_argument("--label", default=os.environ.get("PII_LABEL", "documents"))
    parser.add_argument("--pii-types", default=os.environ.get("PII_TYPES", ""))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("PII_LIMIT", "0")))
    parser.add_argument("--review-mode", default=os.environ.get("PII_REVIEW_MODE", "all"))
    parser.add_argument(
        "--review-threshold", type=float,
        default=float(os.environ.get("PII_REVIEW_THRESHOLD", "0")),
    )
    parser.add_argument("--dpi", type=int, default=int(os.environ.get("RENDER_DPI", "150")))
    parser.add_argument(
        "--rerun", action="store_true", default=os.environ.get("PII_RERUN") == "1",
        help="re-process the source (e.g. a single doc) without resetting the job total",
    )
    args = parser.parse_args(argv)
    if not (args.job_id and args.source and args.output):
        parser.error("--job-id, --source and --output are required")

    from .config import Settings
    from .gemini_client import render_pdf
    from .obs import configure_logging
    from .pii import PiiType
    from .pii_batch import GcsObjectStore, PiiBatchProcessor, run_batch
    from .pii_events import publish_finished
    from .pii_result_store import ReviewPolicy, result_store_from_env
    from .pii_review import GeminiPiiReviewService

    configure_logging()

    # This task's position in the fan-out (Cloud Run sets these; default = solo run).
    index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))

    pii_types = [PiiType(t) for t in args.pii_types.split(",") if t.strip()] or None
    policy = ReviewPolicy(mode=args.review_mode, threshold=args.review_threshold)
    settings = Settings.from_env()
    object_store = GcsObjectStore()
    store = result_store_from_env(object_store)
    review = GeminiPiiReviewService(settings, pii_types=pii_types)
    processor = PiiBatchProcessor(
        review_service=review, renderer=render_pdf, store=object_store, dpi=args.dpi
    )

    project = settings.gcp_project
    topic = os.environ.get("PII_EVENTS_TOPIC", "")
    job_name = os.environ.get("CLOUD_RUN_JOB", "pdf-anonymiser-batch")

    def _publish_finished() -> None:
        publish_finished(
            store, project, topic,
            job_id=args.job_id, region=settings.region, job_name=job_name,
        )

    summary = run_batch(
        store, object_store, processor,
        job_id=args.job_id, source=args.source, output=args.output,
        index=index, count=count, limit=args.limit or None, review_policy=policy,
        on_complete=_publish_finished, rerun=args.rerun,
    )
    print(f"task {index}/{count}: {len(summary.processed)} processed, {len(summary.failed)} failed")
    for result in summary.processed:
        print(f"  ✓ {result.document_id} [{result.verdict}] {result.score:.2f}")
    for uri, err in summary.failed:
        print(f"  ✗ {uri}: {err}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
