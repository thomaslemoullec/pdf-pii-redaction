"""Unit tests for the exponential-backoff retry helper (REL-DEGRADE)."""

from __future__ import annotations

import random

import pytest

from pdf_anonymiser.retry import is_transient, retry_call


class _Coded(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"http {code}")
        self.code = code


class ServerError(Exception):  # name-matched transient (genai-style)
    pass


def test_is_transient_by_code_and_name() -> None:
    assert is_transient(_Coded(429))
    assert is_transient(_Coded(503))
    assert is_transient(ServerError())
    assert not is_transient(_Coded(400))
    assert not is_transient(ValueError("nope"))


def test_retry_succeeds_after_transient_failures() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Coded(503)
        return "ok"

    out = retry_call(
        flaky, attempts=4, base_delay=1.0, sleep=slept.append, rng=random.Random(0)
    )
    assert out == "ok"
    assert calls["n"] == 3
    assert len(slept) == 2  # two backoffs before the third (successful) try


def test_retry_gives_up_after_attempts_and_reraises() -> None:
    calls = {"n": 0}

    def always_503() -> None:
        calls["n"] += 1
        raise _Coded(503)

    with pytest.raises(_Coded):
        retry_call(always_503, attempts=3, sleep=lambda _: None, rng=random.Random(0))
    assert calls["n"] == 3  # exactly `attempts` tries, no more


def test_retry_does_not_retry_permanent_errors() -> None:
    calls = {"n": 0}

    def bad_request() -> None:
        calls["n"] += 1
        raise _Coded(400)

    with pytest.raises(_Coded):
        retry_call(bad_request, attempts=5, sleep=lambda _: None)
    assert calls["n"] == 1  # 400 is permanent → no retry


def test_full_jitter_stays_within_ceiling() -> None:
    slept: list[float] = []

    def always() -> None:
        raise ServerError()

    with pytest.raises(ServerError):
        retry_call(
            always, attempts=4, base_delay=1.0, max_delay=8.0,
            sleep=slept.append, rng=random.Random(1),
        )
    # ceilings are 1, 2, 4 for the three backoffs; jitter keeps each within [0, ceil].
    assert all(0.0 <= d <= c for d, c in zip(slept, [1.0, 2.0, 4.0], strict=True))
