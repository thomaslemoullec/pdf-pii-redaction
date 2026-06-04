"""Exponential-backoff retry for transient external failures.

The splitter's hot path makes network calls that fail *transiently* — Vertex AI
returns 429 (rate limit) or 503 under load, GCS hiccups, the job-trigger API
times out. Those deserve a retry; a 400 (bad request) or a schema/validation
error does not. This module is the one place that knows the difference: a small
``retry_call`` with full-jitter exponential backoff and a transient-error
predicate, used to wrap the Gemini calls, the GCS fetch, and the job trigger.

Deterministic for tests: the sleeper and RNG are injectable, so a test asserts the
retry count without real sleeping or randomness.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

# HTTP statuses worth retrying: request timeout, client-cancel/deadline, rate limit, and
# the 5xx family. 499 (CANCELLED) shows up on the global endpoint when a slow image-gen
# call is cut — transient, so retry it.
_TRANSIENT_CODES = frozenset({408, 429, 499, 500, 502, 503, 504})

# Exception class names that are transient regardless of an exposed code — covers
# google-genai (ServerError), google-api-core, and requests/urllib network errors.
# Matched by name to avoid importing optional SDKs here.
_TRANSIENT_NAMES = frozenset(
    {
        "ServerError",
        "ServiceUnavailable",
        "ResourceExhausted",
        "DeadlineExceeded",
        "Cancelled",
        "CancelledError",
        "InternalServerError",
        "TooManyRequests",
        "Aborted",
        "RetryError",
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "RemoteDisconnected",
        "ServerDisconnectedError",
    }
)


def is_transient(exc: BaseException) -> bool:
    """True for errors a retry might fix (rate limits, 5xx, network blips)."""
    code = getattr(exc, "code", None)
    if not isinstance(code, int):
        code = getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _TRANSIENT_CODES:
        return True
    # requests.HTTPError carries the status on .response, not on the exception.
    response = getattr(exc, "response", None)
    resp_code = getattr(response, "status_code", None)
    if isinstance(resp_code, int) and resp_code in _TRANSIENT_CODES:
        return True
    return type(exc).__name__ in _TRANSIENT_NAMES


def retry_call[T](
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retryable: Callable[[BaseException], bool] = is_transient,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Call ``fn``; on a transient error, back off exponentially and retry.

    Backoff is **full jitter**: the wait before attempt *n* is uniform in
    ``[0, min(max_delay, base_delay * 2**(n-1))]`` — the AWS-recommended scheme,
    which spreads concurrent retries (every windowed call retrying in lockstep
    would just re-create the thundering herd that caused the 429).

    Re-raises immediately on a non-retryable error or once ``attempts`` is reached.
    """
    rand = rng or random.Random()
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not retryable(exc):
                raise
            ceiling = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = rand.uniform(0.0, ceiling)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
    # Unreachable: the loop either returns or raises. Re-raise for type-checkers.
    raise last_exc if last_exc is not None else RuntimeError("retry_call: no attempts")
