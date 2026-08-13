"""In-memory broker.

Part 2 runs against this. Because it implements `BrokerageClient`, swapping in Alpaca for Part 3
touches one constructor call — and the DSN demo still runs if the venue wifi dies or Alpaca has
an outage.

It re-validates every order server-side even though the schema already validated and the graph
already gated. That redundancy is the point: upstream validation is a convenience, the boundary
check is the guarantee. A real broker does not trust its client, and a mock that trusts its
caller will let bugs through that the real one would have caught.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal

from app.brokerage.base import (
    Account,
    BrokerageClient,
    BrokerageError,
    InsufficientFunds,
    MarketClosed,
    OrderResult,
    Position,
    Quote,
    SymbolNotTradable,
)
from app.schemas.orders import OrderRequest, Side

#: Deterministic prices so the demo says the same thing twice.
_PRICES: dict[str, Decimal] = {
    "AAPL": Decimal("227.50"),
    "MSFT": Decimal("415.20"),
    "NVDA": Decimal("183.75"),
    "GOOGL": Decimal("178.40"),
    "AMZN": Decimal("219.30"),
    "TSLA": Decimal("342.10"),
    "SPY": Decimal("598.80"),
    "QQQ": Decimal("521.45"),
}

#: US equities regular session, Eastern. Naive approximation — ignores holidays and half-days.
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)


class MockBroker(BrokerageClient):
    def __init__(
        self,
        *,
        cash: Decimal = Decimal("100000"),
        allowlist: frozenset[str] | None = None,
        enforce_market_hours: bool = False,
    ) -> None:
        self._cash = cash
        self._initial_cash = cash
        self._positions: dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (qty, avg_price)
        self._allowlist = allowlist if allowlist is not None else frozenset(_PRICES)
        self._enforce_market_hours = enforce_market_hours
        #: client_order_id -> result, so a retry returns the original fill instead of re-buying.
        self._submitted: dict[str, OrderResult] = {}
        self._order_counter = 0

    async def get_account(self) -> Account:
        portfolio_value = self._cash + sum(
            quantity * _PRICES[symbol] for symbol, (quantity, _) in self._positions.items()
        )
        return Account(
            buying_power=self._cash,
            cash=self._cash,
            portfolio_value=portfolio_value,
            pattern_day_trader=False,
        )

    async def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for symbol, (quantity, avg_price) in sorted(self._positions.items()):
            price = _PRICES[symbol]
            out.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    avg_entry_price=avg_price,
                    market_value=quantity * price,
                    unrealized_pl=(price - avg_price) * quantity,
                )
            )
        return out

    async def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        if symbol not in _PRICES:
            raise SymbolNotTradable(f"{symbol} is not available on this paper account.")
        last = _PRICES[symbol]
        spread = last * Decimal("0.0001")
        return Quote(symbol=symbol, bid=last - spread, ask=last + spread, last=last)

    async def submit_order(self, request: OrderRequest, client_order_id: str) -> OrderResult:
        # Idempotency FIRST, before any validation or state change. A retry after a timeout must
        # return the original fill, not attempt a second one.
        if client_order_id in self._submitted:
            return self._submitted[client_order_id]

        if request.symbol not in self._allowlist:
            raise SymbolNotTradable(
                f"{request.symbol} is not on the tradable allowlist for this account."
            )

        if self._enforce_market_hours and not self._is_market_open():
            raise MarketClosed("The market is closed. US equities trade 9:30am–4:00pm Eastern.")

        price = _PRICES[request.symbol]
        if request.order_type.value == "limit" and request.limit_price is not None:
            # A marketable limit fills at the limit; otherwise it would rest. Filling a
            # non-marketable limit immediately would be a lie that flatters the demo.
            if request.side is Side.BUY and request.limit_price < price:
                raise InsufficientFunds(
                    f"A buy limit of ${request.limit_price} is below the ${price} market price, "
                    f"so it would rest unfilled. Resting orders aren't simulated yet."
                )
            price = request.limit_price

        notional = request.quantity * price

        if request.side is Side.BUY:
            if notional > self._cash:
                raise InsufficientFunds(
                    f"That order costs ${notional:,.2f} but the account only has "
                    f"${self._cash:,.2f} in buying power."
                )
            self._cash -= notional
            held, avg = self._positions.get(request.symbol, (Decimal(0), Decimal(0)))
            new_quantity = held + request.quantity
            # Weighted average entry, so unrealized P&L stays meaningful across adds.
            self._positions[request.symbol] = (
                new_quantity,
                ((held * avg) + notional) / new_quantity,
            )
        else:
            held, avg = self._positions.get(request.symbol, (Decimal(0), Decimal(0)))
            if request.quantity > held:
                raise InsufficientFunds(
                    f"Cannot sell {request.quantity} share(s) of {request.symbol}: "
                    f"the account holds {held}."
                )
            self._cash += notional
            remaining = held - request.quantity
            if remaining == 0:
                del self._positions[request.symbol]
            else:
                self._positions[request.symbol] = (remaining, avg)

        self._order_counter += 1
        result = OrderResult(
            order_id=f"mock-{self._order_counter:06d}",
            client_order_id=client_order_id,
            status="filled",
            filled_quantity=request.quantity,
            filled_avg_price=price,
        )
        self._submitted[client_order_id] = result
        return result

    async def cancel_order(self, order_id: str) -> None:
        # Every mock order fills instantly, so there is never anything resting to cancel.
        raise BrokerageOrderNotFound(
            f"Order {order_id} is not open — mock orders fill immediately."
        )

    @staticmethod
    def _is_market_open(now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if now.weekday() >= 5:
            return False
        # Rough ET conversion. Real code should use zoneinfo; this mock only needs to be
        # honest about being an approximation.
        eastern_hour = (now.hour - 5) % 24
        current = time(eastern_hour, now.minute)
        return _MARKET_OPEN <= current <= _MARKET_CLOSE


class BrokerageOrderNotFound(BrokerageError):
    """Raised when cancelling something that isn't open.

    Subclasses BrokerageError so the mock matches the Alpaca client, which raises
    BrokerageError for the same condition — handlers written against one broker must catch
    the other's failures too, or the swap the interface promises quietly breaks them.
    """
