"""Intent classification.

Two implementations behind one protocol — the same pattern as `BrokerageClient` and `Retriever`,
for the same reason: the DSN demo must run with no API key and no network.

- `KeywordClassifier` — deterministic rules. Offline, instant, testable, and dumb.
- `LLMClassifier` — the real one. Part 2's target.

## Why the LLM one returns structured JSON

The classifier's output reaches the order path. Parsing free text with a regex there means a
phrasing the regex didn't anticipate silently becomes the wrong intent — and the wrong intent
can mean a trade. So the LLM is constrained to emit JSON validated against `Classification`, and
a validation failure is an error, never a guess.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.schemas.intents import Classification, Intent, OutOfScopeReason
from app.schemas.orders import OrderRequest, OrderType, Side


class Classifier(Protocol):
    def classify(self, message: str) -> Classification: ...


#: Phrasings that are asking for a recommendation. These must route to out_of_scope even though
#: they mention tickers and trading verbs — which is exactly why a naive "contains 'buy'" rule
#: is dangerous, and why `_ADVICE` is checked before `_TRADE`.
_ADVICE = (
    re.compile(r"\bshould\s+i\b", re.I),
    re.compile(
        r"\b(is|are)\s+(it|this|that|\w+)\s+a\s+(good|bad)\s+(buy|sell|investment|idea|entry)", re.I
    ),
    re.compile(r"\bwhat\s+(should|do you think)\s+i\b", re.I),
    re.compile(r"\b(recommend|advice|advise)\b", re.I),
    re.compile(r"\b(will|is)\s+\w+\s+(go|going)\s+(up|down)\b", re.I),
    re.compile(r"\bworth\s+(buying|selling)\b", re.I),
    # Stock-picking, however it is phrased: "what stock will make me rich", "which stock is
    # best", "what should I invest in". Asking WHICH security to hold is a recommendation
    # request even when it is dressed as a question about a concept.
    re.compile(r"\b(what|which)\s+(stock|share|ticker|symbol|compan\w+)\b", re.I),
    re.compile(r"\bmake\s+me\s+(rich|money)\b", re.I),
    re.compile(r"\b(invest|put\s+my\s+money)\s+in\b", re.I),
)

_TRADE = re.compile(r"\b(buy|sell|purchase|short|long)\b", re.I)
_CANCEL = re.compile(r"\b(cancel|modify|change)\s+(my\s+)?(order|trade)\b", re.I)
_PORTFOLIO = re.compile(
    # "account" is both a portfolio word ("my account balance") and a corpus topic
    # ("what account types are there"). The lookahead hands the concept question to educate
    # without giving up the ordinary balance query.
    r"\b(portfolio|positions?|holdings?|p&l|pnl|balance|account(?!\s*type)|cash|own(?:s|ed)?)\b",
    re.I,
)
_RESEARCH = re.compile(r"\b(quote|price|trading at|worth|cost)\b", re.I)
#: A concept question: an asking cue, then a topic the corpus covers.
#:
#: Topic terms end in `\w*` rather than `\b` on purpose. With a hard boundary, "fee" missed
#: "fees", "dividend" missed "dividends", and "settle" missed "settlement" — so the agent
#: refused four of its own corpus documents and told the user they were out of scope. The
#: retriever already stems trailing plurals; the classifier has to be at least as forgiving,
#: or a question never reaches the retriever that could have answered it.
_EDUCATE = re.compile(
    r"\b(what|how|why|when|who|which|explain|define|describe|tell|mean[s]?|difference)\b.*"
    r"\b(order|settl|pdt|day trad|margin|buying power|fee|commission|spread|bid|ask"
    r"|dividend|short|market hour|time in force|account type|risk)\w*",
    re.I | re.S,
)

_SYMBOL = re.compile(r"\b([A-Z]{1,5})\b")
_QUANTITY = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:shares?|units?)?\b", re.I)
#: Limit price. Has to tolerate the filler words people actually type between "limit" and the
#: number — "at a limit of 180.50", "limit price 180.50", "limit 180.50" — plus the bare
#: "at $180.50". Requiring `limit` to sit directly against the digits silently downgraded
#: "at a limit of 180.50" to a MARKET order, which fills at whatever the market says rather than
#: at the price the user named. A quiet, expensive difference.
_LIMIT = re.compile(
    r"\b(?:limit(?:\s+price)?|at)\b(?:\s+(?:a|an|the|of|price))*\s*\$?(\d+(?:\.\d+)?)\b",
    re.I,
)

#: Words that look like tickers but aren't. Without this, "BUY 10 AAPL" can extract "BUY".
_NOT_A_TICKER = frozenset(
    {
        "BUY",
        "SELL",
        "A",
        "I",
        "THE",
        "AT",
        "TO",
        "OF",
        "MY",
        "IS",
        "IT",
        "DO",
        "ME",
        "AND",
        "OR",
        "ALL",
        "NOW",
        "YES",
        "NO",
        "OK",
        "USD",
        "PDT",
        "P",
        "L",
        "HOW",
        "WHAT",
        "WHY",
        "CAN",
    }
)


class KeywordClassifier:
    """Deterministic rules. Good enough to demo the graph offline; not good enough to ship.

    It reads keywords, not meaning. "get me out of my apple position" is a sell request and it
    routes to PORTFOLIO; "what happens between when I trade and when I actually get the shares"
    is the settlement doc and it routes to OUT_OF_SCOPE. Both are handled correctly by the LLM
    version (verified, not assumed). That gap is the argument for it, and it's worth stating
    plainly rather than pretending the rules are adequate.

    Note the LLM version does NOT read "I'd rather not hold NVDA through earnings" as a sell
    either — it classifies it out_of_scope. That is the right call: a musing is not an order,
    and an agent that turns hesitation into a trade would be worse than one that asks.

    The one thing it gets right on purpose: advice-seeking is checked FIRST. "Should I buy NVDA?"
    contains "buy", so any rule that checks trade verbs first would route a request for financial
    advice into the order path.
    """

    def classify(self, message: str) -> Classification:
        text = message.strip()
        if not text:
            return Classification(
                intent=Intent.OUT_OF_SCOPE, confidence=1.0, reasoning="empty message"
            )

        for pattern in _ADVICE:
            if pattern.search(text):
                return Classification(
                    intent=Intent.OUT_OF_SCOPE,
                    confidence=0.9,
                    reasoning="asks for a recommendation, not an explanation",
                )

        if _CANCEL.search(text):
            return Classification(
                intent=Intent.CANCEL_MODIFY, confidence=0.8, reasoning="cancel/modify"
            )

        if _TRADE.search(text) and _extract_symbol(text):
            return Classification(
                intent=Intent.TRADE, confidence=0.85, reasoning="trade verb with a ticker"
            )

        if _PORTFOLIO.search(text):
            return Classification(
                intent=Intent.PORTFOLIO, confidence=0.8, reasoning="portfolio terms"
            )

        if _EDUCATE.search(text):
            return Classification(
                intent=Intent.EDUCATE, confidence=0.75, reasoning="question about a concept"
            )

        if _RESEARCH.search(text) and _extract_symbol(text):
            return Classification(
                intent=Intent.RESEARCH, confidence=0.8, reasoning="price question with a ticker"
            )

        if text.rstrip().endswith("?"):
            # A question we couldn't place is more likely educational than anything else, and
            # educate's low-confidence path already refuses honestly. Guessing "trade" here
            # would be the dangerous default.
            return Classification(
                intent=Intent.EDUCATE, confidence=0.4, reasoning="unclassified question"
            )

        return Classification(
            intent=Intent.OUT_OF_SCOPE, confidence=0.5, reasoning="no rule matched"
        )


def _extract_symbol(text: str, known: frozenset[str] | None = None) -> str | None:
    """Pull a ticker out of a message.

    Case matters, and uppercasing the whole message first is a trap: on "What's AAPL trading at?"
    it yields "S" (the `\\b` after the apostrophe in "WHAT'S"), and on "what is the PDT rule" it
    yields "RULE". Both then get quoted as tickers.

    So: prefer tokens that are ALREADY uppercase in the original text, which is how people
    actually write tickers. Only fall back to case-insensitive matching against a known symbol
    list, where "aapl" is unambiguous because it's on the list.
    """
    for candidate in _SYMBOL.findall(text):
        if candidate not in _NOT_A_TICKER:
            return str(candidate)

    if known:
        for word in re.findall(r"\b([A-Za-z]{1,5})\b", text):
            if word.upper() in known:
                return str(word).upper()

    return None


def classify_out_of_scope_reason(message: str) -> OutOfScopeReason:
    for pattern in _ADVICE:
        if pattern.search(message):
            return OutOfScopeReason.ADVICE_SEEKING
    return OutOfScopeReason.UNRELATED


def extract_order(message: str, known: frozenset[str] | None = None) -> OrderRequest | None:
    """Pull a structured order out of a message.

    Returns None rather than guessing when anything essential is missing. In the LLM version this
    becomes a tool-call schema — but the contract is identical and it is the important part: the
    model fills fields, Pydantic validates them, and Python builds the request. The model never
    constructs an API call.
    """
    symbol = _extract_symbol(message, known)
    if symbol is None:
        return None

    side = Side.SELL if re.search(r"\b(sell|short)\b", message, re.I) else Side.BUY

    quantity_match = _QUANTITY.search(message)
    if quantity_match is None:
        return None

    try:
        quantity = Decimal(quantity_match.group(1))
    except InvalidOperation:
        return None

    limit_match = _LIMIT.search(message)
    order_type = OrderType.MARKET
    limit_price: Decimal | None = None
    if limit_match:
        try:
            limit_price = Decimal(limit_match.group(1))
            order_type = OrderType.LIMIT
        except InvalidOperation:
            return None

    try:
        return OrderRequest(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
        )
    except ValueError:
        # Schema rejected it (bad ticker shape, quantity out of range). Refusing to build a
        # half-valid order is the whole point of validating here.
        return None
