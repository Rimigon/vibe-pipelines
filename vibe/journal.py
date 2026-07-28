"""Structured step journal — observability for autonomous generation runs.

Every step emits one JSONL record with: step id, model, type, request_id,
generation_id, status, cost, refunded, attempts, error, duration, timestamp.
This is the "журналирование шагов / трассировка" the vacancy asks for: a run
can be replayed, audited, and post-mortem-analysed from the journal alone,
which is also the basis for evals and cost analytics.
"""

from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Journal:
    path: Path
    records: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, **fields: Any) -> dict[str, Any]:
        record = {"ts": time.time(), **fields}
        self.records.append(record)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def summary(self) -> dict[str, Any]:
        def _f(r: dict[str, Any]) -> float:
            try:
                return float(r.get("cost") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        costs = [_f(r) for r in self.records]
        total_cost = sum(costs)
        refunded = sum(
            c for c, r in zip(costs, self.records, strict=True) if r.get("refunded")
        )
        n_ok = sum(1 for r in self.records if r.get("status") == "complete")
        n_err = sum(1 for r in self.records if r.get("status") == "error")
        return {
            "steps": len(self.records),
            "completed": n_ok,
            "errors": n_err,
            "gross_cost": round(total_cost, 2),
            "refunded": round(refunded, 2),
            "net_cost": round(total_cost - refunded, 2),
        }
