"""The tool layer — the only place broker calls happen, and the only place roles are enforced.

Every function here is decorated with `@requires(...)`. That decorator is the Part 4 story: a
system prompt saying "you are read-only" is a request the model usually honors, and "usually" is
not a security property. These checks run in Python, before the broker call, and raise regardless
of what the model decided.

Prompts are UX. Code is security.
"""

from __future__ import annotations

from decimal import Decimal

from app.auth.roles import requires
from app.brokerage.base import Account, BrokerageClient, OrderResult, Position, Quote
from app.schemas.intents import Role
from app.schemas.orders import OrderRequest


@requires("read_own")
async def get_quote(broker: BrokerageClient, symbol: str, *, role: Role) -> Quote:
    return await broker.get_quote(symbol)


@requires("read_own")
async def get_positions(broker: BrokerageClient, *, role: Role) -> list[Position]:
    return await broker.get_positions()


@requires("read_own")
async def get_account(broker: BrokerageClient, *, role: Role) -> Account:
    return await broker.get_account()


@requires("trade")
async def place_order(
    broker: BrokerageClient,
    request: OrderRequest,
    client_order_id: str,
    *,
    role: Role,
) -> OrderResult:
    """Submit a CONFIRMED order.

    Reaching this function means the graph has already gated on an explicit user confirmation.
    Nothing here re-checks that, so it must only ever be called from the confirmed branch of
    `handle_confirmation` — never speculatively, and never to "preview" a fill.
    """
    return await broker.submit_order(request, client_order_id)


@requires("trade")
async def validate_order_against_account(
    broker: BrokerageClient,
    request: OrderRequest,
    *,
    role: Role,
    allowlist: frozenset[str],
    max_quantity: Decimal,
) -> list[str]:
    """Server-side checks run BEFORE the order is shown to the user for confirmation.

    Returns a list of human-readable problems; empty means it looks executable.

    Running these before the echo matters: asking someone to confirm an order that cannot fill
    wastes their turn and teaches them the confirmation step is noise. Better to say "you can't
    afford that" than to ask "shall I?" and then fail.

    The schema already validated shape, and the broker will validate again. This is the middle
    layer that produces a *conversational* error instead of an exception.
    """
    problems: list[str] = []

    if request.symbol not in allowlist:
        problems.append(
            f"{request.symbol} isn't on the tradable list for this account. "
            f"Available: {', '.join(sorted(allowlist))}."
        )
        # Everything below needs a price, which we can't get for an untradable symbol.
        return problems

    if request.quantity > max_quantity:
        problems.append(
            f"{request.quantity} shares exceeds the {max_quantity}-share per-order cap."
        )

    quote = await broker.get_quote(request.symbol)
    reference = request.limit_price if request.limit_price is not None else quote.ask

    if request.side.value == "buy":
        account = await broker.get_account()
        cost = request.quantity * reference
        if cost > account.buying_power:
            problems.append(
                f"That would cost about ${cost:,.2f}, but buying power is "
                f"${account.buying_power:,.2f}."
            )
    else:
        positions = {p.symbol: p.quantity for p in await broker.get_positions()}
        held = positions.get(request.symbol, Decimal(0))
        if request.quantity > held:
            problems.append(
                f"You hold {held} share(s) of {request.symbol}, so you can't sell "
                f"{request.quantity}."
            )

    return problems
