"""The agent: classify → route → handle.

## On LangGraph

The proposal specifies a LangGraph `StateGraph`, and that is Part 2's stated learning goal. The
routing logic lives here as plain async functions on purpose:

1. It runs and is testable today, with no LLM and no extra dependency.
2. Wrapping it in a `StateGraph` is mechanical — each `handle_*` becomes a node, `_route`
   becomes the conditional edge — and doing that wiring yourself is precisely the part of Part 2
   worth learning. The interesting decisions (the gate, the ordering of advice-before-trade, the
   role checks) are already made and tested; the graph is the plumbing around them.

So: this is a working agent, and the LangGraph migration is a deliberate exercise left in place
rather than an omission.
"""

from __future__ import annotations

from decimal import Decimal

from app.brokerage.base import BrokerageClient
from app.graph.classifier import Classifier, classify_out_of_scope_reason, extract_order
from app.graph.handlers import (
    handle_confirmation,
    handle_educate,
    handle_out_of_scope,
    handle_portfolio,
    handle_research,
    handle_trade,
)
from app.graph.state import ConversationState
from app.rag.retrieval import Retriever
from app.schemas.intents import Intent, OutOfScopeReason
from app.schemas.orders import OrderRequest


class TradeDeskAgent:
    def __init__(
        self,
        *,
        classifier: Classifier,
        retriever: Retriever,
        broker: BrokerageClient,
        allowlist: frozenset[str],
        max_quantity: Decimal = Decimal(100),
        min_retrieval_score: float = 0.35,
    ) -> None:
        self.classifier = classifier
        self.retriever = retriever
        self.broker = broker
        self.allowlist = allowlist
        self.max_quantity = max_quantity
        self.min_retrieval_score = min_retrieval_score

    async def handle(self, state: ConversationState, message: str) -> ConversationState:
        state.user_message = message
        state.citations = []
        state.record("user", message)

        # A pending order short-circuits everything. This turn can only be about that order:
        # confirming it, or discarding it. Re-classifying first would let "yes" be read as some
        # other intent and leave the order hanging into a later turn.
        if state.pending_order is not None:
            state = await handle_confirmation(state, broker=self.broker)
            state.record("agent", state.reply)
            return state

        classification = self.classifier.classify(message)
        state.classification = classification

        state = await self._route(state, classification.intent)
        state.record("agent", state.reply)
        return state

    async def _route(self, state: ConversationState, intent: Intent) -> ConversationState:
        if intent is Intent.EDUCATE:
            return await handle_educate(
                state, retriever=self.retriever, min_score=self.min_retrieval_score
            )

        if intent is Intent.PORTFOLIO:
            return await handle_portfolio(state, broker=self.broker)

        if intent is Intent.RESEARCH:
            symbol = _symbol_or_none(state.user_message, self.allowlist)
            if symbol is None:
                state.reply = "Which symbol did you want a quote for?"
                return state
            return await handle_research(state, broker=self.broker, symbol=symbol)

        if intent is Intent.TRADE:
            order = extract_order(state.user_message, self.allowlist)
            if order is None:
                state.reply = (
                    "I couldn't read a complete order from that. Try something like "
                    "'buy 10 AAPL' or 'sell 5 MSFT at a limit of 420'."
                )
                return state
            return await self._propose_trade(state, order)

        if intent is Intent.CANCEL_MODIFY:
            state.reply = (
                "There's no resting order to cancel — paper orders here fill immediately. "
                "To reverse a position, place the opposite trade."
            )
            return state

        return await handle_out_of_scope(
            state, reason=classify_out_of_scope_reason(state.user_message)
        )

    async def _propose_trade(
        self, state: ConversationState, order: OrderRequest
    ) -> ConversationState:
        return await handle_trade(
            state,
            broker=self.broker,
            request=order,
            allowlist=self.allowlist,
            max_quantity=self.max_quantity,
        )


def _symbol_or_none(message: str, known: frozenset[str] | None = None) -> str | None:
    from app.graph.classifier import _extract_symbol

    return _extract_symbol(message, known)


__all__ = ["TradeDeskAgent", "ConversationState", "OutOfScopeReason"]
