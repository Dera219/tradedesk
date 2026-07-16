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

P = ParamSpec("P")
T = TypeVar("T")


class AuthorizationError(Exception):
    """Raised when a role attempts a capability it does not have.

    Carries no sensitive detail — the message reaches the user.
    """

    def __init__(self, role: str, capability: str) -> None:
        self.role = role
        self.capability = capability
        super().__init__(f"Role '{role}' is not permitted to {capability}.")


#: Capability grants per role. Compliance is deliberately read-only and cross-account: it can see
#: everything and change nothing. Trader is the inverse — full control, own account only.
#:
#: Keyed by `str`, not by the `Role` literal, on purpose: the decorator looks this up with a
#: value that has NOT been validated yet — that lookup IS the validation. Typing the keys as
#: `Role` would force a cast at the call site, which would assert the very fact we're checking.
#: `Role` still documents intent everywhere a role is passed deliberately.
CAPABILITIES: dict[str, frozenset[str]] = {
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
            # ParamSpec erases kwarg types, so `role` arrives as `object`. Validate it against
            # the known roles rather than casting: a cast would assert a fact we haven't
            # checked, and an unrecognized role reaching the capability lookup would then just
            # miss and deny — correct, but for the wrong reason and impossible to debug.
            raw_role = kwargs.get("role")
            grants = CAPABILITIES.get(raw_role) if isinstance(raw_role, str) else None

            if grants is None:
                # Fail closed. A missing or unrecognized role is a bug, and the safe response to
                # a bug in an authorization path is to deny — never to assume the permissive
                # default.
                raise AuthorizationError(_describe(raw_role), capability)

            if capability not in grants:
                raise AuthorizationError(_describe(raw_role), capability)

            return await fn(*args, **kwargs)

        return wrapper

    return decorator


def _describe(role: object) -> str:
    """Render a role for an error message the user will see.

    Never interpolates the raw value: an unrecognized `role` could be anything the caller passed,
    and echoing arbitrary input into a user-facing string is how you get log injection and worse.
    """
    if isinstance(role, str) and role in CAPABILITIES:
        return role
    return "unknown"
