"""LLM-as-judge for synthetic anonymisation — verify, then let a human review.

Synthetic anonymisation is *generation*, so it can drift: a real value might survive,
PII might be left in place, or non-personal content might be altered. This judge
compares the **source** page against the **anonymised** page and returns a
structured verdict on three axes — **leakage** (did real PII survive?),
**completeness** (was all PII replaced?), **fidelity** (is the rest unchanged?) —
plus a rationale. The verdict is advisory: it surfaces to a human reviewer in the
UI, never auto-gates. Its own output is PII-minimal — it flags *that* a value
leaked and its type, never the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .redaction_metrics import RedactionMetrics
from .retry import retry_call

# DLP infoTypes that are noisy on a *synthetic* output: the page is regenerated full of
# fake names, and DLP's name detector collides with common name tokens, so a "name"/"other"
# carryover is unreliable as a hard leak. These DOWNGRADE to a human review instead of a
# hard fail — the deterministic value-match and certified (checksum-validated) types still
# fail. See the corroboration logic in redaction_metrics.
_SOFT_DLP_TYPES = frozenset({"name", "other"})

if TYPE_CHECKING:
    from PIL.Image import Image

    from .config import Settings


@dataclass(frozen=True)
class RedactionJudgement:
    """The judge's verdict comparing source vs anonymised (advisory; human reviews)."""

    leaked: bool  # any real personal value from the source survived in the output
    leaked_types: tuple[str, ...]
    all_pii_removed: bool  # every PII occurrence in the source was replaced
    layout_preserved: bool  # non-personal content / layout unchanged
    rationale: str

    @property
    def verdict(self) -> str:
        """``fail`` on a leak, ``review`` on incompleteness/drift, else ``pass``."""
        if self.leaked:
            return "fail"
        if not self.all_pii_removed or not self.layout_preserved:
            return "review"
        return "pass"


class RedactionJudge(Protocol):
    """Compare a source page to its anonymised version and judge the result."""

    def judge(self, source: Image, anonymized: Image) -> RedactionJudgement: ...


class GeminiRedactionJudge:
    """A :class:`RedactionJudge` backed by Gemini-vision (both images, one call)."""

    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        self._settings = settings
        self._client = client

    def judge(  # pragma: no cover - live vision glue over the tested verdict logic
        self, source: Image, anonymized: Image
    ) -> RedactionJudgement:
        from google.genai import types as genai_types
        from pydantic import BaseModel, Field

        class _Verdict(BaseModel):
            model_config = {"frozen": True}

            leaked: bool = Field(description="a real value from image 1 still appears in image 2")
            leaked_types: list[str] = Field(default_factory=list)
            all_pii_removed: bool = True
            layout_preserved: bool = True
            rationale: str = ""

        client = self._client
        if client is None:
            from .gemini_client import make_client

            client = make_client(self._settings)

        prompt = (
            "Image 1 is the ORIGINAL document; Image 2 is its ANONYMISED version. "
            "Judge the anonymisation: (1) leaked — does ANY real personal value from "
            "Image 1 (name, date of birth, id/account number, address, phone, email) "
            "still appear in Image 2? (2) all_pii_removed — was every piece of personal "
            "data in Image 1 replaced in Image 2? (3) layout_preserved — is all "
            "non-personal content, layout, and labelling unchanged? Give a short "
            "rationale. Do NOT quote any personal value in the rationale — refer to it "
            "by type only. Return strictly the JSON schema."
        )

        def _call() -> Any:
            return client.models.generate_content(
                model=self._settings.vision_model,  # the grounded Pro vision model
                contents=[prompt, source, anonymized],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_Verdict,
                    temperature=0.0,
                ),
            )

        response = retry_call(
            _call,
            attempts=self._settings.gemini_max_attempts,
            base_delay=self._settings.gemini_retry_base_delay_s,
        )
        parsed = getattr(response, "parsed", None)
        v = parsed if isinstance(parsed, _Verdict) else _Verdict(leaked=False)
        return RedactionJudgement(
            leaked=v.leaked,
            leaked_types=tuple(v.leaked_types),
            all_pii_removed=v.all_pii_removed,
            layout_preserved=v.layout_preserved,
            rationale=v.rationale,
        )


# --- Metrics + explanation combined: the report the UI shows --------------


@dataclass(frozen=True)
class RedactionReport:
    """The deterministic metrics PLUS the LLM's explanation.

    ``dlp_leaks`` are PII types Cloud DLP still detected in the *anonymised* output
    — a certified, deterministic leak signal that backs up the LLM judge. Any DLP
    leak forces a ``fail`` (a checksum-validated IBAN surviving is not debatable).
    """

    metrics: RedactionMetrics
    rationale: str  # the LLM's human explanation (semantic, catches what tokens miss)
    dlp_leaks: tuple[str, ...] = ()  # PII types Cloud DLP still detected in the output
    # The Gemini judge's own structured assessment (vision), alongside the metrics.
    judge_all_removed: bool = True  # judge: every piece of PII was replaced
    judge_layout_ok: bool = True  # judge: non-personal content + layout unchanged
    judge_leaked: bool = False  # judge: a real value still appears in the output
    attempts: int = 1  # how many anonymise→judge rounds this page took (cost driver)

    @property
    def certified_dlp_leaks(self) -> tuple[str, ...]:
        """DLP carryovers that are authoritative (checksum/structured types) → hard fail."""
        return tuple(t for t in self.dlp_leaks if t not in _SOFT_DLP_TYPES)

    @property
    def soft_dlp_leaks(self) -> tuple[str, ...]:
        """DLP carryovers on noisy types (name/other) → route to a human, don't fail."""
        return tuple(t for t in self.dlp_leaks if t in _SOFT_DLP_TYPES)

    @property
    def verdict(self) -> str:
        """One AI verdict that reconciles all three signals — metric, DLP, and judge.

        - **fail** on a *leak*, owned by the deterministic signals: the metric's exact
          value-match (`pii_leaked`) or a **certified** DLP carryover (checksum/structured
          types). The LLM judge's own "leaked" flag also counts.
        - **review** when nothing certainly leaked but a signal *doubts* the result —
          fidelity dipped, the judge says not everything was replaced / the layout drifted,
          OR a **soft** DLP carryover (a `name`/`other` hit, which false-positives on the
          synthetic output's fake names). A human confirms instead of an auto-fail.
        - **pass** only when every signal agrees it's clean.
        """
        if self.certified_dlp_leaks or self.metrics.pii_leaked > 0 or self.judge_leaked:
            return "fail"
        if (self.soft_dlp_leaks or self.metrics.fidelity < self.metrics.fidelity_threshold
                or not self.judge_all_removed or not self.judge_layout_ok):
            return "review"
        return "pass"


class RedactionAnalyzer(Protocol):
    """Vision tools that feed the deterministic metrics (source PII + transcriptions)."""

    def analyze_source(self, image: Image) -> tuple[str, list[tuple[str, str]]]: ...
    def transcribe(self, image: Image) -> str: ...


def build_report(
    metrics: RedactionMetrics, judgement: RedactionJudgement, dlp_leaks: tuple[str, ...] = ()
) -> RedactionReport:
    """Assemble a :class:`RedactionReport` from the three signals — the single source of
    truth for how metrics + the DLP leak check + the LLM judge combine into one report
    (used by the redaction agent)."""
    return RedactionReport(
        metrics=metrics,
        rationale=judgement.rationale,
        dlp_leaks=dlp_leaks,
        judge_all_removed=judgement.all_pii_removed,
        judge_layout_ok=judgement.layout_preserved,
        judge_leaked=judgement.leaked,
    )


class GeminiRedactionAnalyzer:
    """Vision analyzer: extracts the source's PII values + transcribes pages.

    The extracted values are used LOCALLY for measurement only — never persisted or
    egressed (that would defeat PII-minimality). One structured call gets the source
    transcription + its PII values; a plain call transcribes the output.
    """

    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        self._settings = settings
        self._client = client

    def _client_or_default(self) -> Any:
        if self._client is not None:
            return self._client
        from .gemini_client import make_client

        return make_client(self._settings)

    def analyze_source(  # pragma: no cover - live vision glue
        self, image: Image
    ) -> tuple[str, list[tuple[str, str]]]:
        from google.genai import types as genai_types
        from pydantic import BaseModel, Field

        class _Pii(BaseModel):
            model_config = {"frozen": True}

            type: str
            value: str

        class _Source(BaseModel):
            model_config = {"frozen": True}

            transcription: str = ""
            pii: list[_Pii] = Field(default_factory=list)

        prompt = (
            "Transcribe ALL visible text on this document verbatim into 'transcription'. "
            "Then list every piece of personal data in 'pii' as {type, value} — the "
            "EXACT value as written (this is for an internal redaction check). Types: "
            "name, date_of_birth, id_number, iban_account, address, phone, email."
        )
        response = retry_call(
            lambda: self._client_or_default().models.generate_content(
                model=self._settings.vision_model,
                contents=[prompt, image],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json", response_schema=_Source,
                    temperature=0.0,
                ),
            ),
            attempts=self._settings.gemini_max_attempts,
            base_delay=self._settings.gemini_retry_base_delay_s,
        )
        parsed = getattr(response, "parsed", None)
        if not isinstance(parsed, _Source):
            return "", []
        return parsed.transcription, [(p.type, p.value) for p in parsed.pii]

    def transcribe(self, image: Image) -> str:  # pragma: no cover - live vision glue
        from google.genai import types as genai_types

        response = retry_call(
            lambda: self._client_or_default().models.generate_content(
                model=self._settings.vision_model,
                contents=["Transcribe ALL visible text on this document verbatim.", image],
                config=genai_types.GenerateContentConfig(temperature=0.0),
            ),
            attempts=self._settings.gemini_max_attempts,
            base_delay=self._settings.gemini_retry_base_delay_s,
        )
        return (response.text or "").strip()
