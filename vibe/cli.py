"""CLI: ``vibe run pipeline.yaml``, ``vibe estimate``, ``vibe models``.

The YAML form is for marketers (no code); the Python API is for engineers.
A pipeline file lists steps with logical inputs and ``after`` dependencies —
the same model-agnostic vocabulary the SDK uses.
"""

from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import typer
import yaml

from .capabilities import parse_capabilities
from .client import VibeClient
from .pipeline import Pipeline
from .router import Router
from .steps import Step

app = typer.Typer(
    add_completion=False,
    help="Resilient generation pipelines over VibeMarketolog Agent API.",
)


def _token() -> str:
    token = os.environ.get("VIBE_TOKEN")
    if not token:
        typer.secho(
            "Set VIBE_TOKEN env var (your Agent API key).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    return token


def _build_pipeline(spec: dict[str, Any]) -> Pipeline:
    try:
        budget = float(spec.get("budget_rub", 1000.0))
    except (TypeError, ValueError):
        budget = 1000.0
    p = Pipeline(
        budget_rub=budget,
        callback_url=spec.get("callback_url"),
    )
    steps_spec = spec.get("steps") or []
    for s in steps_spec:
        p.add(
            Step(
                id=s["id"],
                type=s["type"],
                model=s.get("model", "auto"),
                prompt=s["prompt"],
                inputs=s.get("inputs") or {},
                after=tuple(s.get("after") or ()),
                params=s.get("params") or {},
                tier=s.get("tier", "balanced"),
            )
        )
    return p


def _print_event(rec: dict[str, Any]) -> None:
    """Live progress printer for `vibe run` — one line per step event."""
    phase = rec.get("phase")
    step = rec.get("step")
    status = rec.get("status")
    model = rec.get("model", "")
    if phase == "start":
        typer.secho(f"• run_id={rec.get('run_id')} budget={rec.get('budget')}₽ steps={rec.get('steps')}", fg=typer.colors.CYAN)
        return
    if phase == "estimate":
        typer.secho(f"• estimate: {rec.get('estimated_total')}₽ (budget gate)", fg=typer.colors.CYAN)
        return
    if phase == "done":
        typer.secho(f"• done: net_cost={rec.get('net_cost')}₽", fg=typer.colors.CYAN)
        return
    if status == "running":
        typer.secho(f"▶ {step:<10} {model} …", fg=typer.colors.YELLOW)
        return
    if status == "complete":
        cost = rec.get("cost", 0)
        url = rec.get("display_url") or ""
        suffix = f"  {url}" if url else ""
        typer.secho(f"✓ {step:<10} {model}  {cost}₽{suffix}", fg=typer.colors.GREEN)
        return
    if status == "error":
        typer.secho(f"✗ {step:<10} {model}  {rec.get('error', '')} {rec.get('message', '')}", fg=typer.colors.RED, err=True)
        return


@app.command()
def run(
    pipeline_file: Path = typer.Argument(..., help="YAML pipeline spec"),
    run_id: str = typer.Option(None, help="Resume an existing run by id"),
) -> None:
    """Run a pipeline end-to-end (estimate → execute → print output URLs)."""
    import uuid

    spec = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
    pipeline = _build_pipeline(spec)
    token = _token()
    run_id = run_id or uuid.uuid4().hex[:12]
    typer.secho(
        f"run_id={run_id}  (resume: vibe run {pipeline_file} --run-id {run_id})",
        fg=typer.colors.CYAN,
    )

    async def _go() -> None:
        async with VibeClient(token) as client:
            outputs = await pipeline.run(client, run_id=run_id, on_event=_print_event)
            for sid, url in outputs.items():
                typer.secho(f"{sid}: {url}", fg=typer.colors.GREEN)

    asyncio.run(_go())


@app.command()
def estimate(
    pipeline_file: Path = typer.Argument(..., help="YAML pipeline spec"),
) -> None:
    """Dry-run: print the per-step cost estimate without charging anything."""
    spec = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
    pipeline = _build_pipeline(spec)
    token = _token()

    async def _go() -> None:
        async with VibeClient(token) as client:
            caps = await client.capabilities()
            registry = parse_capabilities(caps)
            router = Router(registry)
            outputs: dict[str, str] = {}
            total = 0.0
            for s in pipeline.steps:
                model = (
                    s.model
                    if s.model and s.model != "auto"
                    else router.pick_chain(s.type, tier=s.tier)[0]
                )
                body = s.to_body(
                    model, registry.get(model), outputs, drop_unresolved=True
                )
                body["strict"] = True
                est = await client.estimate(body)
                try:
                    c = float(est.get("estimated_cost_rub") or 0.0)
                except (TypeError, ValueError):
                    c = 0.0
                total += c
                typer.echo(f"{s.id:<20} {model:<22} {c:>8.2f} RUB")
            typer.secho(
                f"{'TOTAL':<20} {'':<22} {total:>8.2f} RUB", fg=typer.colors.CYAN
            )
            typer.secho(f"budget: {pipeline.budget_rub:.2f} RUB", fg=typer.colors.CYAN)

    asyncio.run(_go())


@app.command()
def models(
    type: str = typer.Option(None, help="Filter by type: image|video|voice|music|text"),
) -> None:
    """List available models (from /capabilities), sorted by price."""
    token = _token()

    async def _go() -> None:
        async with VibeClient(token) as client:
            registry = parse_capabilities(await client.capabilities())
            items = sorted(
                registry.values(), key=lambda m: (m.type, m.price_hint or 1e9)
            )
            for m in items:
                if type and m.type != type:
                    continue
                price = f"{m.price_hint:.2f}" if m.price_hint is not None else "?"
                typer.echo(f"{m.type:<7} {m.key:<26} {price:>8} RUB")

    asyncio.run(_go())


@app.command()
def balance() -> None:
    """Print current balance and token info."""
    token = _token()

    async def _go() -> None:
        async with VibeClient(token) as client:
            try:
                bal = await client.balance()
            except Exception:
                bal = None
            me = await client.me()
            typer.secho(f"balance: {bal if bal is not None else '?'} RUB", fg=typer.colors.GREEN)
            typer.secho(f"daily_spend: {me.get('daily_spend_today', '?')} / {me.get('daily_spend_limit', '?')} RUB",
                        fg=typer.colors.CYAN)
            typer.echo(json.dumps(me, ensure_ascii=False, indent=2, default=str))

    asyncio.run(_go())


if __name__ == "__main__":  # pragma: no cover
    app()
