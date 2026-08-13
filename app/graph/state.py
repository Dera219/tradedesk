"""Conversation state carried through the graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.schemas.intents import Classification, Role
from app.schemas.orders import PendingOrder


@dataclass(slots=True)
class Turn:
    speaker: Literal["user", "agent"]
    text: str


@dataclass(slots=True)
class ConversationState:
    """State for one conversation.

    `pending_order` is the confirmation gate's memory and the most safety-critical field here.
    Its lifecycle is deliberately narrow:

    1. The trade handler parses an order and stores it, replying with an echo and a question.
    2. The NEXT user turn either confirms it (execute, then clear) or does anything else
       (clear it, unexecuted).

    It must never survive more than one turn. A pending order that lingers means a "yes" three
    turns later — answering some unrelated question — could execute a trade the user has
    forgotten proposing. Everything that isn't an explicit confirmation clears it.

    One deliberate exception: if submitting a confirmed order fails in *transport*
    (BrokerageUnavailable — the broker may or may not have received it), `handle_confirmation`
    re-arms the same pending order and explicitly re-asks. That restarts the one-turn window
    with the user fully informed rather than leaving a stale order lingering silently, and the
    reused client_order_id makes the retried "yes" idempotent at the venue.
    """

    role: Role = "trader"
    history: list[Turn] = field(default_factory=list)

    # Per-turn scratch, repopulated each turn.
    user_message: str = ""
    classification: Classification | None = None
    reply: str = ""
    citations: list[str] = field(default_factory=list)

    # Survives exactly one turn. See the class docstring.
    pending_order: PendingOrder | None = None

    def record(self, speaker: Literal["user", "agent"], text: str) -> None:
        self.history.append(Turn(speaker=speaker, text=text))
