"""The confirmation gate.

The agent never executes a trade in the turn it parses one. It echoes a structured order back and
executes only on an explicit yes in the following turn.

## Why this is worth a whole module

The gate is the difference between "the model thought you wanted this" and "you said yes to
this". An LLM misreading "I was thinking about maybe selling some NVDA eventually" as a sell
order is not hypothetical — it is the expected behaviour of a system that classifies intent from
natural language. The gate makes that misread cost one confusing message instead of a position.

## Why interpreting the confirmation is NOT an LLM job

It is tempting to ask the model "did the user confirm?". Don't. That puts the model in the
decision path for the one decision the whole design exists to take away from it, and it fails
open: an ambiguous reply becomes a fill.

This is deterministic string matching that fails CLOSED. Anything not clearly affirmative is
treated as "not confirmed", the order is discarded, and the user can simply ask again. The cost
of a false negative is retyping. The cost of a false positive is an unwanted trade.
"""

from __future__ import annotations

import re

#: Unambiguous affirmatives. Deliberately conservative — every addition must be a phrase whose
#: ONLY reading is "execute the trade I was just shown".
_AFFIRMATIVE = frozenset(
    {
        "yes",
        "y",
        "yeah",
        "yep",
        "yup",
        "confirm",
        "confirmed",
        "do it",
        "go ahead",
        "execute",
        "place it",
        "place the order",
        "submit",
        "send it",
        "ok",
        "okay",
        "sounds good",
        "affirmative",
        "correct",
    }
)

#: Explicit negatives. Matched only to distinguish "user declined" from "user changed the
#: subject" in the reply text — both outcomes discard the order either way.
_NEGATIVE = frozenset(
    {
        "no",
        "n",
        "nope",
        "cancel",
        "stop",
        "abort",
        "nevermind",
        "never mind",
        "don't",
        "dont",
        "wait",
    },
)

_PUNCTUATION = re.compile(r"[^\w\s']")


def _normalize(text: str) -> str:
    return _PUNCTUATION.sub(" ", text.lower()).strip()


def is_confirmation(text: str) -> bool:
    """True only if `text` is unambiguously "execute the order I was just shown".

    Fails closed by design. Note what is deliberately rejected:

    - "yes but make it 20 shares" — contains an affirmative, but also a modification. The order
      on the table is not the order the user wants, so executing it would fill the wrong thing.
    - "yes, what were the fees again?" — an affirmative followed by a different question.
    - "I think yes" / "probably yes" — hedged. Hedging is not consent.

    All of these discard the pending order and let the user restate. Annoying; never wrong.
    """
    normalized = _normalize(text)
    if not normalized:
        return False

    # Exact match against a known affirmative. Anything longer is, by construction, an
    # affirmative plus something else — and that something else could change the order.
    if normalized in _AFFIRMATIVE:
        return True

    # Allow trailing politeness only: "yes please", "confirm thanks".
    words = normalized.split()
    politeness = {"please", "thanks", "thank", "you", "sir", "now"}
    if words and words[0] in _AFFIRMATIVE and all(w in politeness for w in words[1:]):
        return True

    # Multi-word affirmatives that tokenize into several words ("go ahead", "place the order").
    return normalized in {p for p in _AFFIRMATIVE if " " in p}


def is_rejection(text: str) -> bool:
    """True if the user clearly declined.

    Only affects the wording of the reply — a non-confirmation discards the order regardless.
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    if normalized in _NEGATIVE:
        return True
    words = normalized.split()
    return bool(words) and words[0] in _NEGATIVE
