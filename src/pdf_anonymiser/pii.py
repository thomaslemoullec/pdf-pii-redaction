"""PII scanning — detect the personal data on a page, never its values.

This module is the detection front of the anonymiser:

- :class:`PiiDetector` reads a page image and reports the **types** of PII present
  — their kind, sensitivity, and (best-effort) location — **never the values**
  (PII-minimal: a leak in the report would defeat the purpose). Production backends
  are Gemini-vision and Cloud DLP, unioned by :class:`EnsemblePiiDetector`.
- :class:`PiiLabel` folds a document's findings into one verdict: ``contains_pii``,
  ``max_sensitivity``, a per-type/per-detector breakdown, and an **advisory
  ``routing`` label** (``SensitivityPolicy``) surfaced to the human reviewer as a
  sensitivity hint. It is a label, *not* an enforced egress gate.

Heuristic-free by necessity: names/addresses on a scanned page aren't regex-able,
so detection is vision-based (Gemini) plus DLP's certified infoType detectors.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Any, Protocol

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from PIL.Image import Image

    from .config import Settings


class PiiType(StrEnum):
    """The PII categories the scanner reports (the model's controlled vocabulary)."""

    NAME = "name"
    DATE_OF_BIRTH = "date_of_birth"
    ID_NUMBER = "id_number"  # passport / national id / document number
    IBAN_ACCOUNT = "iban_account"
    ADDRESS = "address"
    PHONE = "phone"
    EMAIL = "email"
    SIGNATURE = "signature"
    OTHER = "other"


class Sensitivity(IntEnum):
    """Ordered so policies can compare (NONE < LOW < MEDIUM < HIGH)."""

    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class Routing(StrEnum):
    """An advisory sensitivity label for the document (shown to the reviewer).

    A hint about how cautiously the document should be handled — not an enforced
    gate. The anonymiser's job is to remove the PII regardless; this just flags how
    sensitive the original is.
    """

    OK_FOR_GLOBAL = "ok_for_global"  # only low-sensitivity PII present
    REDACT_FIRST = "redact_first"  # sensitive, but localised to known regions
    IN_PERIMETER_ONLY = "in_perimeter_only"  # high-sensitivity PII present


# Default sensitivity per type — a reasonable default risk view; tunable via config later.
_TYPE_SENSITIVITY: dict[PiiType, Sensitivity] = {
    PiiType.NAME: Sensitivity.MEDIUM,
    PiiType.DATE_OF_BIRTH: Sensitivity.HIGH,
    PiiType.ID_NUMBER: Sensitivity.HIGH,
    PiiType.IBAN_ACCOUNT: Sensitivity.HIGH,
    PiiType.ADDRESS: Sensitivity.MEDIUM,
    PiiType.PHONE: Sensitivity.LOW,
    PiiType.EMAIL: Sensitivity.LOW,
    PiiType.SIGNATURE: Sensitivity.MEDIUM,
    PiiType.OTHER: Sensitivity.LOW,
}


@dataclass(frozen=True)
class PiiFinding:
    """One PII occurrence — its kind, where, how sensitive. NEVER its value."""

    type: PiiType
    page: int
    sensitivity: Sensitivity
    note: str = ""  # a non-identifying hint ("printed", "handwritten"), never the value
    # Optional normalised bounding box [x0, y0, x1, y1] in 0..1 — enables redaction.
    box: tuple[float, float, float, float] | None = None
    # Provenance — which detector spotted this: "gemini" | "dlp" | "gemini+dlp".
    detector: str = "gemini"


@dataclass(frozen=True)
class SensitivityPolicy:
    """Folds a document's findings into a max-sensitivity + an advisory routing label.

    Pure and deterministic: given the findings it returns the highest sensitivity
    seen and a coarse handling hint for the reviewer. ``low_water`` is the sensitivity
    at/below which a document is labelled ``OK_FOR_GLOBAL``.
    """

    low_water: Sensitivity = Sensitivity.LOW

    def routing(self, findings: tuple[PiiFinding, ...]) -> tuple[Sensitivity, Routing]:
        if not findings:
            return Sensitivity.NONE, Routing.OK_FOR_GLOBAL
        max_sensitivity = max(f.sensitivity for f in findings)
        if max_sensitivity <= self.low_water:
            return max_sensitivity, Routing.OK_FOR_GLOBAL
        # A localised, box-bounded set of over-threshold findings is "redact first";
        # anything unlocalised is the more cautious "in perimeter only".
        over = [f for f in findings if f.sensitivity > self.low_water]
        if over and all(f.box is not None for f in over):
            return max_sensitivity, Routing.REDACT_FIRST
        return max_sensitivity, Routing.IN_PERIMETER_ONLY


@dataclass(frozen=True)
class PiiLabel:
    """The per-document PII verdict: what was found, how sensitive, an advisory label."""

    document_id: str
    findings: tuple[PiiFinding, ...]
    contains_pii: bool
    max_sensitivity: Sensitivity
    routing: Routing
    by_type: dict[str, int]
    by_detector: dict[str, int]  # provenance counts — which detector found how many

    @classmethod
    def from_findings(
        cls, document_id: str, findings: tuple[PiiFinding, ...], policy: SensitivityPolicy
    ) -> PiiLabel:
        max_sensitivity, routing = policy.routing(findings)
        by_type: dict[str, int] = {}
        by_detector: dict[str, int] = {}
        for f in findings:
            by_type[f.type.value] = by_type.get(f.type.value, 0) + 1
            by_detector[f.detector] = by_detector.get(f.detector, 0) + 1
        return cls(
            document_id=document_id,
            findings=findings,
            contains_pii=bool(findings),
            max_sensitivity=max_sensitivity,
            routing=routing,
            by_type=by_type,
            by_detector=by_detector,
        )


class PiiDetector(Protocol):
    """Read one page image, report the PII types present (never the values)."""

    def scan_page(self, image: Image, page_index: int) -> list[PiiFinding]: ...


def scan_document(
    document_id: str,
    pages: list[Image],
    detector: PiiDetector,
    *,
    policy: SensitivityPolicy | None = None,
) -> PiiLabel:
    """Scan every page and fold the findings into one per-document :class:`PiiLabel`."""
    resolved = policy or SensitivityPolicy()
    findings: list[PiiFinding] = []
    for index, image in enumerate(pages):
        findings.extend(detector.scan_page(image, index))
    return PiiLabel.from_findings(document_id, tuple(findings), resolved)


# --- Gemini-vision detector (a production backend; DLP is the other) --------


class GeminiPiiDetector:
    """A :class:`PiiDetector` backed by Gemini-vision (structured, PII-minimal).

    The prompt asks for PII **types + location, never values**, so the report can't
    itself leak. Injectable client mirrors the splitter/curation models, so it is
    unit-testable without network. Cloud DLP is a drop-in alternative backend.
    """

    def __init__(
        self, settings: Settings, *, client: Any = None, prompt_version: str = "pii_v1"
    ) -> None:
        self._settings = settings
        self._client = client
        self._prompt_version = prompt_version

    def scan_page(self, image: Image, page_index: int) -> list[PiiFinding]:  # pragma: no cover
        from google.genai import types as genai_types
        from pydantic import BaseModel, Field

        from .prompts import pii_prompt

        class _Item(BaseModel):
            model_config = {"frozen": True}

            type: str
            box: list[float] = Field(default_factory=list)  # [x0,y0,x1,y1] 0..1, optional

        class _Response(BaseModel):
            model_config = {"frozen": True}

            pii: list[_Item] = Field(default_factory=list)

        client = self._client
        if client is None:
            from .gemini_client import make_client

            client = make_client(self._settings)
        # The grounded Pro model, not the cheap proposer: PII scanning is a
        # safety-critical, once-per-document check, and the lite model hallucinates
        # PII on blank pages (it enumerates the schema). temperature 0 for stability.
        # Retry transient 5xx/504 (DEADLINE_EXCEEDED) the same way the image leg does —
        # the scan is once per document, so a wedged Vertex call must not error the doc.
        from .retry import retry_call

        response = retry_call(
            lambda: client.models.generate_content(
                model=self._settings.vision_model,
                contents=[pii_prompt(self._prompt_version), image],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_Response,
                    temperature=0.0,
                ),
            ),
            attempts=self._settings.gemini_max_attempts,
            base_delay=self._settings.gemini_retry_base_delay_s,
        )
        parsed = getattr(response, "parsed", None)
        items = parsed.pii if isinstance(parsed, _Response) else []
        valid = {t.value for t in PiiType}
        w, h = image.width, image.height
        findings: list[PiiFinding] = []
        for item in items:
            if item.type not in valid:
                continue
            ptype = PiiType(item.type)
            findings.append(
                PiiFinding(
                    type=ptype, page=page_index,
                    sensitivity=_TYPE_SENSITIVITY[ptype],
                    box=_normalise_box(item.box, w, h),
                )
            )
        return findings


def _normalise_box(
    raw: list[float], width: int, height: int
) -> tuple[float, float, float, float] | None:
    """Best-effort normalise a model box to [x0,y0,x1,y1] in 0..1 (boxes are advisory).

    Vision models return boxes inconsistently (pixels or 0..1000); if any value
    exceeds 1 we treat it as pixels and divide by the image dimensions, then clamp.
    """
    if len(raw) != 4:
        return None
    x0, y0, x1, y1 = raw
    if max(raw) > 1.0:
        x0, x1 = x0 / width, x1 / width
        y0, y1 = y0 / height, y1 / height

    def _clamp(v: float) -> float:
        return max(0.0, min(1.0, v))

    return (_clamp(x0), _clamp(y0), _clamp(x1), _clamp(y1))


# --- Cloud DLP detector + the ensemble (certified + contextual) --

# Map Cloud DLP infoTypes → our PII vocabulary. DLP's certified, deterministic
# detectors (checksums, dictionaries) complement Gemini's contextual vision.
_DLP_INFOTYPE_MAP: dict[str, PiiType] = {
    "PERSON_NAME": PiiType.NAME,
    "DATE_OF_BIRTH": PiiType.DATE_OF_BIRTH,
    "IBAN_CODE": PiiType.IBAN_ACCOUNT,  # checksum-validated; covers German IBANs too
    "FINANCIAL_ACCOUNT_NUMBER": PiiType.IBAN_ACCOUNT,
    "STREET_ADDRESS": PiiType.ADDRESS,
    "PHONE_NUMBER": PiiType.PHONE,
    "EMAIL_ADDRESS": PiiType.EMAIL,
    "PASSPORT": PiiType.ID_NUMBER,
    "GERMANY_PASSPORT": PiiType.ID_NUMBER,
    "GERMANY_IDENTITY_CARD_NUMBER": PiiType.ID_NUMBER,
    "FRANCE_NIR": PiiType.ID_NUMBER,
    "UK_PASSPORT": PiiType.ID_NUMBER,
}

# The infoTypes we ask DLP to look for (the keys above).
DLP_INFO_TYPES: tuple[str, ...] = tuple(dict.fromkeys(_DLP_INFOTYPE_MAP))


class DlpPiiDetector:
    """A :class:`PiiDetector` backed by **Cloud DLP** (certified infoType detectors).

    DLP OCRs the page image and matches its certified detectors (with validators
    like the IBAN checksum), so it is precise and deterministic where Gemini is
    contextual. ``include_quote`` is OFF — DLP returns types + locations, never the
    value (PII-minimal). Findings are tagged ``detector="dlp"`` for provenance.
    Lazy import + injectable client so the unit suite needs neither the package nor
    the API.

    ``pii_types`` optionally **scopes** the detector: pass the PII types you expect in
    a document set (e.g. from a pre-scan), and DLP only runs those infoTypes. Fewer
    detectors = less noise, lower cost, faster — the "dynamically configure DLP per
    job" lever. ``None`` (default) requests the full wishlist.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        location: str = "global",
        pii_types: Iterable[PiiType] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._location = location
        # The wishlist, optionally narrowed to the requested PII types (map ours→DLP).
        # An EMPTY scope means the same as None ("scan for everything") — never an
        # empty info_types list, which would break the inspect call.
        wanted = set(pii_types) if pii_types else None
        self._wishlist: tuple[str, ...] = tuple(
            n for n, t in _DLP_INFOTYPE_MAP.items() if wanted is None or t in wanted
        )
        self._info_types: list[str] | None = None  # resolved + cached on first scan
        self._resolve_lock = threading.Lock()  # the cache is read across worker threads

    def _client_or_default(self) -> Any:
        if self._client is not None:
            return self._client
        from google.cloud import dlp_v2

        return dlp_v2.DlpServiceClient()

    def _resolve_info_types(self) -> list[str]:
        """Our (possibly scoped) wishlist ∩ the infoTypes DLP supports in this location.

        Not every built-in detector exists in every region, and one unavailable
        name fails the WHOLE inspect request (400). So we ask DLP what it offers
        here and request only the intersection — an unavailable detector is simply
        skipped, never fatal. Resolved once and cached (under a lock, since the
        re-scan detector is shared across the per-page worker threads — otherwise
        they'd all race to fire the listing call). If listing fails, fall back to the
        (scoped) wishlist (best effort over hard fail).
        """
        if self._info_types is not None:
            return self._info_types
        with self._resolve_lock:
            if self._info_types is not None:  # another thread resolved while we waited
                return self._info_types
            try:
                response = self._client_or_default().list_info_types(
                    request={"parent": f"locations/{self._location}"}
                )
                available = {t.name for t in response.info_types}
                resolved = [n for n in self._wishlist if n in available]
            except Exception:
                resolved = list(self._wishlist)
            self._info_types = resolved or list(self._wishlist)
            return self._info_types

    def scan_page(  # pragma: no cover - live DLP glue
        self, image: Image, page_index: int
    ) -> list[PiiFinding]:
        import io

        from google.cloud import dlp_v2

        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        parent = f"projects/{self._settings.gcp_project}/locations/{self._location}"
        request = {
            "parent": parent,
            "inspect_config": {
                "info_types": [{"name": n} for n in self._resolve_info_types()],
                "include_quote": False,  # PII-minimal — no values returned
                "min_likelihood": dlp_v2.Likelihood.POSSIBLE,
            },
            "item": {
                "byte_item": {
                    "type_": dlp_v2.ByteContentItem.BytesType.IMAGE_PNG,
                    "data": buffer.getvalue(),
                }
            },
        }
        response = self._client_or_default().inspect_content(request=request)
        return _dlp_findings(response, page_index)

    def scan_values(  # pragma: no cover - live DLP glue
        self, image: Image, page_index: int
    ) -> list[tuple[PiiType, str]]:
        """Return certified findings as ``(type, value)`` pairs — for the leak check.

        Identical to :meth:`scan_page` but with ``include_quote=True``, so the matched
        REAL values come back. These power the value-carryover leak check (does a source
        value survive into the synthetic output?) and are used **in-memory only** — never
        persisted or logged, so the stored report stays PII-minimal. Kept separate from
        ``scan_page`` precisely so the quote-bearing path is explicit and opt-in.
        """
        import io

        from google.cloud import dlp_v2

        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        parent = f"projects/{self._settings.gcp_project}/locations/{self._location}"
        request = {
            "parent": parent,
            "inspect_config": {
                "info_types": [{"name": n} for n in self._resolve_info_types()],
                "include_quote": True,  # values are needed for the carryover comparison
                # LIKELY (vs POSSIBLE on the input scan): the carryover is a hard fail
                # gate, so require higher-confidence matches to avoid false leaks.
                "min_likelihood": dlp_v2.Likelihood.LIKELY,
            },
            "item": {
                "byte_item": {
                    "type_": dlp_v2.ByteContentItem.BytesType.IMAGE_PNG,
                    "data": buffer.getvalue(),
                }
            },
        }
        response = self._client_or_default().inspect_content(request=request)
        return _dlp_value_findings(response)


def _dlp_findings(response: Any, page_index: int) -> list[PiiFinding]:
    """Map a DLP inspect response → our findings (separated for unit testing)."""
    findings: list[PiiFinding] = []
    for f in getattr(response.result, "findings", []):
        name = f.info_type.name
        ptype = _DLP_INFOTYPE_MAP.get(name)
        if ptype is None:
            continue
        findings.append(
            PiiFinding(type=ptype, page=page_index, sensitivity=_TYPE_SENSITIVITY[ptype],
                       detector="dlp")
        )
    return findings


def _dlp_value_findings(response: Any) -> list[tuple[PiiType, str]]:
    """Map a DLP inspect response (``include_quote=True``) → ``[(type, value), …]``.

    Drops findings whose infoType isn't in our vocabulary or whose quote is empty
    (e.g. quotes suppressed). Pure → unit-tested with a fake response, no API.
    """
    out: list[tuple[PiiType, str]] = []
    for f in getattr(response.result, "findings", []):
        ptype = _DLP_INFOTYPE_MAP.get(f.info_type.name)
        quote = (getattr(f, "quote", "") or "").strip()
        if ptype is not None and quote:
            out.append((ptype, quote))
    return out


class EnsemblePiiDetector:
    """Run several detectors and UNION their findings, keeping each one's provenance.

    The differentiator: Gemini's findings stay tagged ``"gemini"``, DLP's ``"dlp"``,
    so :attr:`PiiLabel.by_detector` shows what each spotted. Routing uses the max
    sensitivity across the union (so adding a detector can only tighten the gate,
    never loosen it). Cross-detector value-matching to a single ``"gemini+dlp"`` tag
    is intentionally avoided — without comparing values it isn't reliable.

    Resilience: a single detector raising must not sink the whole review. If one
    backend (say DLP, mid-config) errors, we degrade to the others; only a *total*
    failure (every detector raised) propagates.
    """

    def __init__(self, detectors: list[PiiDetector]) -> None:
        self._detectors = detectors

    def scan_page(self, image: Image, page_index: int) -> list[PiiFinding]:
        findings: list[PiiFinding] = []
        last_error: Exception | None = None
        survived = 0
        for detector in self._detectors:
            try:
                findings.extend(detector.scan_page(image, page_index))
                survived += 1
            except Exception as exc:
                last_error = exc
                _log.warning(
                    "PII detector %s failed on page %d, continuing without it: %s",
                    type(detector).__name__, page_index, exc,
                )
        if survived == 0 and last_error is not None:
            raise last_error
        return findings
