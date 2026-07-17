"""FastAPI app — the chat UI and HTTP surface for TradeDesk.

Wires the pieces built in Parts 0–2 (classifier, retriever, mock broker, the compiled LangGraph
agent) behind a small HTTP API and a single-page chat UI. Everything runs offline: no API key, no
network, so the demo cannot be broken by venue wifi.

The safety properties are not re-implemented here. They live in the graph and the tool layer, and
this file only carries messages to and from them — which is the point of putting them there. An
HTTP handler is exactly the kind of place where a "quick shortcut" would otherwise erode a
guarantee; here there is no shortcut to take, because `main.py` has no way to place an order except
by asking the agent, and the agent gates it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.graph.build import GraphAgent
from app.graph.classifier import KeywordClassifier
from app.rag.chunking import chunk_corpus
from app.rag.retrieval import LexicalRetriever
from app.schemas.intents import Role
from app.session import SessionStore

CORPUS = Path(__file__).resolve().parent.parent / "corpus"
INDEX_HTML = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
ALLOWLIST = frozenset({"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "SPY", "QQQ"})

#: Populated at startup. A dict rather than globals so the lifespan handler owns their lifetime.
services: dict[str, Any] = {}


def build_agent_for(role: Role) -> GraphAgent:
    """A fresh agent bound to its own mock broker per role.

    Each role gets its own broker instance so the compliance persona genuinely sees a separate
    account surface — it is read-only and cross-account by design, and sharing one broker object
    would blur that. The retriever and classifier are stateless and shared.
    """
    from app.brokerage.mock import MockBroker

    return GraphAgent(
        classifier=services["classifier"],
        retriever=services["retriever"],
        broker=MockBroker(cash=Decimal("100000"), allowlist=ALLOWLIST),
        allowlist=ALLOWLIST,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Build the corpus index once at startup, not per request — chunking and BM25 setup are not
    # free, and a chat endpoint that re-indexed on every message would be needlessly slow.
    services["classifier"] = KeywordClassifier()
    services["retriever"] = LexicalRetriever(chunk_corpus(CORPUS))
    services["sessions"] = SessionStore()
    services["agents"] = {}  # session_id -> GraphAgent (each owns its broker)
    yield
    services.clear()


app = FastAPI(title="TradeDesk", lifespan=lifespan)


class StartRequest(BaseModel):
    role: Role = "trader"


class StartResponse(BaseModel):
    session_id: str
    role: Role
    greeting: str


class ChatRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=1000)


class ChatResponse(BaseModel):
    reply: str
    intent: str | None
    citations: list[str]
    awaiting_confirmation: bool


@app.post("/api/session", response_model=StartResponse)
async def start_session(req: StartRequest) -> StartResponse:
    sessions: SessionStore = services["sessions"]
    session_id = sessions.create(role=req.role)
    services["agents"][session_id] = build_agent_for(req.role)
    return StartResponse(
        session_id=session_id,
        role=req.role,
        greeting=(
            "This is a paper-trading assistant — simulated money only, and no financial advice. "
            "I can explain trading concepts, quote a symbol, show your positions, or place a "
            "simulated order (which I'll always ask you to confirm first)."
        ),
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    sessions: SessionStore = services["sessions"]
    state = sessions.get(req.session_id)
    agent: GraphAgent | None = services["agents"].get(req.session_id)
    if state is None or agent is None:
        # Fail explicitly rather than silently minting a new session — a lost session must not
        # quietly reset a pending order to a blank slate.
        raise HTTPException(status_code=404, detail="Unknown session. Start a new one.")

    new_state = await agent.handle(state, req.message)
    # Persist the returned state: ainvoke produces a new state object, and the pending order lives
    # in it. Dropping this write would break the confirmation gate across turns.
    services["sessions"]._sessions[req.session_id] = new_state

    return ChatResponse(
        reply=new_state.reply,
        intent=new_state.classification.intent.value if new_state.classification else None,
        citations=new_state.citations,
        awaiting_confirmation=new_state.pending_order is not None,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "corpus_chunks": len(services["retriever"].chunks) if "retriever" in services else 0,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML
