"""Order models.

The LLM never constructs an API call. It fills an OrderRequest, Pydantic validates it, and
Python builds the request. This is the whole safety argument in one sentence: the model's output
is data to be checked, never a command to be run.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"


class OrderRequest(BaseModel):
    """A validated, not-yet-submitted order.

    Constructing one of these is explicitly NOT authorization to send it. It is a proposal that
    must survive server-side checks and an explicit user confirmation first. The type system
    can't enforce that, so the graph does: see app/graph/nodes/trade.py.
    """

    model_config = {"frozen": True}

    symbol: str = Field(pattern=r"^[A-Z]{1,5}$", description="Uppercase US equity ticker")
    side: Side
    quantity: Decimal = Field(gt=0, le=Decimal("10000"))
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = Field(default=None, gt=0)
    time_in_force: TimeInForce = TimeInForce.DAY

    @model_validator(mode="after")
    def _limit_price_matches_type(self) -> OrderRequest:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require a limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market orders must not carry a limit_price")
        return self

    def summarize(self) -> str:
        """Human-readable echo for the confirmation gate.

        This string is what the user says yes to, so it must state every field that affects
        what executes. A confirmation the user cannot fully read is not consent.
        """
        price = (
            f" at a limit of ${self.limit_price}"
            if self.order_type is OrderType.LIMIT
            else " at the current market price"
        )
        duration = "for the day" if self.time_in_force is TimeInForce.DAY else "until cancelled"
        return (
            f"{self.side.value.upper()} {self.quantity} share(s) of {self.symbol}"
            f"{price}, good {duration}."
        )


class PendingOrder(BaseModel):
    """An order awaiting confirmation, held in graph state across exactly one turn."""

    model_config = {"frozen": True}

    request: OrderRequest
    #: Sent to the broker as client_order_id. Generated once, when the order is proposed, and
    #: reused on every retry — so a network timeout that hides a successful fill cannot cause a
    #: retry to double-buy. Generating this at submission time instead would defeat the purpose.
    client_order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    quoted_price: Decimal | None = None
    estimated_cost: Decimal | None = None
