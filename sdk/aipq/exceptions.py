"""Exceptions raised by the AIPQ SDK."""
from __future__ import annotations

from typing import Any


class AIPQError(Exception):
    """Base class for all AIPQ SDK errors."""


class PromptQualityError(AIPQError):
    """
    Raised when a changed prompt fails its golden-dataset evaluation.

    Blocks the calling application from starting with an unvalidated prompt —
    this is the whole point of the quality gate, so this exception is never
    silently swallowed by the SDK itself.
    """

    def __init__(self, prompt_name: str, score: float, threshold: float, details: dict[str, Any]):
        self.prompt_name = prompt_name
        self.score = score
        self.threshold = threshold
        self.details = details
        failed_cases = details.get("failed_case_ids", [])
        super().__init__(
            f"Prompt '{prompt_name}' scored {score:.2f} (threshold {threshold:.2f}) — "
            f"deployment blocked. {len(failed_cases)} case(s) failed: {failed_cases}"
        )
