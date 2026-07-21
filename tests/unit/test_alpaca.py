"""AlpacaBroker request/response mapping, tested against a fake transport.

No network, no real credentials. httpx.MockTransport lets us assert on the exact requests the
client would send and script Alpaca's responses — including the failure shapes that matter:
duplicate client_order_id (the idempotency guarantee), insufficient buying power, and an
untradable symbol. If any of these mappings drift, the confirmation gate upstream starts
showing users errors it can't explain.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.brokerage.alpaca import AlpacaBroker, AlpacaConfigError
from app.brokerage.base import BrokerageError, InsufficientFunds, SymbolNotTradable
from app.schemas.orders import OrderRequest, OrderType, Side

FILLED_ORDER = {
    "id": "b6b8-order-id",
    "client_order_id": "coid-123",
    "status": "filled",
    "filled_qty": "10",
    "filled_avg_price": "227.51",
}


def broker_with(handler) -> AlpacaBroker:
    return AlpacaBroker(
        key_id="test-key",
        secret_key="test-secret",  # noqa: S106 — a fake for MockTransport, never sent anywhere
        transport=httpx.MockTransport(handler),
    )


def an_order(**overrides: object) -> OrderRequest:
    return OrderRequest(
        **{"symbol": "AAPL", "side": Side.BUY, "quantity": Decimal(10), **overrides}
    )  # type: ignore[arg-type]


async def test_missing_credentials_fail_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    with pytest.raises(AlpacaConfigError, match="APCA_API_KEY_ID"):
        AlpacaBroker()


async def test_account_parses_money_as_decimal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/account"
        assert request.headers["APCA-API-KEY-ID"] == "test-key"
        return httpx.Response(
            200,
            json={
                "buying_power": "200000.05",
                "cash": "100000.05",
                "portfolio_value": "123456.78",
                "pattern_day_trader": False,
            },
        )

    broker = broker_with(handler)
    account = await broker.get_account()
    assert account.cash == Decimal("100000.05")
    assert account.buying_power == Decimal("200000.05")
    assert isinstance(account.cash, Decimal)


async def test_quote_uses_last_trade_when_book_is_empty() -> None:
    """After hours IEX reports a 0 bid/ask; a $0.00 quote must not surface to the user."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "quotes" in request.url.path:
            return httpx.Response(200, json={"quote": {"bp": 0, "ap": 227.6}})
        return httpx.Response(200, json={"trade": {"p": 227.5}})

    broker = broker_with(handler)
    quote = await broker.get_quote("aapl")
    assert quote.symbol == "AAPL"
    assert quote.bid == Decimal("227.5")  # fell back to last trade
    assert quote.ask == Decimal("227.6")  # real side kept


async def test_submit_order_sends_validated_fields_and_maps_result() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=FILLED_ORDER)

    broker = broker_with(handler)
    result = await broker.submit_order(an_order(), client_order_id="coid-123")

    assert seen == {
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": "coid-123",
    }
    assert result.order_id == "b6b8-order-id"
    assert result.filled_quantity == Decimal(10)
    assert result.filled_avg_price == Decimal("227.51")


async def test_limit_order_carries_limit_price() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["type"] == "limit"
        assert body["limit_price"] == "225.00"
        return httpx.Response(200, json=FILLED_ORDER)

    broker = broker_with(handler)
    await broker.submit_order(
        an_order(order_type=OrderType.LIMIT, limit_price=Decimal("225.00")),
        client_order_id="coid-123",
    )


async def test_duplicate_client_order_id_returns_original_fill() -> None:
    """The retry-after-timeout case: a duplicate id must fetch the first fill, never re-buy."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(
                422, json={"message": "client_order_id must be unique", "code": 40010001}
            )
        assert request.url.params["client_order_id"] == "coid-123"
        return httpx.Response(200, json=FILLED_ORDER)

    broker = broker_with(handler)
    result = await broker.submit_order(an_order(), client_order_id="coid-123")

    assert result.client_order_id == "coid-123"
    assert result.status == "filled"
    assert calls == ["POST /v2/orders", "GET /v2/orders:by_client_order_id"]


async def test_insufficient_buying_power_maps_to_insufficient_funds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "insufficient buying power"})

    broker = broker_with(handler)
    with pytest.raises(InsufficientFunds):
        await broker.submit_order(an_order(), client_order_id="coid-1")


async def test_unknown_symbol_maps_to_symbol_not_tradable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "asset ZZZZ not found"})

    broker = broker_with(handler)
    with pytest.raises(SymbolNotTradable):
        await broker.submit_order(an_order(symbol="ZZZZ"), client_order_id="coid-1")


async def test_bad_credentials_produce_actionable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    broker = broker_with(handler)
    with pytest.raises(BrokerageError, match="APCA_API_KEY_ID"):
        await broker.get_account()


async def test_network_failure_becomes_brokerage_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    broker = broker_with(handler)
    with pytest.raises(BrokerageError, match="Could not reach Alpaca"):
        await broker.get_account()
