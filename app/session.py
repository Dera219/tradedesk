"""Per-conversation session store.

The agent is stateless between turns; the conversation is not. `ConversationState` — crucially the
pending order and the role — has to persist across HTTP requests, so it lives here keyed by a
session id the client holds.

In-memory on purpose. A real deployment would use Redis or a database, but for a capstone demo a
process-local dict is honest and adequate — and it means no external service to stand up on stage.
The one property that matters for safety is preserved: each session's pending order is isolated
from every other session's, so one user's unconfirmed trade can never be confirmed from another's
turn.

Bounded on purpose too. `POST /api/session` is unauthenticated, and each session pins a compiled
graph agent in memory — an unbounded store means anyone with `curl` and a loop can grow the
process until it dies. `max_sessions` caps the store with least-recently-used eviction: past the
cap, the session that has gone longest without a turn is dropped, and its next request gets the
same explicit 404 as any unknown session — never a silently minted blank state. `on_evict` lets
the owner of any per-session resources (app.main's agent map) release them in the same breath,
so the cap bounds the whole footprint rather than just this dict.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from app.graph.state import ConversationState
from app.schemas.intents import Role

#: Generous for a demo, small enough that a session-creation loop cannot exhaust memory.
DEFAULT_MAX_SESSIONS = 500


@dataclass
class SessionStore:
    #: Hard cap on stored sessions. Creating past it evicts the least recently used.
    max_sessions: int = DEFAULT_MAX_SESSIONS
    #: Called with each evicted session id, so per-session resources held elsewhere are freed
    #: alongside the state — an eviction that leaks the agent would defeat the bound.
    on_evict: Callable[[str], None] | None = None
    _sessions: OrderedDict[str, ConversationState] = field(default_factory=OrderedDict)

    def __post_init__(self) -> None:
        if self.max_sessions < 1:
            raise ValueError(f"max_sessions must be >= 1, got {self.max_sessions}")

    def create(self, *, role: Role = "trader") -> str:
        # token_urlsafe, not a counter: a guessable session id would let one user resume another's
        # conversation — including their pending order. Unguessable ids close that off.
        session_id = secrets.token_urlsafe(16)
        self._sessions[session_id] = ConversationState(role=role)
        while len(self._sessions) > self.max_sessions:
            evicted_id, _ = self._sessions.popitem(last=False)  # least recently used
            if self.on_evict is not None:
                self.on_evict(evicted_id)
        return session_id

    def get(self, session_id: str) -> ConversationState | None:
        state = self._sessions.get(session_id)
        if state is not None:
            # Reading a session is activity: an active conversation must not be the one evicted.
            self._sessions.move_to_end(session_id)
        return state

    def save(self, session_id: str, state: ConversationState) -> None:
        """Persist the state a turn produced.

        The graph returns a NEW state object rather than mutating the old one, and the pending
        order lives inside it — so a turn that isn't saved silently breaks the confirmation gate
        across requests. Callers go through here rather than reaching into the dict, so that
        rule stays enforceable in one place.
        """
        self._sessions[session_id] = state
        self._sessions.move_to_end(session_id)

    def exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    def reset(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            self._sessions[session_id] = ConversationState(role=state.role)

    def __len__(self) -> int:
        return len(self._sessions)
