"""Handler nodes — one per intent, plus the confirmation branch.

Each handler takes state and returns state. They are plain async functions rather than LangGraph
node objects so they can be tested directly; `build.py` wires them into the StateGraph.
"""

from __future__ import annotations

from decimal import Decimal

from app.auth.roles import AuthorizationError
from app.brokerage.base import BrokerageClient, BrokerageError
from app.graph.confirmation import is_confirmation, is_rejection
from app.graph.state import ConversationState
from app.graph.tools import (
    get_account,
    get_positions,
    get_quote,
    place_order,
    validate_order_against_account,
)
from app.rag.retrieval import Retriever
from app.schemas.intents import OutOfScopeReason, Rejection
from app.schemas.orders import OrderRequest, PendingOrder

#: Below this retrieval score, say "not in my docs" rather than answering from a weak match.
DEFAULT_MIN_SCORE = 0.35


async def handle_educate(
    state: ConversationState, *, retriever: Retriever, min_score: float = DEFAULT_MIN_SCORE
) -> ConversationState:
    """RAG answer, or an honest refusal.

    The refusal branch is the point. A retriever always returns its nearest chunks however
    irrelevant, so without a threshold the agent would answer "what's the capital of France?"
    using whichever brokerage doc happened to score highest — confidently, and from nothing.
    """
    results = retriever.search(state.user_message, k=3)

    if not results or results[0].score < min_score:
        state.reply = (
            "That isn't covered in my documentation, so I'd rather not guess. I can explain "
            "order types, settlement, the pattern day trader rule, fees, and market hours."
        )
        state.citations = []
        return state

    # Only pass chunks near the best match. A weak third result adds noise the model may
    # nonetheless weave into the answer.
    best = results[0].score
    keep = [r for r in results if r.score >= best * 0.6]

    context = "\n\n---\n\n".join(r.chunk.to_embedding_text() for r in keep)
    state.reply = _format_educate_reply(context, keep[0].chunk.citation)
    state.citations = [r.chunk.citation for r in keep]
    return state


def _format_educate_reply(context: str, primary_citation: str) -> str:
    """Assemble the grounded answer.

    Currently returns the retrieved text directly. The LLM call that turns this into prose goes
    here — and when it does, the prompt must say to answer ONLY from the context and to say so
    when the context doesn't cover it. A model given context plus a question will otherwise
    happily supplement from its own memory, which silently defeats the whole retrieval step.
    """
    return f"{context}\n\n(Source: {primary_citation})"


async def handle_research(
    state: ConversationState, *, broker: BrokerageClient, symbol: str
) -> ConversationState:
    try:
        quote = await get_quote(broker, symbol, role=state.role)
    except BrokerageError as exc:
        state.reply = str(exc)
        return state
    state.reply = (
        f"{quote.symbol} is trading at ${quote.last} "
        f"(bid ${quote.bid:.2f} / ask ${quote.ask:.2f}). "
        f"This is simulated paper-trading data."
    )
    return state


async def handle_portfolio(
    state: ConversationState, *, broker: BrokerageClient
) -> ConversationState:
    try:
        positions = await get_positions(broker, role=state.role)
        account = await get_account(broker, role=state.role)
    except AuthorizationError as exc:
        state.reply = str(exc)
        return state
    except BrokerageError as exc:
        state.reply = str(exc)
        return state

    if not positions:
        state.reply = (
            f"You have no open positions. Cash: ${account.cash:,.2f}, "
            f"buying power: ${account.buying_power:,.2f}."
        )
        return state

    lines = [
        f"- {p.symbol}: {p.quantity} @ ${p.avg_entry_price:.2f} avg "
        f"(value ${p.market_value:,.2f}, unrealized {'+' if p.unrealized_pl >= 0 else ''}"
        f"${p.unrealized_pl:,.2f})"
        for p in positions
    ]
    state.reply = (
        "Your positions:\n"
        + "\n".join(lines)
        + f"\n\nPortfolio value: ${account.portfolio_value:,.2f} "
        f"(cash ${account.cash:,.2f})"
    )
    return state


async def handle_trade(
    state: ConversationState,
    *,
    broker: BrokerageClient,
    request: OrderRequest,
    allowlist: frozenset[str],
    max_quantity: Decimal,
) -> ConversationState:
    """Parse an order and ask for confirmation. **Never executes.**

    The only path to `place_order` is `handle_confirmation`. If this function ever calls the
    broker, the gate is gone.
    """
    try:
        problems = await validate_order_against_account(
            broker, request, role=state.role, allowlist=allowlist, max_quantity=max_quantity
        )
    except AuthorizationError as exc:
        # e.g. the compliance persona attempting to trade. Refuse before any echo — offering to
        # confirm an order the role can never place is a lie about what will happen next.
        state.reply = str(exc)
        state.pending_order = None
        return state
    except BrokerageError as exc:
        state.reply = str(exc)
        state.pending_order = None
        return state

    if problems:
        state.reply = "I can't place that order:\n" + "\n".join(f"- {p}" for p in problems)
        state.pending_order = None
        return state

    quote = await get_quote(broker, request.symbol, role=state.role)
    reference = request.limit_price if request.limit_price is not None else quote.ask
    estimated = request.quantity * reference

    # client_order_id is generated HERE, at proposal time, and reused on every retry — so a
    # timeout that hides a successful fill cannot cause a double-buy.
    state.pending_order = PendingOrder(
        request=request, quoted_price=quote.last, estimated_cost=estimated
    )
    # Quantize for display only. The Decimal above stays full-precision for the actual
    # arithmetic — rounding money before you're done computing with it is how pennies vanish.
    state.reply = (
        f"Just to confirm: {request.summarize()}\n"
        f"At the current ask of ${reference:.2f}, that's about ${estimated:,.2f}.\n\n"
        f"Reply 'yes' to place it. Anything else cancels."
    )
    return state


async def handle_confirmation(
    state: ConversationState, *, broker: BrokerageClient
) -> ConversationState:
    """The confirmed branch — the ONLY place an order executes."""
    pending = state.pending_order
    if pending is None:
        state.reply = "There's no order waiting for confirmation."
        return state

    # Clear first, unconditionally. Whatever happens below — fill, refusal, or an exception —
    # this order must not survive into another turn where a later "yes" could re-trigger it.
    state.pending_order = None

    if not is_confirmation(state.user_message):
        if is_rejection(state.user_message):
            state.reply = "Cancelled — no order was placed."
        else:
            state.reply = (
                "I didn't read that as a confirmation, so I've cancelled the order. "
                "Nothing was placed. Ask again if you'd like to retry."
            )
        return state

    try:
        result = await place_order(
            broker, pending.request, pending.client_order_id, role=state.role
        )
    except AuthorizationError as exc:
        state.reply = str(exc)
        return state
    except BrokerageError as exc:
        state.reply = f"The order didn't go through: {exc}"
        return state

    state.reply = (
        f"Filled: {result.filled_quantity} share(s) of {pending.request.symbol} "
        f"at ${result.filled_avg_price}. Order ID {result.order_id}. "
        f"(Paper trading — no real money moved.)"
    )
    return state


async def handle_out_of_scope(
    state: ConversationState, *, reason: OutOfScopeReason
) -> ConversationState:
    """Refuse, and redirect.

    ADVICE_SEEKING is the one that matters. "Should I buy NVDA?" is the single most natural
    question to ask this agent and the one it must never answer — that's unlicensed financial
    advice. Redirecting to education is the product working as designed, not a gap in it.
    """
    rejections = {
        OutOfScopeReason.ADVICE_SEEKING: Rejection(
            reason=reason,
            message=(
                "I can't tell you what to buy or sell — that would be financial advice, and I'm "
                "a learning tool, not an advisor. What I can do is explain how to think about "
                "it: what order types do, how costs eat returns, what the risks are."
            ),
            suggested_topic="how to evaluate a trade",
        ),
        OutOfScopeReason.UNSUPPORTED_ASSET: Rejection(
            reason=reason,
            message="This paper account only supports a small list of US equities.",
            suggested_topic="what you can trade here",
        ),
        OutOfScopeReason.UNRELATED: Rejection(
            reason=reason,
            message=(
                "That's outside what I do. I'm here for trading concepts, quotes, your paper "
                "portfolio, and simulated orders."
            ),
            suggested_topic=None,
        ),
    }
    rejection = rejections[reason]
    state.reply = rejection.message
    if rejection.suggested_topic:
        state.reply += f" Want me to explain {rejection.suggested_topic}?"
    return state
