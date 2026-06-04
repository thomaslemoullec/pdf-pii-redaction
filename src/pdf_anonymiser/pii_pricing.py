"""Rough cost ESTIMATE for a batch PII run, from public list prices.

NOT a billing source of truth: actual cost depends on per-page token counts (which we
don't meter) and preview-model pricing changes often. The rates below are public list
prices (captured 2026-06, USD) kept as plain constants so they're trivial to update,
and the report shows the breakdown + this caveat next to the number.

Calls counted (per the pipeline):
- **Planner** — 1 Gemini *Flash* call per job (free-text description → PII-type scope).
- **Pro vision** — per page: PII scan + source analysis; per synthesis attempt:
  transcribe the output + the LLM judge.  (= ``pages*2 + attempts*2``)
- **Image gen** — 1 Nano Banana Pro generation per synthesis attempt.
- **Cloud DLP** — input scan (1 per page, when the DLP ensemble is on) + the certified
  value-carryover check (source page once + 1 per synthesis attempt, when on).
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Public list prices (USD), 2026-06. UPDATE if pricing changes. ----------
COST_PRO_VISION_CALL = 0.005   # one Gemini Pro vision request (~1 image in, short JSON out)
COST_FLASH_CALL = 0.0005       # one Gemini Flash request (the planner)
COST_PRO_IMAGE_GEN = 0.04      # one Nano Banana Pro image generation
COST_DLP_INSPECT_PAGE = 0.0015  # one Cloud DLP image inspectContent


@dataclass(frozen=True)
class CostEstimate:
    """A transparent cost breakdown for the batch report (all figures are estimates)."""

    vision_calls: int
    planner_calls: int
    image_gens: int
    dlp_inspects: int
    vision_cost: float
    planner_cost: float
    image_cost: float
    dlp_cost: float

    @property
    def total(self) -> float:
        return round(self.vision_cost + self.planner_cost + self.image_cost + self.dlp_cost, 4)


def estimate_cost(
    *,
    pages: int,
    attempts_total: int,
    dlp_input_pages: int = 0,
    dlp_carryover_inspects: int = 0,
    planner_calls: int = 0,
) -> CostEstimate:
    """Estimate USD cost from the call counts (see module docstring for the model).

    ``pages`` = anonymised pages; ``attempts_total`` = sum of synthesis attempts across
    those pages; ``dlp_input_pages`` = DLP input-scan inspections; ``dlp_carryover_inspects``
    = DLP value-carryover inspections (source + per-attempt); ``planner_calls`` = Flash
    planner calls (≈ 1 per job).
    """
    vision_calls = pages * 2 + attempts_total * 2  # scan+analyse per page; transcribe+judge per attempt
    image_gens = attempts_total
    dlp_inspects = dlp_input_pages + dlp_carryover_inspects
    return CostEstimate(
        vision_calls=vision_calls,
        planner_calls=planner_calls,
        image_gens=image_gens,
        dlp_inspects=dlp_inspects,
        vision_cost=round(vision_calls * COST_PRO_VISION_CALL, 4),
        planner_cost=round(planner_calls * COST_FLASH_CALL, 4),
        image_cost=round(image_gens * COST_PRO_IMAGE_GEN, 4),
        dlp_cost=round(dlp_inspects * COST_DLP_INSPECT_PAGE, 4),
    )
