"""Role-based authorization, enforced in code.

This module is the Part 4 story, and it is worth being precise about why it exists.

A system prompt saying "you are a compliance officer and must not place trades" is a *request*.
The model usually honors it. Under adversarial input it sometimes doesn't, and "usually" is not
a security property. These decorators are enforcement: the check runs in Python, before the
broker call, and raises regardless of what the model decided to do.

Prompts are UX. Code is security. The jailbreak demo works precisely because the prompt can be
defeated and the decorator cannot.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from app.schemas.intents import Role

P = ParamSpec("P")
T = TypeVar("T")


class AuthorizationError(Exception):
    """Raised when a role attempts a capability it does not have.

    Carries no sensitive detail — the message reaches the user.
    """

    def __init__(self, role: Role, capability: str) -> None:
        self.role = role
        self.capability = capability
        super().__init__(f"Role '{role}' is not permitted to {capability}.")


#: Capability grants per role. Compliance is deliberately read-only and cross-account: it can see
#: everything and change nothing. Trader is the inverse — full control, own account only.
CAPABILITIES: dict[Role, frozenset[str]] = {
    "trader": frozenset({"read_own", "trade", "cancel_modify"}),
    "compliance": frozenset({"read_own", "read_all"}),
}


def requires(capability: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Gate a tool function on a capability.

    The wrapped function must accept `role` as a keyword argument. Reading the role from the
    call site rather than from ambient/global state is deliberate: ambient state is exactly what
    gets confused when concurrent requests share a process, and a role mix-up here is a real
    security bug rather than a cosmetic one.
    """

    def decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            role = kwargs.get("role")
            if role is None:
                # Fail closed. A missing role is a bug, and the safe response to a bug in an
                # authorization path is to deny, never to assume the permissive default.
                raise AuthorizationError("unknown", capability)  # type: ignore[arg-type]
            if capability not in CAPABILITIES.get(role, frozenset()):
                raise AuthorizationError(role, capability)
            return await fn(*args, **kwargs)

        return wrapper

    return decorator
