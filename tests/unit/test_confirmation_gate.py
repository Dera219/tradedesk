"""The confirmation gate and the authorization layer.

These are the two claims the DSN demo makes about safety. If any of these fail, a claim in the
talk is false — which is worse than a broken feature, because it gets presented as working.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.auth.roles import AuthorizationError
from app.brokerage.mock import MockBroker
from app.graph.confirmation import is_confirmation, is_rejection
from app.graph.handlers import handle_confirmation, handle_out_of_scope, handle_trade
from app.graph.state import ConversationState
from app.schemas.intents import OutOfScopeReason
from app.schemas.orders import OrderRequest, Side

ALLOWLIST = frozenset({"AAPL", "MSFT", "NVDA", "SPY"})
MAX_QUANTITY = Decimal(100)


def an_order(**overrides: object) -> OrderRequest:
    return OrderRequest(
        **{"symbol": "AAPL", "side": Side.BUY, "quantity": Decimal(10), **overrides}
    )  # type: ignore[arg-type]


async def propose(
    state: ConversationState, broker: MockBroker, order: OrderRequest
) -> ConversationState:
    return await handle_trade(
        state, broker=broker, request=order, allowlist=ALLOWLIST, max_quantity=MAX_QUANTITY
    )


class TestConfirmationParsing:
    @pytest.mark.parametrize(
        "text",
        [
            "yes",
            "Yes",
            "YES",
            "y",
            "yep",
            "confirm",
            "do it",
            "go ahead",
            "yes please",
            "confirm thanks",
            "ok",
        ],
    )
    def test_unambiguous_affirmatives_confirm(self, text: str) -> None:
        assert is_confirmation(text)

    @pytest.mark.parametrize(
        "text",
        [
            "yes but make it 20 shares",  # affirmative + modification: the order shown is wrong
            "yes, what were the fees?",  # affirmative + different question
            "I think yes",  # hedged
            "probably yes",  # hedged
            "yes to the other one",  # affirmative about something else
            "no",
            "cancel",
            "wait",
            "",
            "   ",
            "what does yes mean",
            "did you say yes",
        ],
    )
    def test_anything_ambiguous_does_not_confirm(self, text: str) -> None:
        """Fails closed. The cost of a false negative is retyping; the cost of a false positive
        is an unwanted trade."""
        assert not is_confirmation(text)

    @pytest.mark.parametrize("text", ["no", "nope", "cancel", "stop", "nevermind", "wait"])
    def test_rejections_are_recognized(self, text: str) -> None:
        assert is_rejection(text)


class TestTheGate:
    async def test_proposing_an_order_does_not_execute_it(self) -> None:
        """The core claim: parsing a trade never moves money."""
        broker = MockBroker(cash=Decimal(100_000))
        state = ConversationState(user_message="buy 10 AAPL")

        state = await propose(state, broker, an_order())

        assert state.pending_order is not None
        assert "confirm" in state.reply.lower()
        assert await broker.get_positions() == [], "an order executed without confirmation"
        assert (await broker.get_account()).cash == Decimal(100_000)

    async def test_confirmation_executes_exactly_once(self) -> None:
        broker = MockBroker(cash=Decimal(100_000))
        state = ConversationState(user_message="buy 10 AAPL")
        state = await propose(state, broker, an_order())

        state.user_message = "yes"
        state = await handle_confirmation(state, broker=broker)

        positions = await broker.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "AAPL"
        assert positions[0].quantity == Decimal(10)
        assert "filled" in state.reply.lower()

    async def test_the_echo_states_every_field_being_confirmed(self) -> None:
        """A confirmation the user cannot fully read is not consent."""
        broker = MockBroker()
        state = await propose(ConversationState(), broker, an_order(quantity=Decimal(7)))
        assert "7" in state.reply
        assert "AAPL" in state.reply
        assert "BUY" in state.reply.upper()

    async def test_declining_places_nothing(self) -> None:
        broker = MockBroker(cash=Decimal(100_000))
        state = await propose(ConversationState(), broker, an_order())

        state.user_message = "no"
        state = await handle_confirmation(state, broker=broker)

        assert await broker.get_positions() == []
        assert state.pending_order is None
        assert "cancel" in state.reply.lower()

    async def test_an_unrelated_reply_discards_the_order(self) -> None:
        """The user asked something else. That is not consent, and the order must not linger
        waiting for a later 'yes'."""
        broker = MockBroker(cash=Decimal(100_000))
        state = await propose(ConversationState(), broker, an_order())

        state.user_message = "actually what are the fees here?"
        state = await handle_confirmation(state, broker=broker)

        assert await broker.get_positions() == []
        assert state.pending_order is None

    async def test_a_yes_two_turns_later_cannot_execute_a_stale_order(self) -> None:
        """The scenario the one-turn lifetime exists to prevent: propose, digress, then say
        'yes' to something entirely different."""
        broker = MockBroker(cash=Decimal(100_000))
        state = await propose(ConversationState(), broker, an_order())

        state.user_message = "what are the fees?"
        state = await handle_confirmation(state, broker=broker)  # clears the order

        state.user_message = "yes"
        state = await handle_confirmation(state, broker=broker)

        assert await broker.get_positions() == [], "a stale order executed on a later 'yes'"
        assert "no order waiting" in state.reply.lower()

    async def test_modified_confirmation_does_not_fill_the_original(self) -> None:
        """ "yes but make it 20" must not fill the 10-share order that was shown."""
        broker = MockBroker(cash=Decimal(100_000))
        state = await propose(ConversationState(), broker, an_order(quantity=Decimal(10)))

        state.user_message = "yes but make it 20 shares"
        state = await handle_confirmation(state, broker=broker)

        assert await broker.get_positions() == []

    async def test_confirming_with_nothing_pending_is_harmless(self) -> None:
        broker = MockBroker()
        state = ConversationState(user_message="yes")
        state = await handle_confirmation(state, broker=broker)
        assert await broker.get_positions() == []
        assert "no order waiting" in state.reply.lower()


class TestServerSideValidation:
    async def test_unaffordable_order_is_refused_before_confirmation(self) -> None:
        """Asking someone to confirm an order that cannot fill teaches them the gate is noise."""
        broker = MockBroker(cash=Decimal(100))
        state = await propose(ConversationState(), broker, an_order(quantity=Decimal(50)))
        assert state.pending_order is None
        assert "buying power" in state.reply.lower()

    async def test_symbol_outside_the_allowlist_is_refused(self) -> None:
        broker = MockBroker()
        state = await propose(ConversationState(), broker, an_order(symbol="TSLA"))
        assert state.pending_order is None
        assert "tradable list" in state.reply.lower()

    async def test_quantity_over_the_cap_is_refused(self) -> None:
        broker = MockBroker(cash=Decimal(10_000_000))
        state = await propose(ConversationState(), broker, an_order(quantity=Decimal(500)))
        assert state.pending_order is None
        assert "cap" in state.reply.lower()

    async def test_selling_more_than_held_is_refused(self) -> None:
        broker = MockBroker(cash=Decimal(100_000))
        state = await propose(
            ConversationState(), broker, an_order(side=Side.SELL, quantity=Decimal(5))
        )
        assert state.pending_order is None
        assert "hold" in state.reply.lower()


class TestIdempotency:
    async def test_retrying_the_same_client_order_id_does_not_double_buy(self) -> None:
        """A timeout that hides a successful fill must not become two positions."""
        broker = MockBroker(cash=Decimal(100_000))
        order = an_order()
        first = await broker.submit_order(order, "stable-id-123")
        second = await broker.submit_order(order, "stable-id-123")

        assert first.order_id == second.order_id
        positions = await broker.get_positions()
        assert positions[0].quantity == Decimal(10), "the retry bought a second time"


class TestAuthorizationInCode:
    """Part 4: roles enforced in Python, not prompts. A jailbroken model still gets a 403."""

    async def test_compliance_cannot_place_an_order_even_when_the_graph_asks(self) -> None:
        broker = MockBroker(cash=Decimal(100_000))
        state = ConversationState(role="compliance")
        state = await propose(state, broker, an_order())

        assert state.pending_order is None, "compliance was offered a trade it cannot make"
        assert "not permitted" in state.reply.lower()
        assert await broker.get_positions() == []

    async def test_compliance_cannot_execute_a_pending_order(self) -> None:
        """Even if a pending order somehow reaches the confirmed branch under a read-only role,
        the tool layer refuses."""
        broker = MockBroker(cash=Decimal(100_000))
        trader_state = await propose(ConversationState(role="trader"), broker, an_order())
        pending = trader_state.pending_order
        assert pending is not None

        hijacked = ConversationState(role="compliance", user_message="yes")
        hijacked.pending_order = pending
        hijacked = await handle_confirmation(hijacked, broker=broker)

        assert await broker.get_positions() == []
        assert "not permitted" in hijacked.reply.lower()

    async def test_compliance_can_still_read(self) -> None:
        from app.graph.handlers import handle_portfolio

        broker = MockBroker(cash=Decimal(50_000))
        state = await handle_portfolio(ConversationState(role="compliance"), broker=broker)
        assert "no open positions" in state.reply.lower()

    async def test_the_tool_layer_raises_rather_than_returning_a_value(self) -> None:
        from app.graph.tools import place_order

        broker = MockBroker()
        with pytest.raises(AuthorizationError):
            await place_order(broker, an_order(), "id-1", role="compliance")


class TestOutOfScope:
    async def test_advice_seeking_is_refused_and_redirected(self) -> None:
        """'Should I buy NVDA?' is the most natural question to ask this agent and the one it
        must never answer."""
        state = await handle_out_of_scope(
            ConversationState(user_message="should I buy NVDA?"),
            reason=OutOfScopeReason.ADVICE_SEEKING,
        )
        assert "financial advice" in state.reply.lower()
        assert "evaluate" in state.reply.lower()

    async def test_unrelated_questions_are_refused(self) -> None:
        state = await handle_out_of_scope(
            ConversationState(user_message="what's the weather?"), reason=OutOfScopeReason.UNRELATED
        )
        assert "outside what I do" in state.reply


async def test_unfilled_order_reports_queued_not_filled_zero() -> None:
    """A real broker queues orders outside market hours. The reply must say 'accepted /
    will execute at open', never the mock-shaped lie 'Filled: 0 share(s) at $None'."""
    from decimal import Decimal as D
    from unittest.mock import AsyncMock

    from app.brokerage.base import OrderResult
    from app.schemas.orders import PendingOrder

    broker = MockBroker(cash=D(100_000), allowlist=ALLOWLIST)
    broker.submit_order = AsyncMock(  # type: ignore[method-assign]
        return_value=OrderResult(
            order_id="ord-1", client_order_id="c-1", status="accepted",
            filled_quantity=D(0), filled_avg_price=None,
        )
    )
    state = ConversationState()
    state.pending_order = PendingOrder(request=an_order())
    state.user_message = "yes"
    await handle_confirmation(state, broker=broker)
    assert "accepted" in state.reply.lower()
    assert "market next opens" in state.reply
    assert "$None" not in state.reply
