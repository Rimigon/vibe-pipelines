"""Crash-recovery state for a pipeline run.

A run is checkpointed to a single JSON file keyed by ``run_id``. On resume we
reload it: completed steps are skipped (their ``display_url`` is reused by
dependents), in-flight steps are re-polled (``idempotency_key`` makes a re-run
safe — the platform returns ``replayed:true`` without recharging), and
not-started steps run normally.

This is the "восстановление состояния после сбоя" bullet: an agent that crashes
mid-scenario loses no money and no progress.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StepState:
    step_id: str
    status: str = "pending"  # pending|running|complete|error|skipped
    model: str | None = None
    generation_id: int | None = None
    voiceover_id: int | None = None
    status_url: str | None = None
    display_url: str | None = None
    cost: float = 0.0
    refunded: bool = False
    idempotency_key: str | None = None
    attempts: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "model": self.model,
            "generation_id": self.generation_id,
            "voiceover_id": self.voiceover_id,
            "status_url": self.status_url,
            "display_url": self.display_url,
            "cost": self.cost,
            "refunded": self.refunded,
            "idempotency_key": self.idempotency_key,
            "attempts": self.attempts,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StepState:
        try:
            cost = float(d.get("cost") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            attempts = int(d.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        return cls(
            step_id=d["step_id"],
            status=d.get("status", "pending"),
            model=d.get("model"),
            generation_id=d.get("generation_id"),
            voiceover_id=d.get("voiceover_id"),
            status_url=d.get("status_url"),
            display_url=d.get("display_url"),
            cost=cost,
            refunded=bool(d.get("refunded")),
            idempotency_key=d.get("idempotency_key"),
            attempts=attempts,
            error=d.get("error"),
        )


@dataclass(slots=True)
class RunState:
    run_id: str
    path: Path
    steps: dict[str, StepState] = field(default_factory=dict)

    @classmethod
    def load(cls, run_id: str, path: Path) -> RunState:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return cls(run_id=run_id, path=path)
            steps = {
                k: StepState.from_dict(v) for k, v in data.get("steps", {}).items()
            }
            return cls(run_id=run_id, path=path, steps=steps)
        return cls(run_id=run_id, path=path)

    def get(self, step_id: str) -> StepState:
        if step_id not in self.steps:
            self.steps[step_id] = StepState(step_id=step_id)
        return self.steps[step_id]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "steps": {k: v.to_dict() for k, v in self.steps.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)
