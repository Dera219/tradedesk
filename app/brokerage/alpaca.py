"""Alpaca paper-trading client — Part 3.

Implements `BrokerageClient` against Alpaca's REST API, so swapping it for `MockBroker` is the
one-constructor change the architecture promised.

## Why the base URL is not configurable

`_TRADING_BASE` is hard-coded to the paper host. There is no parameter, env var, or subclass
hook that points this class at the live-money API: an agent that can execute trades gets the
guarantee written in code, not in configuration that a typo can defeat. If a live client is ever
genuinely needed, that should be a *different class* whose construction is a loud, reviewed
decision — not a string swap.

## Idempotency

`client_order_id` is generated once when an order is proposed (see PendingOrder) and passed
through here. If a network timeout hides a successful fill and the graph retries, Alpaca rejects
the duplicate id — and we treat that rejection as success, fetch the original order, and return
it. A retry can never double-buy.

## Money

Alpaca returns monetary values as JSON strings. They are parsed straight to `Decimal` — floats
never touch account balances.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import httpx

from app.brokerage.base import (
    Account,
    BrokerageClient,
    BrokerageError,
    InsufficientFunds,
    OrderResult,
    Position,
    Quote,
    SymbolNotTradable,
)
from app.schemas.orders import OrderRequest, OrderType

#: Paper trading only. See module docstring before even thinking about changing this.
_TRADING_BASE = "https://paper-api.alpaca.markets"
#: Market data lives on a separate host. The free IEX feed is enough for quotes.
_DATA_BASE = "https://data.alpaca.markets"

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class AlpacaConfigError(BrokerageError):
    """Credentials are missing. Raised at construction, not first use — a demo should fail at
    startup with a clear message, not mid-conversation."""


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise AlpacaConfigError(
            f"{name} is not set. Create a paper-trading key at alpaca.markets and export "
            f"APCA_API_KEY_ID and APCA_API_SECRET_KEY."
        )
    return value


class AlpacaBroker(BrokerageClient):
    """Async client for one Alpaca paper account.

    One instance should be shared across sessions (see app.main): every session talks to the
    same paper account anyway, and sharing reuses the connection pool. `aclose()` at shutdown.

    `transport` exists for tests (httpx.MockTransport) so the full request/response mapping is
    testable without network access or credentials leaving the machine.
    """

    def __init__(
        self,
        *,
        key_id: str | None = None,
        secret_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key_id = key_id or _require_env("APCA_API_KEY_ID")
        secret_key = secret_key or _require_env("APCA_API_SECRET_KEY")
        headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
        }
        self._trading = httpx.AsyncClient(
            base_url=_TRADING_BASE, headers=headers, timeout=_TIMEOUT, transport=transport
        )
        self._data = httpx.AsyncClient(
            base_url=_DATA_BASE, headers=headers, timeout=_TIMEOUT, transport=transport
        )

    async def aclose(self) -> None:
        await self._trading.aclose()
        await self._data.aclose()

    # ---- reads ------------------------------------------------------------------

    async def get_account(self) -> Account:
        payload = await self._get_json(self._trading, "/v2/account")
        return Account(
            buying_power=Decimal(payload["buying_power"]),
            cash=Decimal(payload["cash"]),
            portfolio_value=Decimal(payload["portfolio_value"]),
            pattern_day_trader=bool(payload.get("pattern_day_trader", False)),
        )

    async def get_positions(self) -> list[Position]:
        payload = await self._get_json(self._trading, "/v2/positions")
        return [
            Position(
                symbol=p["symbol"],
                quantity=Decimal(p["qty"]),
                avg_entry_price=Decimal(p["avg_entry_price"]),
                market_value=Decimal(p["market_value"]),
                unrealized_pl=Decimal(p["unrealized_pl"]),
            )
            for p in payload
        ]

    async def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        quote = await self._get_json(
            self._data, f"/v2/stocks/{symbol}/quotes/latest", params={"feed": "iex"}
        )
        trade = await self._get_json(
            self._data, f"/v2/stocks/{symbol}/trades/latest", params={"feed": "iex"}
        )
        bid = Decimal(str(quote["quote"]["bp"]))
        ask = Decimal(str(quote["quote"]["ap"]))
        last = Decimal(str(trade["trade"]["p"]))
        # Outside market hours IEX reports 0 for one or both sides of the book. Reporting a
        # $0.00 bid as if it were real would be worse than approximating with the last trade.
        if bid <= 0:
            bid = last
        if ask <= 0:
            ask = last
        return Quote(symbol=symbol, bid=bid, ask=ask, last=last)

    # ---- the write --------------------------------------------------------------

    async def submit_order(self, request: OrderRequest, client_order_id: str) -> OrderResult:
        body: dict[str, Any] = {
            "symbol": request.symbol,
            "qty": str(request.quantity),
            "side": request.side.value,
            "type": request.order_type.value,
            "time_in_force": request.time_in_force.value,
            "client_order_id": client_order_id,
        }
        if request.order_type is OrderType.LIMIT and request.limit_price is not None:
            body["limit_price"] = str(request.limit_price)

        response = await self._trading.post("/v2/orders", json=body)

        if response.status_code == 422 and "client_order_id" in response.text:
            # The idempotency guarantee: a duplicate id means the first attempt succeeded and
            # this is a retry. Return the original order — never place a second one.
            return await self._order_by_client_id(client_order_id)

        if response.is_error:
            raise self._map_error(response, symbol=request.symbol)

        return self._to_order_result(response.json())

    async def cancel_order(self, order_id: str) -> None:
        response = await self._trading.delete(f"/v2/orders/{order_id}")
        if response.status_code == 404:
            raise BrokerageError(f"Order {order_id} was not found on the paper account.")
        if response.status_code not in (204, 200):
            raise self._map_error(response, symbol="")

    # ---- plumbing ---------------------------------------------------------------

    async def _order_by_client_id(self, client_order_id: str) -> OrderResult:
        payload = await self._get_json(
            self._trading,
            "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
        )
        return self._to_order_result(payload)

    @staticmethod
    def _to_order_result(payload: dict[str, Any]) -> OrderResult:
        filled_avg = payload.get("filled_avg_price")
        return OrderResult(
            order_id=payload["id"],
            client_order_id=payload["client_order_id"],
            status=payload["status"],
            filled_quantity=Decimal(payload.get("filled_qty") or "0"),
            filled_avg_price=Decimal(filled_avg) if filled_avg else None,
        )

    async def _get_json(
        self, client: httpx.AsyncClient, path: str, *, params: dict[str, str] | None = None
    ) -> Any:
        try:
            response = await client.get(path, params=params)
        except httpx.HTTPError as exc:  # DNS failure, timeout, connection reset
            raise BrokerageError(f"Could not reach Alpaca: {exc}") from exc
        if response.is_error:
            raise self._map_error(response, symbol=path.rsplit("/", 2)[-2].upper())
        return response.json()

    @staticmethod
    def _map_error(response: httpx.Response, *, symbol: str) -> BrokerageError:
        """Translate an Alpaca error into the taxonomy handlers already speak.

        Matching on message text is brittle by nature, so unrecognized errors fall through to
        plain BrokerageError — handlers render that conversationally rather than crashing.
        """
        try:
            message = str(response.json().get("message", response.text))
        except ValueError:
            message = response.text
        lowered = message.lower()

        if "insufficient" in lowered or "buying power" in lowered:
            return InsufficientFunds(message)
        if "not found" in lowered or "not tradable" in lowered or "asset" in lowered:
            return SymbolNotTradable(
                f"{symbol or 'That symbol'} is not tradable on this paper account: {message}"
            )
        if response.status_code in (401, 403):
            return BrokerageError(
                "Alpaca rejected the API credentials. Check APCA_API_KEY_ID / "
                "APCA_API_SECRET_KEY (paper keys, not live)."
            )
        return BrokerageError(f"Alpaca error {response.status_code}: {message}")
