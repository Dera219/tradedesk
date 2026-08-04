"""Per-conversation session store.

The agent is stateless between turns; the conversation is not. `ConversationState` — crucially the
pending order and the role — has to persist across HTTP requests, so it lives here keyed by a
session id the client holds.

In-memory on purpose. A real deployment would use Redis or a database, but for a capstone demo a
process-local dict is honest and adequate — and it means no external service to stand up on stage.
The one property that matters for safety is preserved: each session's pending order is isolated
from every other session's, so one user's unconfirmed trade can never be confirmed from another's
turn.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from app.graph.state import ConversationState
from app.schemas.intents import Role


@dataclass
class SessionStore:
    _sessions: dict[str, ConversationState] = field(default_factory=dict)

    def create(self, *, role: Role = "trader") -> str:
        # token_urlsafe, not a counter: a guessable session id would let one user resume another's
        # conversation — including their pending order. Unguessable ids close that off.
        session_id = secrets.token_urlsafe(16)
        self._sessions[session_id] = ConversationState(role=role)
        return session_id

    def get(self, session_id: str) -> ConversationState | None:
        return self._sessions.get(session_id)

    def save(self, session_id: str, state: ConversationState) -> None:
        """Persist the state a turn produced.

        The graph returns a NEW state object rather than mutating the old one, and the pending
        order lives inside it — so a turn that isn't saved silently breaks the confirmation gate
        across requests. Callers go through here rather than reaching into the dict, so that
        rule stays enforceable in one place.
        """
        self._sessions[session_id] = state

    def exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    def reset(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            self._sessions[session_id] = ConversationState(role=state.role)
