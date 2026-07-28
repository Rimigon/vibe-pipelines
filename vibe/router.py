"""Model router: pick a model by type/quality/budget and build a fallback chain.

This is the client-side mirror of the platform's internal multi-engine reserve.
The platform already swaps to a reserve provider when one model is down; here we
add **agent-level** routing: choose the cheapest model that satisfies a quality
tier, and fall back across tiers on persistent failure — so a Veo outage can
degrade gracefully to Seedance instead of failing the whole pipeline.

Price is used as a quality proxy because ``/capabilities`` exposes prices but
not quality scores; the router is written so a real quality metric can be
slotted in without changing the call sites.
"""

from __future__ import annotations
from collections.abc import Iterable
from dataclasses import dataclass

from .capabilities import ModelSchema


@dataclass(slots=True)
class Router:
    registry: dict[str, ModelSchema]

    def models_of_type(self, type_: str) -> list[ModelSchema]:
        return [m for m in self.registry.values() if m.type == type_]

    def ranked(self, type_: str) -> list[ModelSchema]:
        """All models of ``type_`` sorted by price ascending (cheap → pricey)."""
        return sorted(
            self.models_of_type(type_),
            key=lambda m: m.price_hint if m.price_hint is not None else 1e9,
        )

    def pick_chain(
        self,
        type_: str,
        *,
        tier: str = "balanced",
        budget: float | None = None,
        prefer: str | None = None,
        exclude: Iterable[str] = (),
    ) -> list[str]:
        """Return an ordered fallback chain of model keys.

        ``tier``: ``economy`` (cheapest), ``balanced`` (median), ``quality``
        (most expensive). ``prefer`` pins a specific model first if it exists
        and fits the budget. ``exclude`` drops models already tried+failed.

        The chain is: [preferred|tier-pick, same-tier reserves, then degrade to
        cheaper tiers], all filtered by ``budget``.
        """
        excluded = set(exclude)
        ranked = self.ranked(type_)
        fitting = [
            m
            for m in ranked
            if m.key not in excluded
            and (budget is None or m.price_hint is None or m.price_hint <= budget)
        ]
        if not fitting:
            return []

        def tier_pick(t: str) -> int:
            n = len(fitting)
            if t == "economy":
                return 0
            if t == "quality":
                return n - 1
            return n // 2  # balanced → median

        primary_idx = max(0, min(tier_pick(tier), len(fitting) - 1))
        chain: list[str] = []

        if prefer and prefer in self.registry and prefer not in excluded:
            m = self.registry[prefer]
            if budget is None or m.price_hint is None or m.price_hint <= budget:
                chain.append(prefer)

        # Same-tier reserves, then degrade cheaper, then upgrade as last resort.
        order = (
            [primary_idx]
            + list(range(primary_idx))  # cheaper (degrade)
            + list(range(primary_idx + 1, len(fitting)))  # pricier (upgrade)
        )
        seen = set(chain)
        for i in order:
            key = fitting[i].key
            if key not in seen:
                chain.append(key)
                seen.add(key)
        return chain
