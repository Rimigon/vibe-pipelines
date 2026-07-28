"""vibe-pipelines: resilient multi-model generation pipelines over VibeMarketolog Agent API."""

from __future__ import annotations

from .client import VibeClient
from .errors import BudgetExceeded, FieldMappingError, VibeError
from .pipeline import Pipeline
from .steps import Step

__all__ = [
    "VibeClient",
    "VibeError",
    "BudgetExceeded",
    "FieldMappingError",
    "Pipeline",
    "Step",
]

__version__ = "0.1.0"
