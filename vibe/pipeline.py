"""Pipeline: a resilient DAG executor over the Agent API.

Responsibilities (each maps to a vacancy bullet):

* **DAG + concurrency** — topological order; independent steps run concurrently
  via ``asyncio.gather``; a step's completed ``display_url`` feeds dependents.
* **Estimate-before-run** — every step is dry-run via ``/generate/estimate``;
  if the summed estimate exceeds ``budget_rub`` we raise ``BudgetExceeded``
  with a per-step breakdown and **never call ``/generate``**.
* **Model fallback** — when a step's model fails with a retryable server error,
  the router supplies the next candidate and we retry (multi-provider routing).
* **Idempotency + resume** — each step gets a stable ``idempotency_key``
  (``run_id:step_id``); state is checkpointed after every transition so a
  crashed process resumes without double-charging (platform returns
  ``replayed:true``).
* **Refund-aware spend** — the budget gate uses *net* spend (gross − refunded).
* **Journaling** — every step emits a JSONL trace record.
"""

from __future__ import annotations
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .budget import SpendTracker, aggregate_estimate
from .capabilities import ModelSchema, parse_capabilities
from .client import GenerationResult, VibeClient
from .errors import BudgetExceeded, VibeError
from .journal import Journal
from .router import Router
from .state import RunState
from .steps import Step


@dataclass(slots=True)
class Pipeline:
    steps: list[Step] = field(default_factory=list)
    budget_rub: float = 1000.0
    callback_url: str | None = None
    state_dir: Path = field(default_factory=lambda: Path(".vibe-state"))
    journal_path: Path = field(default_factory=lambda: Path("vibe-run.jsonl"))
    _journal: Any = field(default=None, repr=False)
    _on_event: Any = field(default=None, repr=False)

    def add(self, *steps: Step) -> Pipeline:
        self.steps.extend(steps)
        return self

    # --- graph helpers ------------------------------------------------------
    def _topo_levels(self) -> list[list[str]]:
        """Group step ids into waves where every dep in an earlier wave is done."""
        ids = {s.id for s in self.steps}
        by_id = {s.id: s for s in self.steps}
        done: set[str] = set()
        waves: list[list[str]] = []
        remaining = set(ids)
        while remaining:
            ready = [
                sid for sid in remaining if all(d in done for d in by_id[sid].after)
            ]
            if not ready:
                cyc = ", ".join(sorted(remaining))
                raise VibeError(
                    code="cycle_in_pipeline",
                    message=f"Cycle/unsatisfied deps among: {cyc}",
                )
            waves.append(ready)
            done.update(ready)
            remaining -= set(ready)
        return waves

    async def run(
        self,
        client: VibeClient,
        *,
        run_id: str | None = None,
        on_event=None,
    ) -> dict[str, str]:
        run_id = run_id or uuid.uuid4().hex[:12]
        state_path = self.state_dir / f"{run_id}.json"
        state = RunState.load(run_id, state_path)
        journal = Journal(self.journal_path)
        self._journal = journal
        self._on_event = on_event
        self._emit(
            phase="start",
            run_id=run_id,
            budget=self.budget_rub,
            steps=[s.id for s in self.steps],
        )

        # 1) Load capabilities + router once.
        caps_raw = await client.capabilities()
        registry = parse_capabilities(caps_raw)
        router = Router(registry)

        # 2) Estimate phase — price the whole scenario before charging anything.
        outputs: dict[str, str] = {}
        for s in self.steps:
            st = state.get(s.id)
            if st.status == "complete" and st.display_url:
                outputs[s.id] = st.display_url  # resume: reuse completed output

        await self._estimate_phase(client, registry, router, outputs, state, journal)

        # 3) Execution phase — wave by wave, steps in a wave run concurrently.
        tracker = SpendTracker(budget=self.budget_rub)
        for st in state.steps.values():
            if st.status == "complete":
                tracker.record(
                    st.step_id, st.cost, st.refunded
                )  # restore net spend on resume

        for wave in self._topo_levels():
            tasks = [
                self._run_step(
                    s, client, registry, router, outputs, state, tracker, journal
                )
                for s in self.steps
                if s.id in wave
            ]
            await asyncio.gather(*tasks)

        self._emit(phase="done", outputs=outputs, net_cost=round(tracker.net_spent, 2))
        return outputs

    def _emit(self, **fields: Any) -> dict[str, Any]:
        """Write a journal record and forward it to the live on_event callback."""
        journal = getattr(self, "_journal", None)
        rec = (
            journal.emit(**fields)
            if journal is not None
            else {"ts": time.time(), **fields}
        )
        on_event = getattr(self, "_on_event", None)
        if on_event is not None:
            on_event(rec)
        return rec

    # --- estimate phase -----------------------------------------------------
    async def _estimate_phase(
        self,
        client: VibeClient,
        registry: dict[str, ModelSchema],
        router: Router,
        outputs: dict[str, str],
        state: RunState,
        journal: Journal,
    ) -> None:
        # Estimate is about cost, not real URLs. Dependent steps reference steps
        # that haven't run yet — their image URL doesn't exist. The price
        # depends only on model + duration, so we DROP unresolved references
        # (drop_unresolved=True) rather than send a fake URL that would fail
        # media validation. On resume, completed steps' real URLs are in
        # ``outputs`` and do get used.
        estimates: dict[str, dict[str, Any]] = {}
        for s in self.steps:
            st = state.get(s.id)
            if st.status == "complete":
                continue  # already done on resume; no need to re-estimate
            model = self._pick_primary(s, registry, router, self.budget_rub)
            schema = registry.get(model)
            body = s.to_body(model, schema, outputs, drop_unresolved=True)
            body["strict"] = True
            try:
                est = await client.estimate(body)
            except VibeError as exc:
                self._emit(
                    step=s.id,
                    model=model,
                    status="error",
                    error=exc.code,
                    message=exc.message,
                    phase="estimate",
                )
                raise
            estimates[s.id] = est
            st.model = model

        total, per_step = aggregate_estimate(estimates)
        self._emit(phase="estimate", estimated_total=total, per_step=per_step)
        if total > self.budget_rub:
            raise BudgetExceeded(total, self.budget_rub, per_step)

    # --- single step execution ---------------------------------------------
    async def _run_step(
        self,
        step: Step,
        client: VibeClient,
        registry: dict[str, ModelSchema],
        router: Router,
        outputs: dict[str, str],
        state: RunState,
        tracker: SpendTracker,
        journal: Journal,
    ) -> None:
        st = state.get(step.id)
        if st.status == "complete" and st.display_url:
            outputs[step.id] = st.display_url
            return

        # Resume an IN-FLIGHT step: the process crashed mid-poll. Re-poll the
        # existing generation instead of starting a new one (avoids a second
        # charge; the platform keeps the task running on its side).
        if (
            st.status == "running"
            and (st.generation_id is not None or st.voiceover_id is not None)
            and not st.error
        ):
            await self._resume_poll(step, client, st, outputs, tracker, journal, state)
            if st.status == "complete":
                return
            # otherwise it errored → fall through to the fallback/retry chain

        # Fallback chain: explicit model first, then router reserves.
        chain = self._chain_for(step, registry, router, tracker.remaining)
        last_err: VibeError | None = None

        for model in chain:
            schema = registry.get(model)
            try:
                # Budget gate before each attempt — use a per-step estimate if cheap.
                est_cost = self._step_price(model, schema)
                if est_cost is not None and not tracker.can_afford(est_cost):
                    last_err = BudgetExceeded(
                        tracker.net_spent + est_cost, self.budget_rub, tracker.per_step
                    )
                    break

                # Fresh idempotency key per attempt (includes attempt counter):
                # replaying an ERRORED step with a changed body would otherwise
                # hit 409 idempotency_key_conflict on the old key.
                st.attempts += 1
                st.idempotency_key = f"{state.run_id}:{step.id}:{st.attempts}"
                st.status = "running"
                st.model = model
                st.error = None
                state.save()
                self._emit(
                    step=step.id,
                    model=model,
                    type=step.type,
                    status="running",
                    attempts=st.attempts,
                )

                body = step.to_body(model, schema, outputs)
                t0 = time.time()
                gen = await client.generate(body, idempotency_key=st.idempotency_key)

                result = await self._await_completion(client, gen, st)
                st.status = result.status
                st.generation_id = result.generation_id
                st.display_url = result.display_url
                st.cost = result.cost
                st.refunded = result.refunded
                st.error = result.error_message
                state.save()

                tracker.record(step.id, result.cost, result.refunded)
                self._emit(
                    step=step.id,
                    model=model,
                    type=step.type,
                    generation_id=result.generation_id,
                    status=result.status,
                    cost=result.cost,
                    refunded=result.refunded,
                    attempts=st.attempts,
                    error=result.error_message,
                    duration=round(time.time() - t0, 2),
                )

                if result.status == "complete" and result.display_url:
                    outputs[step.id] = result.display_url
                    return
                if result.status == "error":
                    last_err = VibeError(
                        code="generation_error",
                        message=result.error_message or "generation.error",
                    )
                    continue  # try next model in fallback chain

            except VibeError as exc:
                last_err = exc
                st.error = exc.message
                state.save()
                self._emit(
                    step=step.id,
                    model=model,
                    status="error",
                    error=exc.code,
                    message=exc.message,
                    attempts=st.attempts,
                )
                if exc.code in ("insufficient_balance", "daily_spend_limit_exceeded"):
                    break  # no point trying another model
                continue  # retryable → next model

        st.status = "error"
        state.save()
        assert last_err is not None
        raise last_err

    async def _await_completion(
        self, client: VibeClient, gen_resp: dict[str, Any], st: Any
    ) -> GenerationResult:
        """Poll regular or long-voiceover generations to completion."""
        if gen_resp.get("long_voiceover"):
            st.voiceover_id = gen_resp.get("voiceover_id")
            st.status_url = gen_resp.get("status_url")
            return await client.poll(
                voiceover_id=st.voiceover_id, status_url=st.status_url
            )
        gid = gen_resp.get("generation_id")
        return await client.poll(generation_id=gid)

    async def _resume_poll(
        self,
        step: Step,
        client: VibeClient,
        st: Any,
        outputs: dict[str, str],
        tracker: SpendTracker,
        journal: Journal,
        state: RunState,
    ) -> None:
        """Re-poll an in-flight generation after a crash — no new charge."""
        result = await self._await_completion_existing(client, st)
        st.status = result.status
        st.display_url = result.display_url or st.display_url
        st.cost = result.cost or st.cost
        st.refunded = result.refunded
        st.error = result.error_message
        state.save()
        tracker.record(step.id, result.cost, result.refunded)
        self._emit(
            step=step.id,
            model=st.model,
            type=step.type,
            generation_id=result.generation_id,
            status=result.status,
            cost=result.cost,
            refunded=result.refunded,
            resumed=True,
            attempts=st.attempts,
            error=result.error_message,
        )
        if result.status == "complete" and st.display_url:
            outputs[step.id] = st.display_url

    async def _await_completion_existing(
        self, client: VibeClient, st: Any
    ) -> GenerationResult:
        """Poll an already-started generation using stored ids (resume path)."""
        if st.voiceover_id is not None or st.status_url is not None:
            return await client.poll(
                voiceover_id=st.voiceover_id, status_url=st.status_url
            )
        return await client.poll(generation_id=st.generation_id)

    # --- model selection ----------------------------------------------------
    def _pick_primary(
        self,
        step: Step,
        registry: dict[str, ModelSchema],
        router: Router,
        budget: float,
    ) -> str:
        chain = self._chain_for(step, registry, router, budget)
        if not chain:
            raise VibeError(
                code="model_not_supported",
                message=f"No model of type {step.type!r} fits budget {budget}",
            )
        return chain[0]

    def _chain_for(
        self,
        step: Step,
        registry: dict[str, ModelSchema],
        router: Router,
        budget: float | None,
    ) -> list[str]:
        if step.model and step.model != "auto":
            return [step.model]
        return router.pick_chain(step.type, tier=step.tier, budget=budget)

    def _step_price(self, model: str, schema: ModelSchema | None) -> float | None:
        return schema.price_hint if schema else None
