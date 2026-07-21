"""Part 5: TradeDesk as an MCP server.

Any MCP client (Claude Desktop, Claude Code, another agent) gets TradeDesk's tools — quotes,
account, positions, doc search, and paper trading. Run it:

    python -m app.mcp_server                     # stdio transport, mock broker
    TRADEDESK_BROKER=alpaca python -m app.mcp_server   # real Alpaca paper account

## The gate survives the protocol

The chat UI enforces confirmation conversationally; MCP has no conversation, so the gate is
rebuilt as a two-tool handshake:

1. `propose_order` validates and returns an echo + a one-time confirmation token.
2. `confirm_order` executes ONLY with that exact token, once, within its TTL.

There is deliberately no single tool that goes from intent to fill. A model driving this server
cannot skip the pause — the token forces a second, separate decision, and its description tells
the client to put that decision to the human. Proposals are single-use, expire in 120 seconds,
and a new proposal replaces the old one — the same one-live-order rule as ConversationState.

## Roles apply here too

`TRADEDESK_MCP_ROLE=compliance` starts the server read-only: `propose_order` and
`confirm_order` refuse before touching the broker, enforced by the same `@requires` decorators
as the chat path. Prompts are UX; code is security — same sentence, new transport.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from mcp.server.fastmcp import FastMCP

from app.auth.roles import AuthorizationError
from app.brokerage.base import BrokerageClient, BrokerageError
from app.graph.tools import (
    get_account as _account,
)
from app.graph.tools import (
    get_positions as _positions,
)
from app.graph.tools import (
    get_quote as _quote,
)
from app.graph.tools import (
    place_order as _place,
)
from app.graph.tools import (
    validate_order_against_account as _validate,
)
from app.rag.chunking import chunk_corpus
from app.rag.retrieval import LexicalRetriever
from app.schemas.intents import Role
from app.schemas.orders import OrderRequest, OrderType, Side, TimeInForce

ALLOWLIST = frozenset({"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "SPY", "QQQ"})
MAX_QUANTITY = Decimal(100)
PROPOSAL_TTL_SECONDS = 120.0


@dataclass
class Proposal:
    request: OrderRequest
    token: str
    created_at: float

    def expired(self, *, now: float | None = None) -> bool:
        return ((now or time.monotonic()) - self.created_at) > PROPOSAL_TTL_SECONDS


class TradeDeskService:
    """The tool implementations, separated from FastMCP registration so tests can call them
    directly — the transport is not the interesting part to test, the gate is."""

    def __init__(
        self,
        broker: BrokerageClient,
        retriever: LexicalRetriever,
        *,
        role: Role = "trader",
    ) -> None:
        self.broker = broker
        self.retriever = retriever
        self.role = role
        #: At most ONE live proposal, exactly like ConversationState.pending_order.
        self.pending: Proposal | None = None

    # ---- reads ----

    async def quote(self, symbol: str) -> str:
        try:
            q = await _quote(self.broker, symbol.upper(), role=self.role)
        except BrokerageError as exc:
            return str(exc)
        return f"{q.symbol}: last ${q.last}, bid ${q.bid}, ask ${q.ask}"

    async def account(self) -> str:
        a = await _account(self.broker, role=self.role)
        pdt = " (flagged: pattern day trader)" if a.pattern_day_trader else ""
        return (
            f"Paper account — cash ${a.cash:,.2f}, buying power ${a.buying_power:,.2f}, "
            f"portfolio value ${a.portfolio_value:,.2f}{pdt}"
        )

    async def positions(self) -> str:
        held = await _positions(self.broker, role=self.role)
        if not held:
            return "No open positions."
        lines = [
            f"{p.symbol}: {p.quantity} @ ${p.avg_entry_price} avg "
            f"(value ${p.market_value:,.2f}, P&L ${p.unrealized_pl:,.2f})"
            for p in held
        ]
        return "\n".join(lines)

    def search_docs(self, query: str) -> str:
        results = self.retriever.search(query, k=3)
        if not results:
            return "Nothing relevant in the education corpus for that query."
        return "\n\n".join(
            f"[{r.chunk.doc_title} — {' › '.join(r.chunk.heading_path)}]\n{r.chunk.body}"
            for r in results
        )

    # ---- the gate, as two tools ----

    async def propose(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: str,
        order_type: Literal["market", "limit"] = "market",
        limit_price: str | None = None,
        time_in_force: Literal["day", "gtc"] = "day",
    ) -> str:
        # Model output is data to be validated, never a command to be run — same rule as chat.
        try:
            request = OrderRequest(
                symbol=symbol.upper(),
                side=Side(side),
                quantity=Decimal(quantity),
                order_type=OrderType(order_type),
                limit_price=Decimal(limit_price) if limit_price is not None else None,
                time_in_force=TimeInForce(time_in_force),
            )
        except (ValueError, InvalidOperation) as exc:
            self.pending = None
            return f"That order is not valid: {exc}"

        try:
            problems = await _validate(
                self.broker,
                request,
                role=self.role,
                allowlist=ALLOWLIST,
                max_quantity=MAX_QUANTITY,
            )
        except AuthorizationError as exc:
            self.pending = None
            return str(exc)
        except BrokerageError as exc:
            self.pending = None
            return str(exc)

        if problems:
            self.pending = None
            return "I can't propose that order:\n" + "\n".join(f"- {p}" for p in problems)

        # A new proposal always replaces the old one — one live order, ever.
        self.pending = Proposal(
            request=request, token=str(uuid.uuid4()), created_at=time.monotonic()
        )
        return (
            f"PROPOSED (not executed): {request.summarize()}\n"
            f"Show this order to the user and ask for their explicit approval. Only after they "
            f"approve, call confirm_order with token {self.pending.token}. "
            f"The token is single-use and expires in {PROPOSAL_TTL_SECONDS:.0f} seconds."
        )

    async def confirm(self, token: str) -> str:
        proposal = self.pending
        # Fail closed, and burn the proposal on every path out of here: a token that failed
        # once must not be retryable into an execution later.
        if proposal is None:
            return "There is no pending proposal. Nothing was executed. Propose an order first."
        if proposal.expired():
            self.pending = None
            return "That proposal expired. Nothing was executed. Propose the order again."
        if token != proposal.token:
            self.pending = None
            return (
                "That token does not match the pending proposal, which has now been discarded. "
                "Nothing was executed."
            )

        self.pending = None  # single-use, cleared BEFORE the broker call, never after
        try:
            result = await _place(self.broker, proposal.request, str(uuid.uuid4()), role=self.role)
        except AuthorizationError as exc:
            return str(exc)
        except BrokerageError as exc:
            return f"The broker rejected the order: {exc}"

        filled = f" at ${result.filled_avg_price}" if result.filled_avg_price is not None else ""
        return (
            f"Executed: {proposal.request.summarize()} Status: {result.status}, "
            f"{result.filled_quantity} share(s){filled}. Order id {result.order_id}."
        )

    def cancel_proposal(self) -> str:
        if self.pending is None:
            return "There was no pending proposal."
        self.pending = None
        return "Proposal discarded. Nothing was executed."


def build_service() -> TradeDeskService:
    from pathlib import Path

    role: Role = "compliance" if os.getenv("TRADEDESK_MCP_ROLE") == "compliance" else "trader"

    broker: BrokerageClient
    if os.getenv("TRADEDESK_BROKER", "").lower() == "alpaca":
        from app.brokerage.alpaca import AlpacaBroker

        broker = AlpacaBroker()
    else:
        from app.brokerage.mock import MockBroker

        broker = MockBroker(cash=Decimal("100000"), allowlist=ALLOWLIST)

    corpus = Path(__file__).resolve().parent.parent / "corpus"
    retriever = LexicalRetriever(chunk_corpus(corpus))
    return TradeDeskService(broker, retriever, role=role)


def build_server(service: TradeDeskService | None = None) -> FastMCP:
    service = service or build_service()
    mcp = FastMCP(
        "tradedesk",
        instructions=(
            "Paper-trading assistant. No real money. Trades are a two-step handshake: "
            "propose_order returns a token; before calling confirm_order you MUST show the "
            "proposed order to the human user and get their explicit approval. Never call "
            "confirm_order on your own judgment."
        ),
    )

    @mcp.tool(description="Latest quote (bid/ask/last) for a US equity ticker on the allowlist.")
    async def get_quote(symbol: str) -> str:
        return await service.quote(symbol)

    @mcp.tool(description="Paper account balances: cash, buying power, portfolio value.")
    async def get_account() -> str:
        return await service.account()

    @mcp.tool(description="Open paper positions with average entry and unrealized P&L.")
    async def get_positions() -> str:
        return await service.positions()

    @mcp.tool(
        description=(
            "Search TradeDesk's trading-education corpus (PDT rule, order types, margin, fees...)."
        )
    )
    async def search_docs(query: str) -> str:
        return service.search_docs(query)

    @mcp.tool(
        description=(
            "Step 1 of 2: validate and PROPOSE a paper trade. Does NOT execute. Returns an "
            "order echo and a single-use confirmation token. Show the echo to the human user "
            "and ask for approval before ever calling confirm_order."
        )
    )
    async def propose_order(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: str,
        order_type: Literal["market", "limit"] = "market",
        limit_price: str | None = None,
        time_in_force: Literal["day", "gtc"] = "day",
    ) -> str:
        return await service.propose(
            symbol,
            side,
            quantity,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
        )

    @mcp.tool(
        description=(
            "Step 2 of 2: execute the pending proposal. Requires the exact token from "
            "propose_order, only works once, and expires. Call this ONLY after the human user "
            "explicitly approved the proposed order."
        )
    )
    async def confirm_order(token: str) -> str:
        return await service.confirm(token)

    @mcp.tool(description="Discard the pending proposal without executing it.")
    async def cancel_proposal() -> str:
        return service.cancel_proposal()

    return mcp


if __name__ == "__main__":
    build_server().run()
