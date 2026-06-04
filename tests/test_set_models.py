"""Unit tests for scripts/set_models.py — the tfvars model-key rewriter (`make models-write`)."""

from __future__ import annotations

import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "set_models",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "set_models.py",
)
set_models = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(set_models)

rewrite_tfvars = set_models.rewrite_tfvars

_FULL = {
    "gemini_location": "global",
    "vision_model": "gemini-3.5-flash",
    "planner_model": "gemini-3.5-flash",
    "image_model": "gemini-3-pro-image",
}


def test_replaces_existing_keys_in_place_without_duplicating() -> None:
    src = 'gemini_location = "europe-west4"\nvision_model = "old"\n'
    out = rewrite_tfvars(src, _FULL)
    assert 'gemini_location = "global"' in out
    assert 'vision_model = "gemini-3.5-flash"' in out
    assert out.count("gemini_location") == 1  # replaced, not appended
    assert out.count("vision_model") == 1


def test_appends_missing_keys() -> None:
    out = rewrite_tfvars('project_id = "x"\n', _FULL)
    for key, val in _FULL.items():
        assert f'{key} = "{val}"' in out


def test_leaves_unrelated_lines_and_comments_untouched() -> None:
    src = (
        '# header comment\n'
        'project_id  = "my-gcp-project"\n'
        'enable_iap  = true\n'
        'iap_members = ["group:reviewers@x.com"]\n'
    )
    out = rewrite_tfvars(src, _FULL)
    assert "# header comment" in out
    assert 'project_id  = "my-gcp-project"' in out
    assert "enable_iap  = true" in out
    assert 'iap_members = ["group:reviewers@x.com"]' in out


def test_handles_indented_and_spaced_assignments() -> None:
    # an existing assignment with odd spacing is still recognised and replaced
    src = '   vision_model    =    "old"\n'
    out = rewrite_tfvars(src, {"vision_model": "new"})
    assert 'vision_model = "new"' in out
    assert out.count("vision_model") == 1


def test_output_ends_with_single_newline() -> None:
    out = rewrite_tfvars("", {"vision_model": "v"})
    assert out.endswith("\n") and not out.endswith("\n\n")


def test_only_touches_requested_keys() -> None:
    src = 'vision_model = "old"\nimage_model = "keep"\n'
    out = rewrite_tfvars(src, {"vision_model": "new"})
    assert 'image_model = "keep"' in out  # untouched
    assert 'vision_model = "new"' in out
