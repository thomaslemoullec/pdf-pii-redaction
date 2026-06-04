"""Unit tests for the PII scanner + the advisory sensitivity label."""

from __future__ import annotations

import pytest
from PIL import Image

from pdf_anonymiser.pii import (
    PiiFinding,
    PiiLabel,
    PiiType,
    Routing,
    Sensitivity,
    SensitivityPolicy,
    scan_document,
)


def _f(t: PiiType, *, box=None) -> PiiFinding:
    from pdf_anonymiser.pii import _TYPE_SENSITIVITY

    return PiiFinding(type=t, page=0, sensitivity=_TYPE_SENSITIVITY[t], box=box)


def test_no_pii_is_labelled_low_sensitivity() -> None:
    label = PiiLabel.from_findings("d", (), SensitivityPolicy())
    assert not label.contains_pii
    assert label.max_sensitivity == Sensitivity.NONE
    assert label.routing == Routing.OK_FOR_GLOBAL


def test_low_sensitivity_pii_is_ok_for_global() -> None:
    label = PiiLabel.from_findings("d", (_f(PiiType.EMAIL), _f(PiiType.PHONE)), SensitivityPolicy())
    assert label.contains_pii and label.max_sensitivity == Sensitivity.LOW
    assert label.routing == Routing.OK_FOR_GLOBAL


def test_high_sensitivity_without_box_is_in_perimeter() -> None:
    label = PiiLabel.from_findings(
        "d", (_f(PiiType.ID_NUMBER), _f(PiiType.NAME)), SensitivityPolicy()
    )
    assert label.max_sensitivity == Sensitivity.HIGH
    assert label.routing == Routing.IN_PERIMETER_ONLY


def test_high_sensitivity_with_boxes_is_redact_first() -> None:
    # Over-threshold findings that are all box-localised → the "redact first" hint.
    findings = (
        _f(PiiType.ID_NUMBER, box=(0.1, 0.1, 0.4, 0.2)),
        _f(PiiType.NAME, box=(0.1, 0.3, 0.5, 0.4)),
    )
    assert PiiLabel.from_findings("d", findings, SensitivityPolicy()).routing == Routing.REDACT_FIRST


def test_by_type_counts() -> None:
    label = PiiLabel.from_findings(
        "d", (_f(PiiType.NAME), _f(PiiType.NAME), _f(PiiType.IBAN_ACCOUNT)), SensitivityPolicy()
    )
    assert label.by_type == {"name": 2, "iban_account": 1}


def test_low_water_is_configurable() -> None:
    # Raise the low-water mark to MEDIUM → a name (MEDIUM) is now labelled low sensitivity.
    policy = SensitivityPolicy(low_water=Sensitivity.MEDIUM)
    label = PiiLabel.from_findings("d", (_f(PiiType.NAME),), policy)
    assert label.routing == Routing.OK_FOR_GLOBAL


class _FakeDetector:
    def __init__(self, per_page: dict[int, list[PiiFinding]]) -> None:
        self._per_page = per_page

    def scan_page(self, image, page_index):  # type: ignore[no-untyped-def]
        return self._per_page.get(page_index, [])


def test_scan_document_folds_pages() -> None:
    pages = [Image.new("RGB", (10, 10)) for _ in range(3)]
    detector = _FakeDetector({0: [_f(PiiType.NAME)], 2: [_f(PiiType.ID_NUMBER)]})
    label = scan_document("pkg-1", pages, detector)
    assert label.document_id == "pkg-1"
    assert label.by_type == {"name": 1, "id_number": 1}
    assert label.routing == Routing.IN_PERIMETER_ONLY  # ID_NUMBER is HIGH, no box


def test_normalise_box_handles_pixels_normalised_and_bad() -> None:
    from pdf_anonymiser.pii import _normalise_box

    # already-normalised 0..1 → unchanged (clamped)
    assert _normalise_box([0.1, 0.2, 0.5, 0.6], 100, 200) == (0.1, 0.2, 0.5, 0.6)
    # pixel coords (>1) → divided by image dims
    assert _normalise_box([10.0, 20.0, 50.0, 100.0], 100, 200) == (0.1, 0.1, 0.5, 0.5)
    # any coord > 1 → whole box treated as pixels (÷ dims), then clamped to [0,1]
    assert _normalise_box([-5.0, 0.0, 9999.0, 0.5], 100, 200) == (0.0, 0.0, 1.0, 0.0025)
    # wrong length → None
    assert _normalise_box([0.1, 0.2, 0.3], 100, 200) is None


# --- C: DLP detector finding-mapping + the ensemble (provenance) ------------


class _FakeInfoType:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeFinding:
    def __init__(self, name: str) -> None:
        self.info_type = _FakeInfoType(name)


class _FakeResult:
    def __init__(self, names: list[str]) -> None:
        self.findings = [_FakeFinding(n) for n in names]


class _FakeDlpResponse:
    def __init__(self, names: list[str]) -> None:
        self.result = _FakeResult(names)


def test_dlp_findings_map_infotypes_and_tag_provenance() -> None:
    from pdf_anonymiser.pii import _dlp_findings

    resp = _FakeDlpResponse(["PERSON_NAME", "IBAN_CODE", "EMAIL_ADDRESS", "NOT_A_TYPE"])
    findings = _dlp_findings(resp, page_index=2)

    # unknown infoTypes are dropped; the rest map to our vocabulary
    assert [f.type for f in findings] == [PiiType.NAME, PiiType.IBAN_ACCOUNT, PiiType.EMAIL]
    assert all(f.detector == "dlp" for f in findings)
    assert all(f.page == 2 for f in findings)
    # IBAN inherits its HIGH sensitivity from the central type map
    assert findings[1].sensitivity == Sensitivity.HIGH


def test_dlp_detector_calls_client_and_maps(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:  # type: ignore[no-untyped-def]
    # google-cloud-dlp is airlock-blocked locally, so stub the lazy import the
    # response-parsing depends on, and inject a fake client.
    import sys
    import types

    from pdf_anonymiser.pii import DLP_INFO_TYPES, DlpPiiDetector

    fake_module = types.ModuleType("dlp_v2")
    fake_module.Likelihood = types.SimpleNamespace(POSSIBLE=4)  # type: ignore[attr-defined]
    fake_module.ByteContentItem = types.SimpleNamespace(  # type: ignore[attr-defined]
        BytesType=types.SimpleNamespace(IMAGE_PNG=1)
    )
    # `from google.cloud import dlp_v2` imports the parent packages first, so the
    # whole google.cloud chain must resolve (the SDK isn't installed locally).
    google_pkg = sys.modules.get("google") or types.ModuleType("google")
    cloud_pkg = sys.modules.get("google.cloud") or types.ModuleType("google.cloud")
    cloud_pkg.dlp_v2 = fake_module  # type: ignore[attr-defined]
    google_pkg.cloud = cloud_pkg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_pkg)
    monkeypatch.setitem(sys.modules, "google.cloud.dlp_v2", fake_module)

    captured: dict[str, object] = {}

    # DLP only offers a subset here — PERSON_NAME is unavailable in this region.
    available = [n for n in DLP_INFO_TYPES if n != "PERSON_NAME"]

    class _FakeListResp:
        def __init__(self, names):  # type: ignore[no-untyped-def]
            self.info_types = [types.SimpleNamespace(name=n) for n in names]

    class _FakeClient:
        def list_info_types(self, *, request):  # type: ignore[no-untyped-def]
            captured["list_parent"] = request["parent"]
            return _FakeListResp(available)

        def inspect_content(self, *, request):  # type: ignore[no-untyped-def]
            captured["request"] = request
            return _FakeDlpResponse(["PHONE_NUMBER"])

    detector = DlpPiiDetector(settings, client=_FakeClient())
    findings = detector.scan_page(Image.new("RGB", (8, 8), "white"), page_index=0)

    assert [f.type for f in findings] == [PiiType.PHONE]
    assert all(f.detector == "dlp" for f in findings)
    req = captured["request"]
    assert req["parent"] == f"projects/{settings.gcp_project}/locations/global"
    assert req["inspect_config"]["include_quote"] is False  # PII-minimal
    # only the region-available infoTypes are requested (unavailable ones dropped,
    # so one bad/absent name can never 400 the whole request)
    requested = {t["name"] for t in req["inspect_config"]["info_types"]}
    assert "PERSON_NAME" not in requested
    assert requested == set(available)
    assert captured["list_parent"] == "locations/global"


def test_dlp_resolve_info_types_falls_back_when_listing_fails(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    from pdf_anonymiser.pii import DLP_INFO_TYPES, DlpPiiDetector

    class _ListFailsClient:
        def list_info_types(self, *, request):  # type: ignore[no-untyped-def]
            raise RuntimeError("listing unavailable")

    detector = DlpPiiDetector(settings, client=_ListFailsClient())
    # best effort: a failed listing falls back to the full wishlist, never crashes
    assert detector._resolve_info_types() == list(DLP_INFO_TYPES)


def test_dlp_detector_scopes_infotypes_to_requested_pii_types(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    import types

    from pdf_anonymiser.pii import DLP_INFO_TYPES, DlpPiiDetector

    # scope to EMAIL + IBAN only → wishlist holds just their DLP infoTypes
    detector = DlpPiiDetector(
        settings, pii_types=[PiiType.EMAIL, PiiType.IBAN_ACCOUNT]
    )

    class _AllAvailable:
        def list_info_types(self, *, request):  # type: ignore[no-untyped-def]
            return types.SimpleNamespace(
                info_types=[types.SimpleNamespace(name=n) for n in DLP_INFO_TYPES]
            )

    detector._client = _AllAvailable()
    resolved = set(detector._resolve_info_types())
    assert resolved == {"IBAN_CODE", "FINANCIAL_ACCOUNT_NUMBER", "EMAIL_ADDRESS"}
    assert "PERSON_NAME" not in resolved  # NAME wasn't requested


def test_dlp_detector_empty_scope_means_full_wishlist(settings) -> None:  # type: ignore[no-untyped-def]
    # An EMPTY pii_types ([]), like a blank-description plan, must mean "everything"
    # — NOT an empty info_types list (which would break the inspect call).
    from pdf_anonymiser.pii import DLP_INFO_TYPES, DlpPiiDetector

    assert DlpPiiDetector(settings, pii_types=[])._wishlist == DLP_INFO_TYPES
    assert DlpPiiDetector(settings, pii_types=None)._wishlist == DLP_INFO_TYPES


def test_ensemble_unions_findings_and_keeps_each_detectors_provenance() -> None:
    from pdf_anonymiser.pii import EnsemblePiiDetector

    gemini = _FakeDetector({0: [_f(PiiType.NAME), _f(PiiType.SIGNATURE)]})
    dlp = _FakeDetector(
        {
            0: [
                PiiFinding(
                    type=PiiType.NAME, page=0,
                    sensitivity=Sensitivity.MEDIUM, detector="dlp",
                ),
                PiiFinding(
                    type=PiiType.IBAN_ACCOUNT, page=0,
                    sensitivity=Sensitivity.HIGH, detector="dlp",
                ),
            ]
        }
    )
    ensemble = EnsemblePiiDetector([gemini, dlp])

    label = scan_document("pkg-e", [Image.new("RGB", (8, 8))], ensemble)

    # union: 2 from gemini + 2 from dlp
    assert label.by_detector == {"gemini": 2, "dlp": 2}
    assert label.by_type == {"name": 2, "signature": 1, "iban_account": 1}
    # adding DLP's IBAN (HIGH) can only tighten the gate
    assert label.routing == Routing.IN_PERIMETER_ONLY


class _BrokenDetector:
    def scan_page(self, image, page_index):  # type: ignore[no-untyped-def]
        raise RuntimeError("DLP misconfigured")


def test_ensemble_degrades_when_one_detector_fails() -> None:
    from pdf_anonymiser.pii import EnsemblePiiDetector

    gemini = _FakeDetector({0: [_f(PiiType.NAME)]})
    ensemble = EnsemblePiiDetector([gemini, _BrokenDetector()])

    # DLP raising must not sink the review — Gemini's findings still come through.
    label = scan_document("pkg-deg", [Image.new("RGB", (8, 8))], ensemble)
    assert label.by_detector == {"gemini": 1}


def test_ensemble_raises_only_when_all_detectors_fail() -> None:
    from pdf_anonymiser.pii import EnsemblePiiDetector

    ensemble = EnsemblePiiDetector([_BrokenDetector(), _BrokenDetector()])
    with pytest.raises(RuntimeError, match="DLP misconfigured"):
        ensemble.scan_page(Image.new("RGB", (8, 8)), 0)


# --- DLP value findings (for the certified value-carryover leak check) ---------


class _FakeQuoteFinding:
    def __init__(self, name: str, quote: str) -> None:
        self.info_type = _FakeInfoType(name)
        self.quote = quote


class _FakeQuoteResponse:
    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self.result = types_simple([_FakeQuoteFinding(n, q) for n, q in pairs])


def types_simple(findings):  # type: ignore[no-untyped-def]
    import types as _t

    return _t.SimpleNamespace(findings=findings)


def test_dlp_value_findings_maps_quotes_and_drops_empty_or_unknown() -> None:
    from pdf_anonymiser.pii import _dlp_value_findings

    resp = _FakeQuoteResponse(
        [
            ("IBAN_CODE", "DE89 3704 0044"),
            ("PERSON_NAME", "Hans Müller"),
            ("EMAIL_ADDRESS", ""),  # empty quote → dropped
            ("NOT_A_TYPE", "whatever"),  # off-vocabulary → dropped
        ]
    )
    out = _dlp_value_findings(resp)
    assert out == [
        (PiiType.IBAN_ACCOUNT, "DE89 3704 0044"),
        (PiiType.NAME, "Hans Müller"),
    ]


def test_scan_values_requests_quotes_and_returns_pairs(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:  # type: ignore[no-untyped-def]
    import sys
    import types

    from pdf_anonymiser.pii import DlpPiiDetector

    fake_module = types.ModuleType("dlp_v2")
    fake_module.Likelihood = types.SimpleNamespace(POSSIBLE=4)  # type: ignore[attr-defined]
    fake_module.ByteContentItem = types.SimpleNamespace(  # type: ignore[attr-defined]
        BytesType=types.SimpleNamespace(IMAGE_PNG=1)
    )
    google_pkg = sys.modules.get("google") or types.ModuleType("google")
    cloud_pkg = sys.modules.get("google.cloud") or types.ModuleType("google.cloud")
    cloud_pkg.dlp_v2 = fake_module  # type: ignore[attr-defined]
    google_pkg.cloud = cloud_pkg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_pkg)
    monkeypatch.setitem(sys.modules, "google.cloud.dlp_v2", fake_module)

    captured: dict[str, object] = {}

    class _FakeClient:
        def list_info_types(self, *, request):  # type: ignore[no-untyped-def]
            return types.SimpleNamespace(
                info_types=[types.SimpleNamespace(name=n) for n in ("IBAN_CODE",)]
            )

        def inspect_content(self, *, request):  # type: ignore[no-untyped-def]
            captured["request"] = request
            return _FakeQuoteResponse([("IBAN_CODE", "DE89 3704 0044")])

    detector = DlpPiiDetector(settings, client=_FakeClient())
    pairs = detector.scan_values(Image.new("RGB", (8, 8), "white"), page_index=0)

    assert pairs == [(PiiType.IBAN_ACCOUNT, "DE89 3704 0044")]
    # the value-carryover path MUST ask DLP for the quotes (unlike scan_page)
    assert captured["request"]["inspect_config"]["include_quote"] is True
