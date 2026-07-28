"""Budget guardrail: estimate-before-run + spend tracking with refund net.

The platform charges per generation in rubles and exposes a free dry-run
(``POST /generate/estimate``). We use it to price the *whole* scenario before
spending a kopeck, and refuse to run if the estimate exceeds the declared
budget — surfacing a per-step breakdown instead of a silent overspend.

During execution we track actual ``cost`` and ``refunded`` per step so the
*net* spend (what actually left the balance) is the number we gate on, not the
gross estimate. This is the "считать себестоимость сценария" bullet.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(slots=True)
class SpendTracker:
    budget: float
    gross_spent: float = 0.0
    refunded: float = 0.0
    per_step: dict[str, float] = field(default_factory=dict)

    @property
    def net_spent(self) -> float:
        return self.gross_spent - self.refunded

    @property
    def remaining(self) -> float:
        return self.budget - self.net_spent

    def record(self, step_id: str, cost: float, refunded: bool) -> None:
        self.gross_spent += cost
        if refunded:
            self.refunded += cost
        self.per_step[step_id] = self.per_step.get(step_id, 0.0) + (
            cost if not refunded else 0.0
        )

    def can_afford(self, estimated: float) -> bool:
        return self.net_spent + estimated <= self.budget + 1e-6


def aggregate_estimate(estimates: dict[str, dict]) -> tuple[float, dict[str, float]]:
    """Sum ``estimated_cost_rub`` across per-step estimate responses.

    Returns (total, per_step). Tolerates estimate responses that omit the
    field (treats as 0) — the caller still gets the breakdown.
    """
    per_step: dict[str, float] = {}
    total = 0.0
    for step_id, est in estimates.items():
        try:
            c = float(est.get("estimated_cost_rub") or 0.0)
        except (TypeError, ValueError):
            c = 0.0
        per_step[step_id] = c
        total += c
    return total, per_step
