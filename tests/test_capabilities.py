"""The footgun killer: logical inputs map to the right physical field per model.

The docs warn that sending ``image_input`` to a video model silently produces
text-to-video and still charges. These tests pin the mapping for every video
family and assert the image-type field *is not* used for video.
"""

from __future__ import annotations
import pytest

from vibe.capabilities import ModelSchema, map_step, parse_capabilities
from vibe.errors import FieldMappingError


def _schema(caps, model):
    return parse_capabilities(caps)[model]


def test_parse_capabilities_builds_registry(caps):
    reg = parse_capabilities(caps)
    assert "seedance-2-fast" in reg
    assert reg["omnihuman-1-5"].type == "video"
    assert "image_url" in reg["omnihuman-1-5"].all_params


@pytest.mark.parametrize(
    "model, expected_field",
    [
        ("seedance-2-fast", "first_frame_url"),  # ByteDance Seedance
        ("kling-3.0-pro", "image_urls"),  # Kling
        ("veo3_fast", "image_urls"),  # Veo 3
        ("omnihuman-1-5", "image_url"),  # Omnihuman (singular!)
        (
            "seedream-5-pro",
            "image_input",
        ),  # image edit model — only here is image_input correct
    ],
)
def test_source_image_maps_to_correct_field(caps, model, expected_field):
    schema = _schema(caps, model)
    body = map_step(
        model,
        schema,
        {"source_image": "https://x/img.png"},
        base={"type": schema.type, "model": model, "prompt": "p"},
    )
    assert expected_field in body
    # The image-type-only field must NOT leak into video bodies.
    if schema.type == "video":
        assert "image_input" not in body


def test_list_params_wrap_single_url(caps):
    schema = _schema(caps, "kling-3.0-pro")
    body = map_step(
        "kling-3.0-pro",
        schema,
        {"source_image": "https://x/a.png"},
        base={"type": "video", "model": "kling-3.0-pro", "prompt": "p"},
    )
    assert body["image_urls"] == ["https://x/a.png"]  # list, not bare string


def test_veo3_image_to_video_companion_flag(caps):
    schema = _schema(caps, "veo3_fast")
    body = map_step(
        "veo3_fast",
        schema,
        {"source_image": "https://x/a.png"},
        base={"type": "video", "model": "veo3_fast", "prompt": "p"},
    )
    assert body["generation_type"] == "image-to-video"


def test_source_audio_maps_to_audio_url(caps):
    schema = _schema(caps, "omnihuman-1-5")
    body = map_step(
        "omnihuman-1-5",
        schema,
        {"source_image": "https://x/i.png", "source_audio": "https://x/a.mp3"},
        base={"type": "video", "model": "omnihuman-1-5", "prompt": "p"},
    )
    assert body["image_url"] == "https://x/i.png"
    assert body["audio_url"] == "https://x/a.mp3"


def test_unmappable_logical_input_raises(caps):
    """If a model accepts none of the candidate fields, fail loudly (free) not silently (paid)."""
    schema = ModelSchema(
        key="text-only", type="text", required={"prompt"}, optional=set()
    )
    with pytest.raises(FieldMappingError):
        map_step(
            "text-only",
            schema,
            {"source_image": "https://x/i.png"},
            base={"type": "text", "model": "text-only", "prompt": "p"},
        )


def test_reference_images_list(caps):
    schema = _schema(caps, "seedance-2-fast")
    body = map_step(
        "seedance-2-fast",
        schema,
        {"reference_images": ["https://x/1.png", "https://x/2.png"]},
        base={"type": "video", "model": "seedance-2-fast", "prompt": "p"},
    )
    assert body["reference_image_urls"] == ["https://x/1.png", "https://x/2.png"]
