"""Model schema registry + logical→physical field mapper.

The single most expensive footgun in the API (called out in the docs):

    «Самая частая ошибка агентов — слать ``image_input`` для видео.
     Видео-модели его не читают → чистый text-to-video, а деньги спишутся.»

Different video models consume the source image under *different* field names:
``image_urls``, ``first_frame_url``, ``image_url`` (singular),
``character_image_url``, ``image_input`` (image type only). Getting it wrong
silently downgrades image-to-video to text-to-video and **still charges you**.

This module reads the real per-model schema from ``GET /capabilities`` and maps
*logical* step inputs (``source_image``, ``source_audio``, ``source_video``,
``character_image``, references) to whichever physical param the chosen model
actually accepts — so a pipeline author never types a model-specific field name.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from .errors import FieldMappingError


@dataclass(slots=True)
class ModelSchema:
    """Normalised view of one entry from ``GET /capabilities``."""

    key: str
    type: str  # image|text|video|voice|music
    params: set[str] = field(default_factory=set)
    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)
    price_hint: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def all_params(self) -> set[str]:
        return self.params or (self.required | self.optional)


def parse_capabilities(raw: dict[str, Any]) -> dict[str, ModelSchema]:
    """Build the registry from a raw ``/capabilities`` response.

    The endpoint is self-describing: every model lists its required/optional
    parameter names. We tolerate several reasonable shapes.
    """
    registry: dict[str, ModelSchema] = {}
    models = raw.get("models") or raw.get("capabilities") or raw
    if isinstance(models, dict):
        items = models.items()
    elif isinstance(models, list):
        items = ((m.get("model") or m.get("key") or m.get("id"), m) for m in models)
    else:
        items = ()
    for key, m in items:
        if not key or not isinstance(m, dict):
            continue
        required = set(m.get("required") or [])
        optional = set(m.get("optional") or [])
        params = set(m.get("params") or [])
        registry[str(key)] = ModelSchema(
            key=str(key),
            type=str(m.get("type", "")),
            params=params,
            required=required,
            optional=optional,
            price_hint=_to_float(m.get("price") or m.get("cost") or m.get("price_rub")),
            raw=m,
        )
    return registry


# --- logical → physical mapping --------------------------------------------
#
# For each logical input, candidate physical param names in priority order.
# The first one the model actually accepts wins. This is robust to new models:
# if a future model exposes ``image_urls``, it just works.

LOGICAL_TO_PHYSICAL: dict[str, tuple[str, ...]] = {
    "source_image": (
        "first_frame_url",  # seedance-2*
        "character_image_url",  # motion-control-*
        "image_url",  # omnihuman-1-5 (singular)
        "image_input",  # type=image edit models
        "image_urls",  # veo3*/kling/grok-itv/gemini-omni-video (list)
    ),
    "source_audio": ("audio_url",),
    "source_video": (
        "reference_video_url",  # motion-control-* (singular)
        "video_url",  # volcengine-lipsync
        "reference_video_urls",  # seedance references (list)
    ),
    "character_image": ("character_image_url", "image_url", "image_urls"),
    "reference_images": ("reference_image_urls", "image_urls"),
    "reference_audios": ("reference_audio_urls",),
    "mask": ("mask_url",),
}

# Params that are *list-typed*; a single logical value gets wrapped in a list.
LIST_PARAMS: frozenset[str] = frozenset(
    {
        "image_urls",
        "reference_image_urls",
        "reference_video_urls",
        "reference_audio_urls",
        "image_input",
    }  # image_input accepts a single url OR a list (up to 10)
)

# Companion flags that must be set when a source image is present.
# veo3* requires generation_type=image-to-video when image_urls is used.
COMPANIONS_WHEN_IMAGE: dict[str, dict[str, Any]] = {
    "veo3_fast": {"generation_type": "image-to-video"},
    "veo3.1": {"generation_type": "image-to-video"},
    "veo3": {"generation_type": "image-to-video"},
}


def map_step(
    model: str,
    schema: ModelSchema | None,
    logical_inputs: dict[str, Any],
    base: dict[str, Any],
) -> dict[str, Any]:
    """Produce a ready-to-send ``/generate`` body from logical inputs.

    ``base`` carries the always-present fields (type, model, prompt, ...).
    Raises :class:`FieldMappingError` if a logical input has nowhere to go on
    the chosen model — better to fail loudly (free, via estimate/strict) than
    to silently drop it and pay for the wrong generation.
    """
    body = dict(base)
    if schema is not None:
        accepted = schema.all_params
    else:
        # No schema available (offline/mocked): fall back to permissive mapping
        # using the union of all candidate physical names.
        accepted = {p for tup in LOGICAL_TO_PHYSICAL.values() for p in tup}

    used_image = False
    for logical, value in logical_inputs.items():
        candidates = LOGICAL_TO_PHYSICAL.get(logical)
        if not candidates:
            raise FieldMappingError(
                f"Unknown logical input {logical!r}", model=model, logical=logical
            )
        chosen = next((c for c in candidates if c in accepted), None)
        if chosen is None:
            raise FieldMappingError(
                f"Model {model!r} accepts none of {candidates} for logical input "
                f"{logical!r}; cannot map. Check /capabilities.",
                model=model,
                logical=logical,
            )
        if chosen in LIST_PARAMS and not isinstance(value, (list, tuple)):
            body[chosen] = [value]
        else:
            body[chosen] = value
        if logical in ("source_image", "character_image", "reference_images"):
            used_image = True

    # Companion flags (e.g. veo3 image-to-video mode).
    if used_image:
        companion = COMPANIONS_WHEN_IMAGE.get(model)
        if companion and schema and companion.keys() <= accepted:
            body.update(companion)

    return body


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
