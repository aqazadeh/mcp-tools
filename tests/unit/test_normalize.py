from typing import Any

import pytest

from mcp_tools.core.catalog.loader import load_catalog
from mcp_tools.core.catalog.normalize import normalize_request
from mcp_tools.core.catalog.pricing import quote
from mcp_tools.core.errors import ApiError
from mcp_tools.core.ids import new_id

CATALOG = load_catalog()
IMAGE = CATALOG.get("mock_image")
VIDEO = CATALOG.get("mock_video")
AUDIO = CATALOG.get("mock_audio")
UPSCALE = CATALOG.get("mock_upscale")
MED = new_id("med", 1)
JOB = new_id("job", 1)


def _error(model: Any, raw: dict[str, Any]) -> ApiError:
    with pytest.raises(ApiError) as excinfo:
        normalize_request(model, raw)
    return excinfo.value


def test_defaults_are_applied_without_adjustments() -> None:
    request = normalize_request(IMAGE, {"model": "mock_image", "prompt": "  a fox  "})
    assert request.prompt == "a fox" and request.aspect_ratio == "1:1" and request.count == 1
    assert request.params == {"resolution": "1k", "style": "gradient", "simulate": "success"}
    assert request.adjustments == [] and request.medias == [] and request.duration is None


def test_soft_problems_become_adjustments() -> None:
    request = normalize_request(
        VIDEO,
        {"prompt": "x", "aspect_ratio": "2:1", "count": 9, "duration": 30, "resolution": "720P"},
    )
    assert (request.aspect_ratio, request.count, request.duration) == ("16:9", 4, 10.0)
    assert request.params["resolution"] == "720p"
    assert {a["field"] for a in request.adjustments} == {"aspect_ratio", "count", "duration", "resolution"}
    assert all({"field", "from", "to", "reason"} <= set(a) for a in request.adjustments)

    numeric_strings = normalize_request(VIDEO, {"prompt": "x", "duration": "3", "count": "2"})
    assert numeric_strings.duration == 3.0 and numeric_strings.count == 2

    ignored = normalize_request(AUDIO, {"prompt": "x", "aspect_ratio": "16:9"})
    assert ignored.aspect_ratio is None and ignored.adjustments[0]["field"] == "aspect_ratio"
    assert normalize_request(IMAGE, {"prompt": "x", "duration": 5}).adjustments[0]["field"] == "duration"


def test_hard_problems_are_errors_that_name_the_allowed_values() -> None:
    unknown = _error(IMAGE, {"prompt": "x", "stile": "noise"})
    assert unknown.code == "invalid_params" and "stile" in unknown.message
    assert "style" in (unknown.allowed or []) and unknown.recovery_tool == "models_explore"

    bad_option = _error(IMAGE, {"prompt": "x", "style": "neon"})
    assert bad_option.allowed == ["gradient", "checker", "noise"]

    assert "requires a prompt" in _error(IMAGE, {}).message
    assert "whole number" in _error(IMAGE, {"prompt": "x", "count": "many"}).message
    assert "must be a number" in _error(VIDEO, {"prompt": "x", "duration": "long"}).message
    assert "not valid" in _error(IMAGE, {"prompt": "x", "aspect_ratio": "wide"}).message
    assert "must be a string" in _error(IMAGE, {"prompt": 7}).message
    assert "must be a number" in _error(IMAGE, {"prompt": "x", "seed": True}).message


def test_media_references_must_be_ids_with_valid_roles() -> None:
    ok = normalize_request(IMAGE, {"prompt": "x", "medias": [{"value": MED}, {"role": "image", "value": JOB}]})
    assert [(m.role, m.value) for m in ok.medias] == [("image", MED), ("image", JOB)]
    assert ok.adjustments[0]["reason"] == "the model has a single media role"

    url = _error(IMAGE, {"prompt": "x", "medias": [{"role": "image", "value": "https://x.test/a.png"}]})
    assert url.recovery_tool == "media_import_url" and "media_import_url" in (url.hint or "")
    assert _error(IMAGE, {"prompt": "x", "medias": [{"value": "not-an-id"}]}).recovery_tool == "media_list"
    assert "not a role" in _error(IMAGE, {"prompt": "x", "medias": [{"role": "mask", "value": MED}]}).message
    assert "at most 4" in _error(IMAGE, {"prompt": "x", "medias": [{"value": MED}] * 5}).message
    assert "does not accept reference media" in _error(AUDIO, {"prompt": "x", "medias": [{"value": MED}]}).message
    assert "requires a media" in _error(UPSCALE, {}).message
    assert "must be a list" in _error(IMAGE, {"prompt": "x", "medias": "nope"}).message


def test_prompt_free_models_and_folder_ids() -> None:
    request = normalize_request(UPSCALE, {"prompt": "ignored", "scale": 3, "medias": [{"value": MED}]})
    assert request.prompt is None and request.params["scale"] == 2  # nearest allowed option
    assert {a["field"] for a in request.adjustments} >= {"prompt", "scale"}
    folder = new_id("fld", 1)
    assert normalize_request(IMAGE, {"prompt": "x", "folder_id": folder}).folder_id == folder
    assert "folder id" in _error(IMAGE, {"prompt": "x", "folder_id": "my-folder"}).message


def test_canonical_form_is_stable_for_idempotency() -> None:
    a = normalize_request(IMAGE, {"prompt": "x", "style": "noise", "resolution": "2k"}).canonical()
    b = normalize_request(IMAGE, {"resolution": "2k", "style": "noise", "prompt": " x "}).canonical()
    assert a == b and list(a["params"]) == sorted(a["params"])


def test_pricing_units_and_multipliers() -> None:
    assert quote(IMAGE, normalize_request(IMAGE, {"prompt": "x"})).total == 10
    doubled = quote(IMAGE, normalize_request(IMAGE, {"prompt": "x", "resolution": "2k", "count": 3}))
    assert (doubled.per_job, doubled.count, doubled.total) == (20, 3, 60)
    video = quote(VIDEO, normalize_request(VIDEO, {"prompt": "x", "duration": 5, "resolution": "720p"}))
    assert video.per_job == 20 * 5 * 2 and video.breakdown()["unit"] == "per_second"
    assert quote(AUDIO, normalize_request(AUDIO, {"prompt": "x"})).per_job == 6  # 3 s default x 2 credits


@pytest.mark.parametrize("count", ["--5", "\u00b2", "9" * 5000, "1e3", " "])
def test_odd_count_strings_are_errors_not_crashes(count: str) -> None:
    model = load_catalog(()).get("mock_image")
    with pytest.raises(ApiError):
        normalize_request(model, {"model": "mock_image", "prompt": "p", "count": count})


def test_adjustments_do_not_echo_oversized_values() -> None:
    model = load_catalog(()).get("mock_image")
    request = normalize_request(model, {"model": "mock_image", "prompt": "p", "seed": "0" * 50_000 + "7"})
    assert request.params["seed"] == 7 and len(str(request.adjustments)) < 500
