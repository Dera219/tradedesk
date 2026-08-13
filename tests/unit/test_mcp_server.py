"""The MCP surface must preserve the gate.

These tests call the service layer directly — the transport is FastMCP's problem; the gate is
ours. The claims mirror the chat-path gate tests: no execution without the exact token, tokens
are single-use, failure burns the proposal, roles hold, and validation runs before proposal.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.brokerage.mock import MockBroker
from app.mcp_server import ALLOWLIST, TradeDeskService, build_server
from app.rag.chunking import chunk_corpus
from app.rag.retrieval import LexicalRetriever
from evals.harness import SpyBroker

CORPUS = Path(__file__).resolve().parent.parent.parent / "corpus"


@pytest.fixture(scope="module")
def retriever() -> LexicalRetriever:
    return LexicalRetriever(chunk_corpus(CORPUS))


@pytest.fixture
def spy() -> SpyBroker:
    return SpyBroker(MockBroker(cash=Decimal("100000"), allowlist=ALLOWLIST))


@pytest.fixture
def service(spy: SpyBroker, retriever: LexicalRetriever) -> TradeDeskService:
    return TradeDeskService(spy, retriever, role="trader")


def _token(proposal_reply: str) -> str:
    """The token is the uuid after 'token ' in the propose reply."""
    after = proposal_reply.split("token ", 1)[1]
    return after.split()[0].rstrip(".")


async def test_propose_does_not_execute(service: TradeDeskService, spy: SpyBroker) -> None:
    reply = await service.propose("AAPL", "buy", "10")
    assert "PROPOSED (not executed)" in reply
    assert spy.executed == []


async def test_confirm_with_token_executes_exactly_once(
    service: TradeDeskService, spy: SpyBroker
) -> None:
    token = _token(await service.propose("AAPL", "buy", "10"))
    reply = await service.confirm(token)
    assert "Executed" in reply
    assert len(spy.executed) == 1

    # The token is burned: replaying it must not double-execute.
    replay = await service.confirm(token)
    assert "no pending proposal" in replay.lower()
    assert len(spy.executed) == 1


async def test_wrong_token_burns_the_proposal(service: TradeDeskService, spy: SpyBroker) -> None:
    token = _token(await service.propose("AAPL", "buy", "10"))
    reply = await service.confirm("not-the-token")
    assert "Nothing was executed" in reply

    # The real token is now dead too — a failed confirm may not be retried into a fill.
    retry = await service.confirm(token)
    assert "no pending proposal" in retry.lower()
    assert spy.executed == []


async def test_expired_proposal_refuses(service: TradeDeskService, spy: SpyBroker) -> None:
    token = _token(await service.propose("AAPL", "buy", "10"))
    assert service.pending is not None
    service.pending.created_at -= 10_000  # age it past the TTL
    reply = await service.confirm(token)
    assert "expired" in reply.lower()
    assert spy.executed == []


async def test_new_proposal_replaces_old(service: TradeDeskService, spy: SpyBroker) -> None:
    stale = _token(await service.propose("AAPL", "buy", "10"))
    fresh = _token(await service.propose("MSFT", "buy", "5"))
    assert stale != fresh

    # The stale token must not fill anything — and its failure burns the fresh proposal.
    reply = await service.confirm(stale)
    assert "Nothing was executed" in reply
    assert spy.executed == []
    assert service.pending is None


async def test_cancel_proposal(service: TradeDeskService, spy: SpyBroker) -> None:
    token = _token(await service.propose("AAPL", "buy", "10"))
    assert "discarded" in service.cancel_proposal().lower()
    reply = await service.confirm(token)
    assert "no pending proposal" in reply.lower()
    assert spy.executed == []


async def test_client_order_id_is_minted_at_proposal_time(
    service: TradeDeskService, spy: SpyBroker
) -> None:
    """README safety claim #5: the idempotency id exists from the moment the order is proposed.
    Minting it at confirm time instead would make a timed-out confirm unretryable — the retry
    would carry a fresh id, and a hidden fill could double-execute."""
    token = _token(await service.propose("AAPL", "buy", "10"))
    assert service.pending is not None
    proposal_time_id = service.pending.client_order_id

    await service.confirm(token)
    assert [coid for _, coid in spy.executed] == [proposal_time_id]


async def test_transport_failure_keeps_the_proposal_retryable(
    service: TradeDeskService, spy: SpyBroker
) -> None:
    """No answer from the venue is not a failed check: the proposal must survive, and the SAME
    token must retry with the SAME client_order_id — the pair that makes a retry unable to
    double-execute. Every other confirm failure still burns the proposal."""
    from app.brokerage.base import BrokerageUnavailable

    token = _token(await service.propose("AAPL", "buy", "10"))
    assert service.pending is not None
    proposal_time_id = service.pending.client_order_id

    real_submit = spy.submit_order
    fail_next = {"value": True}

    async def submit(request, client_order_id):  # type: ignore[no-untyped-def]
        if fail_next["value"]:
            fail_next["value"] = False
            raise BrokerageUnavailable("Could not reach Alpaca: request timed out")
        return await real_submit(request, client_order_id)

    spy.submit_order = submit  # type: ignore[method-assign]

    reply = await service.confirm(token)
    assert "could not reach" in reply.lower()
    assert "same token" in reply.lower()
    assert spy.executed == []
    assert service.pending is not None, "a transport failure burned the proposal"
    assert service.pending.token == token
    assert service.pending.client_order_id == proposal_time_id

    retry = await service.confirm(token)
    assert "Executed" in retry
    assert [coid for _, coid in spy.executed] == [proposal_time_id]
    assert service.pending is None, "a successful confirm must still burn the proposal"


async def test_compliance_role_cannot_propose(spy: SpyBroker, retriever: LexicalRetriever) -> None:
    service = TradeDeskService(spy, retriever, role="compliance")
    await service.propose("AAPL", "buy", "10")
    assert service.pending is None
    assert spy.executed == []
    # And a stolen token from nowhere does nothing either.
    assert "no pending proposal" in (await service.confirm("any")).lower()


async def test_validation_runs_before_proposal(service: TradeDeskService, spy: SpyBroker) -> None:
    reply = await service.propose("AAPL", "buy", "5000")
    assert service.pending is None
    assert "can't propose" in reply.lower() or "can't place" in reply.lower()

    reply = await service.propose("ZZZZ", "buy", "10")
    assert service.pending is None
    assert spy.executed == []


async def test_garbage_quantity_is_rejected_not_crashed(
    service: TradeDeskService, spy: SpyBroker
) -> None:
    reply = await service.propose("AAPL", "buy", "ten")
    assert "not valid" in reply.lower()
    assert service.pending is None
    assert spy.executed == []


async def test_reads_work(service: TradeDeskService) -> None:
    assert "AAPL" in await service.quote("aapl")
    assert "cash" in (await service.account()).lower()
    assert "no open positions" in (await service.positions()).lower()
    assert service.search_docs("pattern day trader rule")  # corpus has a PDT doc


async def test_server_registers_the_full_surface() -> None:
    server = build_server()
    tools = {t.name for t in await server.list_tools()}
    assert tools == {
        "get_quote",
        "get_account",
        "get_positions",
        "search_docs",
        "propose_order",
        "confirm_order",
        "cancel_proposal",
    }
