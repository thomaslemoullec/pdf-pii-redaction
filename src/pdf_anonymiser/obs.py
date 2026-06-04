"""Observability — structured (JSON) logging that Cloud Logging parses into fields.

Cloud Run captures stdout; when a line is JSON, Cloud Logging promotes ``severity`` /
``message`` and puts everything else in ``jsonPayload``. We emit one structured record
per document and per job-lifecycle step (``event`` = ``pii.document`` / ``pii.job``) with
metric-bearing fields (verdict, attempts, leaks, seconds). The dashboard's **log-based
metrics** (see ``infra/monitoring.tf``) extract those fields — so the app only has to log,
never call a metrics API. Locally (no JSON handler configured) the same calls degrade to
ordinary text logs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

# Stable event markers the log-based metrics filter on.
EVENT_DOCUMENT = "pii.document"
EVENT_JOB = "pii.job"

_LOGGER = logging.getLogger("pdf_anonymiser")
_RESERVED = set(logging.makeLogRecord({}).__dict__)


class JsonFormatter(logging.Formatter):
    """Render a log record as a single Cloud Logging-friendly JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        # Any extra=... fields the caller bound (e.g. job_id, verdict) land in jsonPayload.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(*, level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger (idempotent). Call at entrypoints."""
    if getattr(configure_logging, "_done", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    configure_logging._done = True  # type: ignore[attr-defined]


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured log record: ``event`` marker + arbitrary metric fields."""
    _LOGGER.log(level, event, extra={"event": event, **fields})
