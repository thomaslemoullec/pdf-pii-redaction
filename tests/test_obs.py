"""Unit tests for structured (JSON) logging — the source the dashboard metrics read."""

from __future__ import annotations

import io
import json
import logging

from pdf_anonymiser import obs


def _capture(fn) -> dict:  # type: ignore[no-untyped-def]
    """Run fn() with a JSON handler bound to the package logger; return the parsed line."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(obs.JsonFormatter())
    logger = logging.getLogger("pdf_anonymiser")
    saved = (logger.handlers[:], logger.level, logger.propagate)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        fn()
    finally:
        logger.handlers, logger.level, logger.propagate = saved
    return json.loads(buf.getvalue())


def test_log_event_emits_json_with_severity_message_and_fields() -> None:
    rec = _capture(
        lambda: obs.log_event(obs.EVENT_DOCUMENT, job_id="j1", verdict="pass", score=0.91)
    )
    assert rec["severity"] == "INFO"
    assert rec["message"] == "pii.document"  # the event marker is the message
    assert rec["event"] == "pii.document"
    assert rec["job_id"] == "j1"
    assert rec["verdict"] == "pass"
    assert rec["score"] == 0.91


def test_log_event_serialises_non_json_values_via_default_str() -> None:
    rec = _capture(lambda: obs.log_event(obs.EVENT_JOB, when=object()))  # not JSON-native
    assert rec["event"] == "pii.job" and isinstance(rec["when"], str)


def test_json_formatter_includes_exception_when_present() -> None:
    def boom() -> None:
        try:
            raise ValueError("nope")
        except ValueError:
            logging.getLogger("pdf_anonymiser").error("pii.job", exc_info=True)

    rec = _capture(boom)
    assert "exception" in rec and "ValueError" in rec["exception"]


def test_configure_logging_is_idempotent() -> None:
    obs.configure_logging()
    root = logging.getLogger()
    n = len(root.handlers)
    obs.configure_logging()
    assert len(root.handlers) == n  # no duplicate handlers on repeat calls
