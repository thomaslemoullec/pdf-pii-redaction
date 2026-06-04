"""Gemini client factory + PDF rendering — the two bits of model glue the pipeline needs.

Kept tiny and dependency-lazy so the unit suite imports the package without the
``google-genai`` / ``pypdfium2`` extras installed (they're only needed on the live path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL.Image import Image

    from .config import Settings


def render_pdf(pdf_bytes: bytes, dpi: int) -> list[Image]:
    """Render PDF page bytes to PIL Images via pypdfium2 (BSD, no system deps)."""
    import pypdfium2 as pdfium  # lazy import — only on the live path

    scale = dpi / 72.0  # pypdfium2 takes a scale factor; 72 dpi is the PDF baseline
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        return [pdf[i].render(scale=scale).to_pil() for i in range(len(pdf))]
    finally:
        pdf.close()


def make_client(settings: Settings) -> Any:
    """Construct the production ``google-genai`` client on the Vertex AI backend.

    Pinned to ``settings.gemini_location`` (default an EU region for data residency).
    A per-request timeout (ms) means a wedged Vertex call fails fast and is retried,
    rather than hanging a batch task indefinitely. Lazy import so the module loads in
    environments without the gemini extra.
    """
    from google import genai
    from google.genai import types as genai_types

    return genai.Client(
        vertexai=True,
        project=settings.gcp_project,
        location=settings.gemini_location,
        http_options=genai_types.HttpOptions(timeout=settings.gemini_timeout_ms),
    )
