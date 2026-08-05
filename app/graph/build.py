"""LangGraph StateGraph wiring — Part 2.

```
                    START
                      │
            ┌─────────┴──────────┐
            │  is an order       │      <- conditional entry point
            │  awaiting confirm? │
            └─────────┬──────────┘
                 yes  │  no
          ┌───────────┘  └────────────┐
          ▼                           ▼
    confirmation                classify_intent
    (the ONLY path                    │
     to place_order)          ┌───────┴────────┐
          │                   │  route_intent  │
          │                   └───────┬────────┘
          │        ┌──────────┬───────┼────────┬──────────────┐
          │        ▼          ▼       ▼        ▼              ▼
          │     educate   research  portfolio  trade    out_of_scope
          │      (RAG)    (quotes)  (positions) (GATED)  (redirect)
          │        │          │       │        │              │
          └────────┴──────────┴───────┴────────┴──────────────┘
                                  │
                                 END
```

## Two decisions worth naming

**The pending-order check is the conditional ENTRY point, not a node after classification.**
If classification ran first, "yes" would be handed to the classifier, which would route it
somewhere by its own logic — and the order would sit in state waiting for a later turn to
resurrect it. Gating before classification means a pending order makes the turn about that order
and nothing else. The graph shape enforces the one-turn lifetime.

**`place_order` is reachable from exactly one node.** `trade` proposes and stores; only
`confirmation` executes. That is visible in the diagram above, which is the point of drawing the
graph at all — you can see that there is no edge from `trade` to a fill.

## On state updates

LangGraph nodes return a dict of *changed fields*, and `ainvoke` returns a dict rather than the
dataclass. `_updates()` exists so handlers stay plain mutating functions (directly testable, no
graph needed) while the nodes stay pure-looking to LangGraph. Returning `None` for
`pending_order` genuinely clears it — verified, and load-bearing for the gate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from langgraph.graph import END, START, StateGraph

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
from app.schemas.intents import Intent

#: Node names. Kept as constants because a typo in an `add_edge` string is a silent
#: mis-wiring — the graph compiles and routes somewhere unintended.
CLASSIFY = "classify_intent"
CONFIRMATION = "confirmation"
EDUCATE = "educate"
RESEARCH = "research"
PORTFOLIO = "portfolio"
TRADE = "trade"
CANCEL_MODIFY = "cancel_modify"
OUT_OF_SCOPE = "out_of_scope"


def _updates(state: ConversationState) -> dict[str, Any]:
    """Extract the fields a handler may have changed.

    `pending_order` is included unconditionally, including when it is None — that None IS the
    gate clearing itself, and omitting it would leave a stale order alive in the channel.
    """
    return {
        "reply": state.reply,
        "citations": state.citations,
        "pending_order": state.pending_order,
        "history": state.history,
    }


def build_graph(
    *,
    classifier: Classifier,
    retriever: Retriever,
    broker: BrokerageClient,
    allowlist: frozenset[str],
    max_quantity: Decimal = Decimal(100),
    min_retrieval_score: float = 0.35,
) -> Any:
    """Compile the agent graph.

    Dependencies are closed over rather than stuffed into state: a broker connection is not
    conversation state, and putting it in the channel dict would make it part of every
    checkpoint LangGraph serializes.
    """

    # --- entry routing ------------------------------------------------------------------

    def entry_router(state: ConversationState) -> str:
        # A pending order short-circuits everything. See the module docstring.
        return CONFIRMATION if state.pending_order is not None else CLASSIFY

    # --- nodes --------------------------------------------------------------------------

    async def classify_intent(state: ConversationState) -> dict[str, Any]:
        classification = classifier.classify(state.user_message)
        return {"classification": classification}

    def route_intent(state: ConversationState) -> str:
        if state.classification is None:
            # Unreachable via the graph, but a wrong answer here reaches the order path, so
            # fail to the harmless branch rather than assuming.
            return OUT_OF_SCOPE
        return {
            Intent.EDUCATE: EDUCATE,
            Intent.RESEARCH: RESEARCH,
            Intent.PORTFOLIO: PORTFOLIO,
            Intent.TRADE: TRADE,
            Intent.CANCEL_MODIFY: CANCEL_MODIFY,
            Intent.OUT_OF_SCOPE: OUT_OF_SCOPE,
        }[state.classification.intent]

    async def educate_node(state: ConversationState) -> dict[str, Any]:
        await handle_educate(state, retriever=retriever, min_score=min_retrieval_score)
        return _updates(state)

    async def research_node(state: ConversationState) -> dict[str, Any]:
        from app.graph.classifier import _extract_symbol

        symbol = _extract_symbol(state.user_message, allowlist)
        if symbol is None:
            state.reply = "Which symbol did you want a quote for?"
            return _updates(state)
        await handle_research(state, broker=broker, symbol=symbol)
        return _updates(state)

    async def portfolio_node(state: ConversationState) -> dict[str, Any]:
        await handle_portfolio(state, broker=broker)
        return _updates(state)

    async def trade_node(state: ConversationState) -> dict[str, Any]:
        order = extract_order(state.user_message, allowlist)
        if order is None:
            # Refuse rather than guess. A defaulted quantity invents an order nobody placed.
            state.reply = (
                "I couldn't read a complete order from that. Try something like "
                "'buy 10 AAPL' or 'sell 5 MSFT at a limit of 420'."
            )
            state.pending_order = None
            return _updates(state)
        await handle_trade(
            state,
            broker=broker,
            request=order,
            allowlist=allowlist,
            max_quantity=max_quantity,
        )
        return _updates(state)

    async def cancel_modify_node(state: ConversationState) -> dict[str, Any]:
        state.reply = (
            "There's no resting order to cancel — paper orders here fill immediately. "
            "To reverse a position, place the opposite trade."
        )
        return _updates(state)

    async def out_of_scope_node(state: ConversationState) -> dict[str, Any]:
        await handle_out_of_scope(state, reason=classify_out_of_scope_reason(state.user_message))
        return _updates(state)

    async def confirmation_node(state: ConversationState) -> dict[str, Any]:
        await handle_confirmation(state, broker=broker)
        return _updates(state)

    # --- wiring -------------------------------------------------------------------------

    graph = StateGraph(ConversationState)

    graph.add_node(CLASSIFY, classify_intent)
    graph.add_node(CONFIRMATION, confirmation_node)
    graph.add_node(EDUCATE, educate_node)
    graph.add_node(RESEARCH, research_node)
    graph.add_node(PORTFOLIO, portfolio_node)
    graph.add_node(TRADE, trade_node)
    graph.add_node(CANCEL_MODIFY, cancel_modify_node)
    graph.add_node(OUT_OF_SCOPE, out_of_scope_node)

    graph.add_conditional_edges(
        START, entry_router, {CONFIRMATION: CONFIRMATION, CLASSIFY: CLASSIFY}
    )
    graph.add_conditional_edges(
        CLASSIFY,
        route_intent,
        {
            EDUCATE: EDUCATE,
            RESEARCH: RESEARCH,
            PORTFOLIO: PORTFOLIO,
            TRADE: TRADE,
            CANCEL_MODIFY: CANCEL_MODIFY,
            OUT_OF_SCOPE: OUT_OF_SCOPE,
        },
    )

    for terminal in (
        CONFIRMATION,
        EDUCATE,
        RESEARCH,
        PORTFOLIO,
        TRADE,
        CANCEL_MODIFY,
        OUT_OF_SCOPE,
    ):
        graph.add_edge(terminal, END)

    return graph.compile()


class GraphAgent:
    """`TradeDeskAgent`'s interface, backed by the compiled StateGraph.

    Drop-in with the hand-rolled agent on purpose: the same tests and the same demo run against
    both, so "we migrated to LangGraph" is a claim backed by a passing suite rather than a hope.
    """

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
        self.broker = broker
        self.allowlist = allowlist
        self._graph = build_graph(
            classifier=classifier,
            retriever=retriever,
            broker=broker,
            allowlist=allowlist,
            max_quantity=max_quantity,
            min_retrieval_score=min_retrieval_score,
        )

    async def handle(self, state: ConversationState, message: str) -> ConversationState:
        state.user_message = message
        state.citations = []
        # Clear the previous turn's classification too. On a confirmation turn the entry router
        # skips the classifier entirely, so a stale value here would report an intent that was
        # never computed — the API surfaces this field, and state that misreports what happened
        # is worse than state that says nothing.
        state.classification = None
        state.record("user", message)

        result = await self._graph.ainvoke(state)

        # ainvoke returns a channel dict, not the dataclass.
        new_state = ConversationState(**result)
        new_state.record("agent", new_state.reply)
        return new_state

    def edges(self) -> set[tuple[str, str]]:
        """(source, target) pairs of the compiled graph.

        Exposed so the wiring can be asserted structurally rather than by reading a picture.
        The claim worth pinning is that `trade` has no edge to `confirmation`: proposing an
        order cannot flow into filling it within a single turn.
        """
        graph = self._graph.get_graph()
        return {(edge.source, edge.target) for edge in graph.edges}

    def draw(self) -> str:
        """ASCII diagram of the compiled graph, generated from the real wiring so it can't drift
        from what actually runs. Useful for the DSN slide.

        Requires the optional `grandalf` package (`pip install grandalf`). It is not a runtime
        dependency — nothing in the agent path calls this — so it stays optional rather than
        adding weight to the deploy for the sake of one diagram.
        """
        try:
            return str(self._graph.get_graph().draw_ascii())
        except ImportError as exc:
            raise ImportError(
                "draw() needs grandalf: pip install grandalf. "
                "Use edges() for a dependency-free view of the wiring."
            ) from exc
