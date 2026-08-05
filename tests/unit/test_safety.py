"""Tests for the safety-critical invariants.

These are the claims the README makes and the demo rests on. If any of these fail, the
corresponding claim in the DSN talk is false — which is a much worse outcome than a broken
feature, so they are worth having before the handlers exist.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.auth.roles import AuthorizationError, requires
from app.graph.classifier import KeywordClassifier
from app.schemas.intents import Classification, Intent
from app.schemas.orders import OrderRequest, OrderType, PendingOrder, Side


def _market_order(**overrides: object) -> OrderRequest:
    return OrderRequest(**{"symbol": "AAPL", "side": Side.BUY, "quantity": Decimal(5), **overrides})  # type: ignore[arg-type]


class TestOrderValidation:
    """The LLM fills these fields. Assume every field is adversarial."""

    def test_market_order_summary_states_every_executing_field(self) -> None:
        summary = _market_order().summarize()
        assert "BUY" in summary and "5" in summary and "AAPL" in summary
        # The user consents to this string. Anything affecting execution must appear in it.
        assert "market price" in summary

    def test_limit_order_summary_includes_price(self) -> None:
        order = OrderRequest(
            symbol="NVDA",
            side=Side.SELL,
            quantity=Decimal(2),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("180.50"),
        )
        assert "180.50" in order.summarize()

    def test_limit_order_without_price_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _market_order(order_type=OrderType.LIMIT)

    def test_market_order_with_price_is_rejected(self) -> None:
        # Silently ignoring a stray limit_price would execute at market when the user's
        # confirmation mentioned a price. Reject rather than reconcile.
        with pytest.raises(ValidationError):
            _market_order(limit_price=Decimal("100"))

    @pytest.mark.parametrize("symbol", ["aapl", "TOOLONG", "AA PL", "", "AA.PL"])
    def test_malformed_symbols_are_rejected(self, symbol: str) -> None:
        with pytest.raises(ValidationError):
            _market_order(symbol=symbol)

    @pytest.mark.parametrize("quantity", [Decimal(0), Decimal(-3), Decimal("10001")])
    def test_out_of_range_quantities_are_rejected(self, quantity: Decimal) -> None:
        with pytest.raises(ValidationError):
            _market_order(quantity=quantity)


class TestIdempotency:
    def test_separate_proposals_get_separate_ids(self) -> None:
        order = _market_order()
        assert (
            PendingOrder(request=order).client_order_id
            != PendingOrder(request=order).client_order_id
        )

    def test_id_is_stable_when_reused_across_a_retry(self) -> None:
        original = PendingOrder(request=_market_order())
        retry = PendingOrder(request=original.request, client_order_id=original.client_order_id)
        # This is what stops a timeout-hidden fill from becoming a double-buy.
        assert retry.client_order_id == original.client_order_id


class TestClassification:
    def test_mutating_intents_are_flagged(self) -> None:
        for intent in (Intent.TRADE, Intent.CANCEL_MODIFY):
            assert Classification(intent=intent, confidence=0.9, reasoning="x").is_mutating

    def test_read_only_intents_are_not_flagged(self) -> None:
        for intent in (Intent.EDUCATE, Intent.RESEARCH, Intent.PORTFOLIO, Intent.OUT_OF_SCOPE):
            assert not Classification(intent=intent, confidence=0.9, reasoning="x").is_mutating

    @pytest.mark.parametrize("confidence", [-0.1, 1.5])
    def test_confidence_must_be_a_probability(self, confidence: float) -> None:
        with pytest.raises(ValidationError):
            Classification(intent=Intent.TRADE, confidence=confidence, reasoning="x")


class TestAuthorization:
    """Part 4's claim: roles are enforced in code, not prompts.

    A jailbroken model can decide to call place_order. These tests assert it gets a 403 anyway.
    """

    @staticmethod
    @requires("trade")
    async def _place(*, role: str) -> str:
        return "executed"

    @staticmethod
    @requires("read_own")
    async def _read(*, role: str) -> str:
        return "read"

    async def test_trader_may_trade(self) -> None:
        assert await self._place(role="trader") == "executed"

    async def test_compliance_may_read(self) -> None:
        assert await self._read(role="compliance") == "read"

    async def test_compliance_may_not_trade(self) -> None:
        with pytest.raises(AuthorizationError):
            await self._place(role="compliance")

    async def test_missing_role_fails_closed(self) -> None:
        # A bug in the role-passing path must deny, never default to permissive.
        with pytest.raises(AuthorizationError):
            await self._place()  # type: ignore[call-arg]

    async def test_unknown_role_fails_closed(self) -> None:
        with pytest.raises(AuthorizationError):
            await self._place(role="superuser")


class TestClassifierReachesTheWholeCorpus:
    """Every corpus topic must be reachable by an ordinary phrasing.

    A question the classifier sends to out_of_scope never reaches the retriever, so a keyword
    gap makes the agent refuse a document it actually has. These broke once: "fee" did not
    match "fees", "dividend" did not match "dividends", "settle" did not match "settlement".
    """

    @pytest.mark.parametrize(
        "message",
        [
            "how does settlement work",
            "what are the fees",
            "how do dividends work",
            "tell me about short selling",
            "explain time in force",
            "what are the risks",
            "what account types are there",
            "what is the pattern day trader rule?",
            "explain margin",
            "what's a limit order",
            "what is the bid ask spread",
            "when are market hours",
        ],
    )
    def test_corpus_topic_routes_to_educate(self, message: str) -> None:
        assert KeywordClassifier().classify(message).intent is Intent.EDUCATE

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            # Broadening educate must not have weakened the checks that run before it.
            ("should I buy NVDA?", Intent.OUT_OF_SCOPE),
            ("is TSLA a good buy right now?", Intent.OUT_OF_SCOPE),
            ("what stock will make me rich?", Intent.OUT_OF_SCOPE),
            ("buy 10 AAPL", Intent.TRADE),
            ("sell 5 MSFT", Intent.TRADE),
            ("show me my positions", Intent.PORTFOLIO),
            ("how much is in my account", Intent.PORTFOLIO),
        ],
    )
    def test_ordering_still_holds(self, message: str, expected: Intent) -> None:
        assert KeywordClassifier().classify(message).intent is expected
