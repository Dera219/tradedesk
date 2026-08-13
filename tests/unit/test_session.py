"""SessionStore's bound.

POST /api/session is unauthenticated and every session pins a compiled graph agent, so the
store's cap is what stands between a public demo and a memory-exhaustion loop. These tests pin
the eviction policy: least recently used goes first, activity counts as recency, and the
`on_evict` hook fires so per-session resources held elsewhere are freed with the state.
"""

from __future__ import annotations

import pytest

from app.session import DEFAULT_MAX_SESSIONS, SessionStore


class TestTheCap:
    def test_creating_past_the_cap_evicts_the_least_recently_used(self) -> None:
        store = SessionStore(max_sessions=2)
        first = store.create()
        second = store.create()
        third = store.create()

        assert store.get(first) is None, "the oldest session survived past the cap"
        assert store.get(second) is not None
        assert store.get(third) is not None
        assert len(store) == 2

    def test_the_store_never_exceeds_the_cap(self) -> None:
        store = SessionStore(max_sessions=5)
        for _ in range(50):
            store.create()
        assert len(store) == 5

    def test_activity_protects_a_session_from_eviction(self) -> None:
        """An active conversation — one being read and written — must not be the one evicted
        while an abandoned session survives."""
        store = SessionStore(max_sessions=2)
        active = store.create()
        idle = store.create()

        assert store.get(active) is not None  # a turn happens on the active session
        store.create()  # pushes the store past the cap

        assert store.get(active) is not None, "the active session was evicted"
        assert store.get(idle) is None, "the idle session should have been the one to go"

    def test_save_counts_as_activity(self) -> None:
        store = SessionStore(max_sessions=2)
        active = store.create()
        state = store.get(active)
        assert state is not None
        idle = store.create()

        store.save(active, state)  # the turn's write-back
        store.create()

        assert store.exists(active)
        assert not store.exists(idle)

    def test_on_evict_fires_with_the_evicted_id(self) -> None:
        """Eviction must free per-session resources held elsewhere (app.main's agent map), or
        the cap bounds this dict while the real memory keeps growing."""
        evicted: list[str] = []
        store = SessionStore(max_sessions=1, on_evict=evicted.append)
        first = store.create()
        second = store.create()

        assert evicted == [first]
        assert store.exists(second)

    def test_a_nonsense_cap_fails_at_construction(self) -> None:
        with pytest.raises(ValueError, match="max_sessions"):
            SessionStore(max_sessions=0)

    def test_the_default_cap_is_sane(self) -> None:
        assert SessionStore().max_sessions == DEFAULT_MAX_SESSIONS
        assert DEFAULT_MAX_SESSIONS >= 100  # roomy for a demo
        assert DEFAULT_MAX_SESSIONS <= 10_000  # bounded enough to mean something


class TestExistingBehaviourSurvivesTheBound:
    def test_ids_are_unguessable_tokens(self) -> None:
        store = SessionStore()
        assert len(store.create()) >= 20  # token_urlsafe(16), not a counter

    def test_reset_keeps_the_role(self) -> None:
        store = SessionStore()
        sid = store.create(role="compliance")
        store.reset(sid)
        state = store.get(sid)
        assert state is not None
        assert state.role == "compliance"

    def test_get_of_an_unknown_session_is_none(self) -> None:
        assert SessionStore().get("nope") is None
