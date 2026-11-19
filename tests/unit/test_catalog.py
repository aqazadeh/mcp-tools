import pytest

from mcp_tools.core.catalog.loader import (
    Availability,
    Catalog,
    CatalogError,
    load_catalog,
    parse_catalog_document,
)
from mcp_tools.core.errors import ApiError
from mcp_tools.core.pagination import PageRequest, decode_cursor, encode_cursor, page_request
from mcp_tools.core.yamlsafe import YamlError, load_yaml

DOC = """
provider: acme
provider_name: Acme
models:
  - id: acme_photo
    name: Acme Photo
    description: Photoreal product shots
    output_type: image
    tags: [photo, product]
    aspect_ratios: ["1:1"]
    parameters:
      - {name: quality, type: string, default: low, options: [low, high]}
    medias:
      - {role: image, kinds: [image], max: 2}
    pricing: {unit: per_output, credits: 40, multipliers: {quality: {low: 1, high: 4}}}
  - id: acme_clip
    name: Acme Clip
    description: Short clips from text
    output_type: video
    tags: [video]
    pricing: {unit: per_second, credits: 50}
"""


def _catalog(available: bool = True) -> Catalog:
    models = parse_catalog_document(DOC, source="test")
    return Catalog(models, lambda _p: Availability(available, None if available else "no key"))


def test_packaged_catalog_loads_and_mock_models_are_available() -> None:
    catalog = load_catalog()
    assert {"mock_image", "mock_video", "mock_audio"} <= {
        m.id for m in catalog.browse(PageRequest(limit=50)).items
    }
    assert catalog.get("mock_image").pricing.credits == 10
    assert catalog.by_task("upscale")[0].id == "mock_upscale"


def test_provider_defaults_are_merged_into_models() -> None:
    catalog = _catalog()
    model = catalog.get("acme_photo")
    assert (model.provider, model.provider_name) == ("acme", "Acme")
    assert model.accepts_media and not catalog.get("acme_clip").accepts_media


def test_public_views_hide_private_fields() -> None:
    catalog = _catalog(available=False)
    summary = catalog.public(catalog.get("acme_photo"))
    detail = catalog.public(catalog.get("acme_photo"), detail=True)
    assert summary["available"] is False and summary["unavailable_reason"] == "no key"
    assert summary["input"] == "media" and "parameters" not in summary
    assert detail["parameters"][0] == {
        "name": "quality",
        "type": "string",
        "required": False,
        "default": "low",
        "options": ["low", "high"],
    }
    assert detail["medias"] == [{"role": "image", "kinds": ["image"], "max": 2}]
    flat = repr(detail)
    for private in ("provider_ref", "common_map", "provider_field", "output_hosts", "execution"):
        assert private not in flat


def test_get_unknown_model_suggests_close_matches() -> None:
    with pytest.raises(ApiError) as excinfo:
        _catalog().get("acme_fotho")
    assert excinfo.value.code == "unknown_model"
    assert excinfo.value.recovery_tool == "models_explore"
    assert excinfo.value.allowed is not None and excinfo.value.allowed[0] == "acme_photo"


def test_browse_filters_and_paginates_with_opaque_cursor() -> None:
    catalog = _catalog()
    assert [m.id for m in catalog.browse(PageRequest(), type="video").items] == ["acme_clip"]
    assert [m.id for m in catalog.browse(PageRequest(), input="media").items] == ["acme_photo"]
    first = catalog.browse(PageRequest(limit=1))
    assert first.has_more and first.next_cursor
    second = catalog.browse(PageRequest(limit=1, cursor=decode_cursor(first.next_cursor)))
    assert not second.has_more and second.next_cursor is None
    assert {first.items[0].id, second.items[0].id} == {"acme_photo", "acme_clip"}


def test_search_requires_every_word_and_recommend_ranks_partial_matches() -> None:
    catalog = _catalog()
    assert [m.id for m in catalog.search("product photo", PageRequest()).items] == ["acme_photo"]
    assert catalog.search("product video", PageRequest()).items == []
    assert catalog.recommend("a product video clip")[0].id == "acme_clip"


def test_invalid_documents_are_rejected_at_load_time() -> None:
    with pytest.raises(CatalogError, match="models"):
        parse_catalog_document("provider: x", source="t")
    with pytest.raises(CatalogError, match="duplicate model id"):
        Catalog([*parse_catalog_document(DOC, source="a"), *parse_catalog_document(DOC, source="b")])
    shadow = DOC.replace("name: quality", "name: prompt")
    with pytest.raises(CatalogError, match="shadow common fields"):
        parse_catalog_document(shadow, source="t")
    unknown_key = DOC.replace("tags: [video]", "tags: [video]\n    webhook: https://x.test")
    with pytest.raises(CatalogError, match="webhook"):
        parse_catalog_document(unknown_key, source="t")


def test_yaml_aliases_and_oversized_documents_are_rejected() -> None:
    with pytest.raises(YamlError, match="aliases"):
        load_yaml("a: &x [1, 2]\nb: *x\n")
    with pytest.raises(YamlError, match="exceeds"):
        load_yaml("a: " + "x" * 100, max_bytes=50)
    assert load_yaml("a: 1") == {"a": 1}


def test_cursor_round_trip_and_rejection() -> None:
    assert decode_cursor(encode_cursor({"o": 5})) == {"o": 5}
    assert decode_cursor("") is None
    with pytest.raises(ApiError):
        decode_cursor("not-a-cursor!")
    assert page_request(0, "").limit == 20
    assert page_request(500, "").limit == 50
