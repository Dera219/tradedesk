"""Brokerage interface.

MockBroker and AlpacaBroker both implement this. That is what lets the DSN demo run offline if
the venue wifi dies or Alpaca has an outage — swap one constructor call, everything else is
unchanged. Build against this interface from day one rather than retrofitting it in Part 3;
retrofitting an abstraction after the concrete code exists is how the mock ends up subtly
diverging from the real thing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.orders import OrderRequest


class Quote(BaseModel):
    model_config = {"frozen": True}

    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal


class Position(BaseModel):
    model_config = {"frozen": True}

    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal


class Account(BaseModel):
    model_config = {"frozen": True}

    buying_power: Decimal
    cash: Decimal
    portfolio_value: Decimal
    #: Alpaca flags accounts restricted by the pattern-day-trader rule. Surfacing it matters:
    #: it's both a real constraint on what can execute and one of the concepts in the RAG corpus.
    pattern_day_trader: bool = False


class OrderResult(BaseModel):
    model_config = {"frozen": True}

    order_id: str
    client_order_id: str
    status: str
    filled_quantity: Decimal = Decimal(0)
    filled_avg_price: Decimal | None = None


class BrokerageError(Exception):
    """Base for broker failures.

    Handler nodes catch this and reply conversationally. An unhandled broker exception surfacing
    as a stack trace in a chat UI is a bad demo moment and an easy one to avoid.
    """


class InsufficientFunds(BrokerageError): ...


class MarketClosed(BrokerageError): ...


class SymbolNotTradable(BrokerageError): ...


class BrokerageClient(ABC):
    """Read methods are safe to call freely. `submit_order` must only ever be reached from the
    confirmed branch of the trade node — never speculatively, never to 'preview' a fill."""

    @abstractmethod
    async def get_account(self) -> Account: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def submit_order(self, request: OrderRequest, client_order_id: str) -> OrderResult:
        """Submit a confirmed order.

        Implementations MUST pass client_order_id through to the venue for idempotency, and MUST
        re-validate server-side rather than trusting that validation already happened upstream.
        """

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...
