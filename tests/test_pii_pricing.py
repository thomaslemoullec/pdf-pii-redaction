"""Tests for the batch PII cost estimate (pure arithmetic over public list prices)."""

from __future__ import annotations

from pdf_anonymiser.pii_pricing import (
    COST_DLP_INSPECT_PAGE,
    COST_FLASH_CALL,
    COST_PRO_IMAGE_GEN,
    COST_PRO_VISION_CALL,
    estimate_cost,
)


def test_estimate_cost_breakdown() -> None:
    # 2 pages, 3 synthesis attempts total; DLP input scan on both pages; carryover on
    # (source page once per page + one per attempt = 2 + 3 = 5); one Flash planner call.
    est = estimate_cost(
        pages=2, attempts_total=3, dlp_input_pages=2, dlp_carryover_inspects=5, planner_calls=1,
    )
    # per page: scan + analyse (2); per attempt: transcribe + judge (2)
    assert est.vision_calls == 2 * 2 + 3 * 2  # 10
    assert est.planner_calls == 1
    assert est.image_gens == 3
    assert est.dlp_inspects == 2 + 5  # input scan + carryover
    assert est.vision_cost == round(10 * COST_PRO_VISION_CALL, 4)
    assert est.planner_cost == round(1 * COST_FLASH_CALL, 4)
    assert est.image_cost == round(3 * COST_PRO_IMAGE_GEN, 4)
    assert est.dlp_cost == round(7 * COST_DLP_INSPECT_PAGE, 4)
    assert est.total == round(
        est.vision_cost + est.planner_cost + est.image_cost + est.dlp_cost, 4
    )


def test_estimate_cost_no_dlp_is_zero_dlp() -> None:
    est = estimate_cost(pages=1, attempts_total=1)  # DLP + planner default to 0
    assert est.dlp_inspects == 0 and est.dlp_cost == 0.0
    assert est.planner_calls == 0 and est.planner_cost == 0.0
    assert est.image_gens == 1  # one attempt → one image
