"""Synthetic anonymisation — regenerate a document with PII swapped for fakes.

An alternative to black-box redaction: instead of masking PII (which destroys
layout and realism), the image model ("Nano Banana", Gemini image) regenerates the
page with every piece of personal data replaced by a DIFFERENT realistic synthetic
value of the same type and format — preserving layout, fonts and labels. The result
is a faithful-looking document that carries **no real PII**, so it can be shared
freely or seed an eval/training corpus.

Distinct from redaction: this is **generation**, not masking — the output is a new
document. Use redaction for audit-grade "prove nothing was added"; use this for
shareable synthetic corpora. Generation can drift, so fidelity + value-replacement
should be verified (re-scan with the PII detector + a compliance spot-check) —
:func:`pii.scan_document` over the output confirms the structure survived.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from .retry import is_transient, retry_call


class _EmptyImageResponse(RuntimeError):
    """The image model returned no image part (empty/safety-blocked response). Often
    transient on dense pages, so it's treated as retryable."""

if TYPE_CHECKING:
    from PIL.Image import Image

    from .config import Settings


class DocumentAnonymizer(Protocol):
    """Regenerate a page image with its PII replaced by synthetic equivalents.

    ``hint`` grounds the editor in what the scanner found (so it catches every
    occurrence); ``feedback`` carries the judge's correction from a failed attempt.
    """

    def anonymize_page(
        self, image: Image, *, hint: str | None = None, feedback: str | None = None
    ) -> Image: ...


class GeminiImageAnonymizer:
    """A :class:`DocumentAnonymizer` backed by the Gemini image model (Nano Banana).

    Injectable client mirrors the other Gemini wrappers, so it is unit-testable
    without network. Retries transient failures with backoff. The model id comes
    from ``settings.image_model`` (a config flip swaps Nano Banana ↔ Nano Banana
    Pro ↔ a future editor).
    """

    def __init__(
        self, settings: Settings, *, client: Any = None, prompt_version: str = "anonymize_v3"
    ) -> None:
        self._settings = settings
        self._client = client
        self._prompt_version = prompt_version

    def anonymize_page(  # pragma: no cover - live image-gen glue
        self, image: Image, *, hint: str | None = None, feedback: str | None = None
    ) -> Image:
        import io

        from google.genai import types as genai_types
        from PIL import Image as PILImage

        from .prompts import anonymize_prompt

        client = self._client
        if client is None:
            from .gemini_client import make_client

            client = make_client(self._settings)

        instruction = anonymize_prompt(self._prompt_version)
        if hint:
            instruction += f"\n\nThe page contains these PII items to replace: {hint}."
        if feedback:
            instruction += f"\n\nYour previous attempt was INSUFFICIENT — fix it: {feedback}"

        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        page_part = genai_types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png")

        def _call() -> bytes:
            resp = client.models.generate_content(
                model=self._settings.image_model,
                contents=[instruction, page_part],
                config=genai_types.GenerateContentConfig(response_modalities=["IMAGE"]),
            )
            # Defensive parse: candidates / content / parts can each be None when the
            # model returns an empty or safety-blocked response (common on dense pages) —
            # never iterate a None. If no image part is found, raise a RETRYABLE error.
            for cand in getattr(resp, "candidates", None) or []:
                for part in getattr(getattr(cand, "content", None), "parts", None) or []:
                    inline = getattr(part, "inline_data", None)
                    if inline is not None and inline.data:
                        return inline.data
            raise _EmptyImageResponse("image model returned no image part")

        data = retry_call(
            _call,
            attempts=self._settings.gemini_max_attempts,
            base_delay=self._settings.gemini_retry_base_delay_s,
            retryable=lambda e: is_transient(e) or isinstance(e, _EmptyImageResponse),
        )
        return PILImage.open(io.BytesIO(data)).convert("RGB")
