"""Tests for the batch PII lifecycle events (logs URL + best-effort publish)."""

from __future__ import annotations

from pdf_anonymiser.pii_events import batch_logs_url, publish_event


def test_batch_logs_url_is_a_scoped_logging_link() -> None:
    url = batch_logs_url("proj-x", "europe-west3", "idp-pii-batch-dev", "job123")
    assert url.startswith("https://console.cloud.google.com/logs/query;query=")
    assert url.endswith("?project=proj-x")
    # the query (url-encoded) targets the right job + run
    assert "cloud_run_job" in url
    assert "idp-pii-batch-dev" in url
    assert "job123" in url


def test_publish_event_noop_without_topic() -> None:
    # no topic configured (local/dev) → silently does nothing, never raises
    publish_event("proj-x", "", {"event": "started"})


def test_publish_event_swallows_publish_errors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # a broken publisher must NOT raise into the job/launcher
    import sys
    import types

    fake = types.ModuleType("pubsub_v1")

    class _Boom:
        def topic_path(self, *a):  # type: ignore[no-untyped-def]
            return "t"

        def publish(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("pubsub down")

    fake.PublisherClient = _Boom  # type: ignore[attr-defined]
    google_pkg = sys.modules.get("google") or types.ModuleType("google")
    cloud_pkg = sys.modules.get("google.cloud") or types.ModuleType("google.cloud")
    cloud_pkg.pubsub_v1 = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_pkg)
    monkeypatch.setitem(sys.modules, "google.cloud.pubsub_v1", fake)

    publish_event("proj-x", "some-topic", {"event": "finished"})  # no exception


# --- started / finished lifecycle events (links + breakdown) -------------------


class _FakeJob:
    total = 3
    completed = 3
    dataset = "invoices"
    source_uri = "gs://b/in/"
    output_uri = "gs://b/anonymised"


class _FakeDoc:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict


class _FakeStore:
    def get_job(self, job_id):  # type: ignore[no-untyped-def]
        return _FakeJob()

    def list_documents(self, job_id, *, size=0, **kw):  # type: ignore[no-untyped-def]
        return ([_FakeDoc("pass"), _FakeDoc("fail"), _FakeDoc("error")], 3)


def test_publish_started_includes_logs_and_dashboard_links(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pdf_anonymiser.pii_events as ev

    captured: dict = {}
    monkeypatch.setenv("PII_DASHBOARD_URL", "https://console.cloud.google.com/dash")
    monkeypatch.setattr(ev, "publish_event", lambda p, t, payload: captured.update(payload))

    ev.publish_started(
        "proj", "topic", job_id="j1", label="invoices", source="gs://b/in/",
        output="gs://b/anonymised", total=3, region="europe-west3", job_name="jn",
    )
    assert captured["event"] == "started" and captured["total"] == 3
    assert captured["dashboard_url"] == "https://console.cloud.google.com/dash"
    assert captured["logs_url"].startswith("https://console.cloud.google.com/logs/query")


def test_publish_finished_carries_breakdown_and_links(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pdf_anonymiser.pii_events as ev

    captured: dict = {}
    monkeypatch.setenv("PII_DASHBOARD_URL", "https://dash")
    monkeypatch.setattr(ev, "publish_event", lambda p, t, payload: captured.update(payload))

    ev.publish_finished(_FakeStore(), "proj", "topic", job_id="j1", region="r", job_name="jn")

    assert captured["event"] == "finished"
    assert captured["failed"] == 1 and captured["leaked"] == 1
    assert captured["verdicts"] == {"pass": 1, "fail": 1, "error": 1}
    assert captured["total"] == 3 and captured["completed"] == 3
    assert captured["dashboard_url"] == "https://dash"
    assert "logs_url" in captured


def test_publish_finished_noop_when_job_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pdf_anonymiser.pii_events as ev

    class _NoJob:
        def get_job(self, job_id):  # type: ignore[no-untyped-def]
            return None

    called = {"n": 0}
    monkeypatch.setattr(ev, "publish_event", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    ev.publish_finished(_NoJob(), "proj", "topic", job_id="j1", region="r", job_name="jn")
    assert called["n"] == 0  # nothing published when the job can't be read
