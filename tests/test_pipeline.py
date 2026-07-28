"""Pipeline DAG executor: budget gate, dependency ordering, and crash resume.

Fully mocked transport — no network, no rubles spent. Verifies the three
load-bearing guarantees: estimate-before-run refuses over-budget scenarios
without calling /generate; dependent steps start only after their deps
complete; and a resumed run reuses completed outputs and re-polls in-flight.
"""

from __future__ import annotations
import json
import re
from pathlib import Path

import httpx
import pytest

from vibe.client import VibeClient
from vibe.errors import BudgetExceeded
from vibe.pipeline import Pipeline
from vibe.steps import Step
from tests.conftest import CAPABILITIES, body


def _price_of(model: str) -> int:
    for m in CAPABILITIES["models"]:
        if m["model"] == model:
            return int(m["price"])
    return 50


def _transport(state: dict, on_generate=None) -> httpx.MockTransport:
    gid_counter = {"n": 1000}
    status_re = re.compile(r"/api/agent/generation/(\d+)/status")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/agent/capabilities":
            return httpx.Response(200, json=CAPABILITIES)
        if path == "/api/agent/generate/estimate":
            b = body(request)
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "dry_run": True,
                    "estimated_cost_rub": _price_of(b.get("model", "")),
                    "balance": {"current": 500, "after": 500},
                },
            )
        if path == "/api/agent/generate":
            b = body(request)
            gid_counter["n"] += 1
            gid = gid_counter["n"]
            state["order"].append(b["model"])
            state["costs"][gid] = _price_of(b["model"])
            if on_generate is not None:
                on_generate(b)
            return httpx.Response(
                200,
                json={
                    "status": "processing",
                    "generation_id": gid,
                    "task_id": f"t{gid}",
                    "cost": _price_of(b["model"]),
                    "balance_after": 500,
                },
            )
        m = status_re.search(path)
        if m is not None:
            gid = int(m.group(1))
            return httpx.Response(
                200,
                json={
                    "status": "complete",
                    "generation_id": gid,
                    "display_url": f"https://lk.vibemarketolog.ru/files/generation/{gid}?sig=x",
                    "cost": state["costs"].get(gid, 10),
                    "refunded": False,
                    "model": "x",
                    "type": "x",
                },
            )
        return httpx.Response(
            404,
            json={
                "status": "error",
                "error": "not_found",
                "message": f"no mock for {path}",
            },
        )

    return httpx.MockTransport(handler)


def _reel_pipeline(budget: float, state_dir: Path) -> Pipeline:
    poster = Step(
        id="poster",
        type="image",
        model="seedream-5-pro",
        prompt="баннер",
        params={"aspect_ratio": "1:1"},
    )
    jingle = Step(
        id="jingle",
        type="music",
        model="suno-v5.5",
        prompt="лёгкий pop",
        params={"music_style": "pop"},
    )
    clip = Step(
        id="clip",
        type="video",
        model="seedance-2-fast",
        prompt="зум на продукт",
        inputs={"source_image": "${poster}"},
        params={"duration": 5, "aspect_ratio": "9:16"},
    ).depends_on("poster")
    return Pipeline(
        steps=[poster, jingle, clip],
        budget_rub=budget,
        state_dir=state_dir,
        journal_path=state_dir / "run.jsonl",
    )


async def test_pipeline_runs_in_dependency_order(tmp_path):
    state: dict = {"order": [], "costs": {}}
    c = VibeClient("t", transport=_transport(state))
    p = _reel_pipeline(budget=1000, state_dir=tmp_path)
    outputs = await p.run(c, run_id="r1")
    await c.aclose()

    assert set(outputs) == {"poster", "jingle", "clip"}
    assert all(
        u.startswith("https://lk.vibemarketolog.ru/files/") for u in outputs.values()
    )

    order = state["order"]
    # clip depends on poster → its /generate call must come AFTER poster's.
    assert order.index("seedance-2-fast") > order.index("seedream-5-pro")
    # jingle has no deps → runs in wave 1 alongside poster, before clip.
    assert order.index("suno-v5.5") < order.index("seedance-2-fast")
    assert len(order) == 3  # exactly one generate per step


async def test_budget_gate_blocks_run_before_any_charge(tmp_path):
    state: dict = {"order": [], "costs": {}}
    c = VibeClient("t", transport=_transport(state))
    p = _reel_pipeline(budget=10, state_dir=tmp_path)  # estimate 238 > 10
    with pytest.raises(BudgetExceeded) as exc:
        await p.run(c, run_id="r2")
    await c.aclose()
    assert exc.value.estimated == 238
    assert state["order"] == []  # /generate NEVER called — no money spent


async def test_resume_skips_completed_step(tmp_path):
    run_id = "r3"
    state_path = tmp_path / f"{run_id}.json"
    state_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "steps": {
                    "poster": {
                        "step_id": "poster",
                        "status": "complete",
                        "display_url": "https://resumed/poster.png",
                        "cost": 19.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    state: dict = {"order": [], "costs": {}}
    c = VibeClient("t", transport=_transport(state))
    p = _reel_pipeline(budget=1000, state_dir=tmp_path)
    outputs = await p.run(c, run_id=run_id)
    await c.aclose()

    # poster was NOT regenerated — its resumed URL is reused as clip's input.
    assert "seedream-5-pro" not in state["order"]
    assert outputs["poster"] == "https://resumed/poster.png"
    assert "seedance-2-fast" in state["order"]
    assert state["order"].count("seedance-2-fast") == 1


def _transport_recording(state: dict) -> httpx.MockTransport:
    """Like _transport but also records each /generate body's idempotency_key."""

    def on_generate(b: dict) -> None:
        state["keys"].setdefault(b["model"], []).append(b.get("idempotency_key"))

    return _transport(state, on_generate=on_generate)


async def test_inflight_resume_re_polls_without_recharge(tmp_path):
    # clip was running when the process crashed (generation_id already issued).
    run_id = "r4"
    sp = tmp_path / f"{run_id}.json"
    sp.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "steps": {
                    "poster": {
                        "step_id": "poster",
                        "status": "complete",
                        "display_url": "https://resumed/poster.png",
                        "cost": 40.0,
                    },
                    "clip": {
                        "step_id": "clip",
                        "status": "running",
                        "generation_id": 7777,
                        "model": "seedance-2-mini",
                        "idempotency_key": "r4:clip:1",
                        "attempts": 1,
                        "cost": 0.0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    state: dict = {"order": [], "costs": {}, "keys": {}}
    c = VibeClient("t", transport=_transport_recording(state))
    p = _reel_pipeline(budget=1000, state_dir=tmp_path)
    outputs = await p.run(c, run_id=run_id)
    await c.aclose()

    # clip was NOT re-generated — resumed via re-poll of generation 7777.
    assert (
        "seedance-2-fast" not in state["order"]
    )  # resumed via re-poll, not regenerated
    assert outputs["clip"].endswith("/files/generation/7777?sig=x")


async def test_errored_step_retry_uses_fresh_idempotency_key(tmp_path):
    # clip errored with an old key; retrying with a changed prompt must NOT
    # reuse the old key (would 409 idempotency_key_conflict on a new body).
    run_id = "r5"
    sp = tmp_path / f"{run_id}.json"
    sp.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "steps": {
                    "poster": {
                        "step_id": "poster",
                        "status": "complete",
                        "display_url": "https://resumed/poster.png",
                        "cost": 40.0,
                    },
                    "clip": {
                        "step_id": "clip",
                        "status": "error",
                        "model": "seedance-2-mini",
                        "idempotency_key": "r5:clip:1",
                        "attempts": 1,
                        "error": "captions are not enough",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    state: dict = {"order": [], "costs": {}, "keys": {}}
    c = VibeClient("t", transport=_transport_recording(state))
    p = _reel_pipeline(budget=1000, state_dir=tmp_path)
    outputs = await p.run(c, run_id=run_id)
    await c.aclose()

    # clip WAS re-generated (errored → retry), with a NEW key ≠ old one.
    assert "seedance-2-fast" in state["order"]  # errored → retried
    new_keys = state["keys"].get("seedance-2-fast", [])
    assert new_keys and new_keys[0] != "r5:clip:1"  # fresh key, no 409
    assert outputs["clip"].startswith("https://lk.vibemarketolog.ru/files/")
