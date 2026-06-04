"""pdf_anonymiser — synthetic PII anonymisation for scanned PDFs.

Point it at a folder of PDFs; it detects the personal data on every page (Gemini
vision ∪ Cloud DLP), regenerates each page with the PII swapped for realistic
synthetic values (Nano Banana), scores the result (PII removed × layout preserved),
and routes documents to a human review queue. GCS-only storage, no database.
"""

from __future__ import annotations

__version__ = "0.1.0"
