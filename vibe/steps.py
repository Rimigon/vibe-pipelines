"""Step: a single generation node in a pipeline, expressed in logical inputs.

A pipeline author never writes model-specific field names. They declare a step
with a *type*, a *model* (or let the router pick), a prompt, and logical inputs
like ``source_image``/``source_audio``. The mapper in :mod:`vibe.capabilities`
translates those to the right physical field per model at run time.

Steps declare their dependencies via ``after``; the executor builds a DAG and
runs independent steps concurrently, feeding each completed step's
``display_url`` into dependents that reference it.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from .capabilities import map_step
from .errors import FieldMappingError


@dataclass(slots=True)
class Step:
    id: str
    type: str  # image|text|video|voice|music
    model: str
    prompt: str
    # logical inputs; values are either a URL string, a list of URLs, or a
    # reference to another step's output: ``Step`` instance (resolved at runtime
    # to that step's display_url) or the string id "${step_id}".
    inputs: dict[str, Any] = field(default_factory=dict)
    after: tuple[str, ...] = ()
    # extra model-specific physical params the author *does* want to pin
    # (aspect_ratio, duration, resolution, voice_id, quality, ...). These pass
    # through unchanged after a strict-mode compatibility check.
    params: dict[str, Any] = field(default_factory=dict)
    tier: str = "balanced"  # economy|balanced|quality — used when model is auto-picked
    callback_url: str | None = None
    _resolved_inputs: dict[str, Any] = field(default_factory=dict, repr=False)

    def depends_on(self, *step_ids: str) -> Step:
        self.after = tuple({*self.after, *step_ids})
        return self

    def with_inputs(self, **logical: Any) -> Step:
        self.inputs.update(logical)
        return self

    def resolve_references(
        self, outputs: dict[str, str], *, drop_unresolved: bool = False
    ) -> dict[str, Any]:
        """Replace ``${step_id}`` references and ``Step`` objects with real URLs.

        ``outputs`` maps step_id → display_url. Returns the resolved logical
        inputs dict. Called by the executor right before mapping.

        ``drop_unresolved``: if True, inputs pointing at steps that haven't
        produced an output yet are *dropped* instead of raising. Used for the
        pre-run estimate — a dependent step's image URL doesn't exist yet, but
        the price depends only on model + duration, so omitting it still yields
        an accurate cost estimate (and avoids sending a fake URL that would
        fail media validation).
        """
        resolved: dict[str, Any] = {}
        for logical, value in self.inputs.items():
            r = _resolve_value(value, outputs, drop_unresolved=drop_unresolved)
            if r is _UNRESOLVED:
                continue
            resolved[logical] = r
        self._resolved_inputs = resolved
        return resolved

    def to_body(
        self,
        model: str,
        schema: Any,
        outputs: dict[str, str],
        *,
        drop_unresolved: bool = False,
    ) -> dict[str, Any]:
        """Build the ``/generate`` body for this step under the chosen model."""
        base: dict[str, Any] = {
            "type": self.type,
            "model": model,
            "prompt": self.prompt,
        }
        base.update(self.params)
        if self.callback_url:
            base["callback_url"] = self.callback_url
        resolved = self.resolve_references(outputs, drop_unresolved=drop_unresolved)
        return map_step(model, schema, resolved, base)


_UNRESOLVED = object()


def _resolve_value(
    value: Any, outputs: dict[str, str], *, drop_unresolved: bool = False
) -> Any:
    if isinstance(value, Step):
        url = outputs.get(value.id)
        if url:
            return url
        if drop_unresolved:
            return _UNRESOLVED
        raise FieldMappingError(
            f"Step {value.id!r} has no output URL yet; dependency ordering is wrong.",
            logical="step_reference",
        )
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        ref = value[2:-1]
        url = outputs.get(ref)
        if url:
            return url
        if drop_unresolved:
            return _UNRESOLVED
        raise FieldMappingError(
            f"Referenced step {ref!r} has no output URL yet.",
            logical="step_reference",
        )
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        for v in value:
            r = _resolve_value(v, outputs, drop_unresolved=drop_unresolved)
            if r is _UNRESOLVED:
                continue
            out.append(r)
        return out
    return value
