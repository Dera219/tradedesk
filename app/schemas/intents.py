"""Intent taxonomy.

The classifier returns structured JSON validated against these models. It is never asked to
return free text that we then parse with regex — a regex over model output fails silently and
unpredictably the first time the model phrases something differently, and here a misparse can
reach the order path.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Intent(StrEnum):
    """What the user is asking for.

    OUT_OF_SCOPE is not a fallback bucket for parse failures — it is a real, deliberate intent.
    It catches advice-seeking ("should I buy NVDA?", "is this a good entry?") and routes to
    education instead of answering. A paper-trading tutor that answers those questions is giving
    unlicensed financial advice, so the redirect is the product working, not the product failing.
    """

    EDUCATE = "educate"
    RESEARCH = "research"
    PORTFOLIO = "portfolio"
    TRADE = "trade"
    CANCEL_MODIFY = "cancel_modify"
    OUT_OF_SCOPE = "out_of_scope"


#: Intents that can change account state. These require the confirmation gate before execution.
MUTATING_INTENTS: frozenset[Intent] = frozenset({Intent.TRADE, Intent.CANCEL_MODIFY})


class Classification(BaseModel):
    """The classifier node's output."""

    model_config = {"frozen": True}

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(
        max_length=500,
        description="Why this intent. Surfaced in traces for the demo, never shown to the user.",
    )

    @property
    def is_mutating(self) -> bool:
        return self.intent in MUTATING_INTENTS


class OutOfScopeReason(StrEnum):
    ADVICE_SEEKING = "advice_seeking"
    UNRELATED = "unrelated"
    UNSUPPORTED_ASSET = "unsupported_asset"


class Rejection(BaseModel):
    """A refusal the user actually sees. Kept structured so the reply is consistent."""

    model_config = {"frozen": True}

    reason: OutOfScopeReason
    message: str
    suggested_topic: str | None = Field(
        default=None,
        description="Education topic to redirect toward, e.g. 'how to evaluate a stock'.",
    )


Role = Literal["trader", "compliance"]
