"""LLM-backed intent classifier — the real Part 2 classifier.

`KeywordClassifier` runs the offline demo; this is what ships. Both satisfy the same `Classifier`
protocol, so swapping one for the other is a single line in `build_agent_for` / the demo — the
graph, the gate, and every safety property are untouched.

## Why structured output, not free-text parsing

The classifier's result reaches the order path: an `intent` of TRADE sends the turn toward a
fill. Parsing the model's prose with a regex there means a phrasing the regex didn't anticipate
silently becomes the wrong intent. So the model is constrained to emit JSON validated against the
`Classification` Pydantic schema via `messages.parse()` — validation happens in the SDK, and a
result that doesn't fit the schema is a failure we handle, never a guess we run with.

## Fail closed

Any failure — a refusal, a malformed response, a validation error, a network error — resolves to
`OUT_OF_SCOPE`. That is the harmless branch: it declines and redirects. The one thing a broken
classifier must never do is fall through to TRADE, so the except clause routes the other way.
"""

from __future__ import annotations

import os
from typing import Any, Protocol, cast

from app.schemas.intents import Classification, Intent


class _MessagesClient(Protocol):
    """The slice of the Anthropic client this classifier uses.

    Typed as a Protocol so a fake client in tests, and the real `anthropic.Anthropic()`, are both
    accepted without importing the SDK here — the SDK is only needed when a real client is built.
    """

    class _Messages(Protocol):
        def parse(self, **kwargs: Any) -> Any: ...

    @property
    def messages(self) -> _Messages: ...


#: Default model. The skill's guidance is firm — use Opus 4.8 unless the user names another model;
#: never downgrade for cost, that's their decision. Overridable via MODEL_ID for anyone who wants
#: Sonnet/Haiku for latency or cost on a high-volume deployment.
DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM = """\
You are the intent classifier for TradeDesk, a paper-trading assistant. Classify each user
message into exactly one of six intents. Return only the structured fields requested.

The six intents:

- educate: the user wants to LEARN a trading concept — "what is a limit order?", "how does
  settlement work?", "explain the PDT rule". Definitional and how-does-it-work questions.
- research: the user wants a QUOTE or current price for a specific symbol — "what's AAPL
  trading at?", "price of NVDA".
- portfolio: the user wants to see their own account — positions, holdings, P&L, cash, buying
  power.
- trade: the user wants to PLACE, buy, or sell a specific order — "buy 10 AAPL", "sell 5 MSFT
  at a limit of 420". There must be an actionable instruction to transact.
- cancel_modify: the user wants to cancel or change an existing order.
- out_of_scope: everything else. This includes two important cases:
    1. ADVICE-SEEKING — "should I buy NVDA?", "is TSLA a good investment?", "will AAPL go up?",
       "what should I invest in?". These ask for a recommendation. Classify them out_of_scope,
       NOT trade. Recommending securities is financial advice, which this assistant must not
       give. This distinction is the most important call you make: a request for advice that
       mentions a ticker and a verb like "buy" is still advice, not an order.
    2. Anything unrelated to trading.

Rules:
- "Should I buy X?" is out_of_scope (advice). "Buy 10 X" is trade (an instruction). The presence
  of a ticker or the word "buy" does not make something a trade — only an actionable instruction
  to transact does.
- confidence is your calibrated probability in [0, 1] that the intent is correct.
- reasoning is one short sentence, for developer traces only. The user never sees it.
"""


class LLMClassifier:
    """Classifies intent with Claude, validated against the `Classification` schema.

    The Anthropic client is injected so tests can pass a fake and this module needs no API key or
    network to exercise its logic. A real client (`anthropic.Anthropic()`) resolves credentials
    from the environment.

    Note: `classify` is synchronous to match the `Classifier` protocol and the `KeywordClassifier`
    it stands in for, so it uses the synchronous SDK client. Inside the async graph that means one
    blocking call per turn — fine for the single-user DSN demo; a high-concurrency deployment would
    move to `AsyncAnthropic` and an async protocol.
    """

    def __init__(self, *, model: str | None = None, client: _MessagesClient | None = None) -> None:
        self.model = model or os.getenv("MODEL_ID", DEFAULT_MODEL)
        if client is not None:
            self._client: _MessagesClient = client
        else:
            import anthropic

            # The real client's `messages.parse` has fully typed params, not **kwargs, so it
            # doesn't match the loose Protocol structurally — cast asserts it conforms to the
            # narrow slice we actually call.
            self._client = cast(_MessagesClient, anthropic.Anthropic())

    def classify(self, message: str) -> Classification:
        text = message.strip()
        if not text:
            return Classification(
                intent=Intent.OUT_OF_SCOPE, confidence=1.0, reasoning="empty message"
            )

        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM,
                messages=[{"role": "user", "content": text}],
                output_format=Classification,
            )
        except Exception as exc:  # noqa: BLE001 — any failure must fail closed, see module docstring
            return _fail_closed(f"classifier call failed: {type(exc).__name__}")

        result = getattr(response, "parsed_output", None)
        if not isinstance(result, Classification):
            # A refusal or an unparseable response. Do not guess an intent — decline.
            return _fail_closed("classifier returned no valid structured output")

        return result


def _fail_closed(reason: str) -> Classification:
    return Classification(intent=Intent.OUT_OF_SCOPE, confidence=0.0, reasoning=reason)
