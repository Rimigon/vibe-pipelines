"""Budget guardrail: estimate aggregation + refund-aware net spend."""

from __future__ import annotations

from vibe.budget import SpendTracker, aggregate_estimate


def test_aggregate_estimate_sums_per_step():
    estimates = {
        "poster": {"estimated_cost_rub": 19},
        "clip": {"estimated_cost_rub": 180},
        "jingle": {"estimated_cost_rub": 99},
    }
    total, per_step = aggregate_estimate(estimates)
    assert total == 298
    assert per_step == {"poster": 19, "clip": 180, "jingle": 99}


def test_aggregate_estimate_tolerates_missing_field():
    total, per_step = aggregate_estimate({"x": {}})
    assert total == 0.0
    assert per_step == {"x": 0.0}


def test_spend_tracker_net_reflects_refunds():
    t = SpendTracker(budget=300)
    t.record("poster", cost=19, refunded=False)
    t.record("clip", cost=180, refunded=True)  # failed → refunded
    assert t.gross_spent == 199
    assert t.refunded == 180
    assert t.net_spent == 19
    assert t.remaining == 281


def test_can_afford_uses_net_spent():
    t = SpendTracker(budget=100)
    t.record("a", cost=80, refunded=True)  # net = 0
    assert t.can_afford(100) is True
    t.record("b", cost=50, refunded=False)  # net = 50
    assert t.can_afford(50) is True
    assert t.can_afford(51) is False
