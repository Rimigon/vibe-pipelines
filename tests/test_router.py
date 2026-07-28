"""Model router: tier selection + fallback chain construction."""

from __future__ import annotations

from vibe.capabilities import parse_capabilities
from vibe.router import Router


def test_economy_picks_cheapest(caps):
    r = Router(parse_capabilities(caps))
    chain = r.pick_chain("video", tier="economy")
    assert chain[0] == "seedance-2-fast"  # 120 < 149 < 180 < 320


def test_quality_picks_priciest(caps):
    r = Router(parse_capabilities(caps))
    chain = r.pick_chain("video", tier="quality")
    assert chain[0] == "omnihuman-1-5"  # 320


def test_budget_filter(caps):
    r = Router(parse_capabilities(caps))
    chain = r.pick_chain("video", tier="quality", budget=150)
    # omnihuman(320) and kling(180) excluded; veo3_fast(149) is the priciest fitting
    assert "omnihuman-1-5" not in chain
    assert chain[0] == "veo3_fast"


def test_exclude_drops_failed_model(caps):
    r = Router(parse_capabilities(caps))
    chain = r.pick_chain("video", tier="economy", exclude={"seedance-2-fast"})
    assert "seedance-2-fast" not in chain
    assert chain[0] == "veo3_fast"  # next cheapest


def test_prefer_pins_model_first(caps):
    r = Router(parse_capabilities(caps))
    chain = r.pick_chain("video", tier="economy", prefer="kling-3.0-pro")
    assert chain[0] == "kling-3.0-pro"


def test_no_fitting_model_returns_empty(caps):
    r = Router(parse_capabilities(caps))
    assert r.pick_chain("video", tier="economy", budget=5) == []
