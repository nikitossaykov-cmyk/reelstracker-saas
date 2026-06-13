"""Thin Replicate wrapper.

Classifies exceptions into:
  - ReplicateSafetyError: moderation refused — caller should fall back
    to a defensively-generic prompt before giving up.
  - ReplicateTransientError: 5xx / timeout — caller may retry.
  - ReplicateError: anything else, terminal.

This isolation matters because the persona worker has a one-retry-on-
safety pattern (per the gpt-image-1 fallback skill notes), and the
Strategy E worker classifies transients differently from hard fails.
"""
from __future__ import annotations

import os
import re
from typing import Any


SAFETY_PAT = re.compile(
    r"(nsfw|safety|moderat|policy|content[_ ]filter|inappropriate)",
    re.IGNORECASE,
)
TRANSIENT_PAT = re.compile(
    r"(timeout|timed out|502|503|504|temporarily unavailable|gateway)",
    re.IGNORECASE,
)


class ReplicateError(Exception):
    pass


class ReplicateSafetyError(ReplicateError):
    pass


class ReplicateTransientError(ReplicateError):
    pass


class ReplicateClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Replicate api_key is required")
        self.api_key = api_key

    def run_model(self, ref: str, params: dict[str, Any]):
        os.environ["REPLICATE_API_TOKEN"] = self.api_key
        import replicate
        try:
            return replicate.run(ref, input=params)
        except Exception as e:
            msg = str(e)
            if SAFETY_PAT.search(msg):
                raise ReplicateSafetyError(msg) from e
            if TRANSIENT_PAT.search(msg):
                raise ReplicateTransientError(msg) from e
            raise ReplicateError(msg) from e
