"""Runtime configuration — everything operational comes from the environment.

Nothing is hard-coded: the same image runs local / dev / prod by environment alone.
Defaults lean privacy-first (EU data residency) and safe (Nano Banana *Pro* for
anonymisation, which removes salutation/signature names that the flash model leaks).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the PDF anonymiser."""

    gcp_project: str
    region: str
    # Vertex AI location for the Gemini calls. Defaults to an EU region so raw page
    # content (which contains real PII at scan/judge time) stays in the EU perimeter.
    # NOTE: some *preview* models are only served on the "global" endpoint. If you pin
    # a preview vision/image model that has no EU endpoint, set GEMINI_LOCATION=global
    # and accept the residency trade-off, or choose an EU-available GA model. See the
    # README "Data residency" section.
    gemini_location: str
    # The grounded vision model used for PII detection, the redaction judge, and the
    # source analyzer (one model, three vision/reasoning roles). Accuracy-critical: a
    # miss here is a leak, so this stays on the high-capability Pro tier.
    vision_model: str
    # The PII-type planner maps a free-text description to our PII vocabulary at launch.
    # It's a lightweight, text-only, non-detection task, so it runs on the cheaper/faster
    # Flash tier — getting it slightly wrong only widens/narrows the scan scope, it can't
    # cause a leak (the vision scan is the real detector). Override with PLANNER_MODEL.
    planner_model: str
    # The image model for synthetic anonymisation. Nano Banana *Pro* by default: on
    # dense real-world pages, the flash model left names in salutations/signatures (a
    # leak), while Pro reached full removal + fidelity. Flip to flash via IMAGE_MODEL.
    image_model: str
    # PDF → page raster resolution (DPI). 150 balances OCR fidelity vs image-gen cost.
    render_dpi: int
    # Exponential-backoff retry for transient Gemini failures (429/503/504/network).
    gemini_max_attempts: int
    gemini_retry_base_delay_s: float
    # Per-request timeout in MILLISECONDS (google-genai HttpOptions.timeout unit), so a
    # wedged Vertex call fails fast and is retried rather than hanging a batch task.
    # 240000 ms = 240 s — headroom for slow Pro image generation on the global endpoint
    # (which can exceed 2 min on dense scanned pages); transient 504s are still retried.
    gemini_timeout_ms: int
    # PII detection ensemble: when on, union Gemini-vision findings with Cloud DLP's
    # certified infoType detectors (checksum-validated IBANs, etc.). Off by default —
    # DLP needs the API enabled + roles/dlp.user; flip with PII_USE_DLP=1.
    pii_use_dlp: bool
    # Certified leak check (value-carryover). DLP reads the REAL values on the source
    # page AND on the synthetic page; any source value that still appears in the output
    # is a hard, certified leak (forces a retry, then a fail). ON by default. Unlike a
    # naive "is there any PII of this type?" re-scan, this compares VALUES, so the
    # synthetic fakes (different values) never trip it — which is why it's safe to leave
    # on. Needs DLP (roles/dlp.user); the extracted values are used in-memory only,
    # never persisted or logged. Disable with PII_DLP_LEAK_CHECK=0.
    pii_dlp_leak_check: bool
    # How many pages to anonymise+judge concurrently per document. Pages are
    # independent and the slow leg is Pro image-gen, so parallelism turns an N-page
    # wait into ~one page's wait. Capped to stay within Vertex quota.
    pii_max_parallel: int

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from environment variables, with sensible local-dev defaults."""
        return cls(
            gcp_project=os.environ.get("GCP_PROJECT", "local-dev"),
            region=os.environ.get("GCP_REGION", "europe-west3"),
            gemini_location=os.environ.get("GEMINI_LOCATION", "europe-west4"),
            vision_model=os.environ.get("VISION_MODEL", "gemini-2.5-pro"),
            planner_model=os.environ.get("PLANNER_MODEL", "gemini-2.5-flash"),
            image_model=os.environ.get("IMAGE_MODEL", "gemini-2.5-flash-image"),
            render_dpi=int(os.environ.get("RENDER_DPI", "150")),
            gemini_max_attempts=int(os.environ.get("GEMINI_MAX_ATTEMPTS", "4")),
            gemini_retry_base_delay_s=float(os.environ.get("GEMINI_RETRY_BASE_DELAY_S", "0.5")),
            gemini_timeout_ms=int(os.environ.get("GEMINI_TIMEOUT_MS", "240000")),
            pii_use_dlp=os.environ.get("PII_USE_DLP", "0") == "1",
            pii_dlp_leak_check=os.environ.get("PII_DLP_LEAK_CHECK", "1") == "1",
            pii_max_parallel=int(os.environ.get("PII_MAX_PARALLEL", "4")),
        )
