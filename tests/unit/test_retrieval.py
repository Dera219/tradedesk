"""Retrieval and classification tests.

The two headline tests here are regressions for bugs that **the unit tests passed straight
through** and only running `scripts/demo.py` exposed. Both are recorded with the reasoning
because both were silent: no crash, no warning, just a wrong answer delivered confidently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.graph.classifier import KeywordClassifier, _extract_symbol, extract_order
from app.rag.chunking import chunk_corpus
from app.rag.retrieval import LexicalRetriever, tokenize
from app.schemas.intents import Intent

CORPUS = Path(__file__).resolve().parents[2] / "corpus"
ALLOWLIST = frozenset({"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "SPY", "QQQ"})
MIN_SCORE = 0.35


@pytest.fixture(scope="module")
def retriever() -> LexicalRetriever:
    return LexicalRetriever(chunk_corpus(CORPUS))


class TestTheHonestyThreshold:
    """The agent must refuse what it can't ground. A retriever always returns *something*."""

    @pytest.mark.parametrize(
        "query",
        [
            "What's the capital of France?",
            "who won the world cup",
            "what is the airspeed of a swallow",
            "how do I bake bread",
            "what is the weather in Lagos",
            "tell me a joke",
        ],
    )
    def test_unanswerable_questions_score_below_threshold(
        self, retriever: LexicalRetriever, query: str
    ) -> None:
        hits = retriever.search(query, k=3)
        top = hits[0].score if hits else 0.0
        assert top < MIN_SCORE, f"{query!r} scored {top:.3f} — the agent would answer from nothing"

    @pytest.mark.parametrize(
        "query",
        [
            "How much equity do I need to day trade?",
            "What is the pattern day trader rule?",
            "what counts as a day trade",
            "what happens if my equity falls below the minimum",
            "why does the PDT rule exist",
            "when am I classified as a pattern day trader",
        ],
    )
    def test_answerable_questions_score_above_threshold(
        self, retriever: LexicalRetriever, query: str
    ) -> None:
        hits = retriever.search(query, k=3)
        assert hits, f"{query!r} retrieved nothing"
        assert hits[0].score >= MIN_SCORE, (
            f"{query!r} scored {hits[0].score:.3f} — the agent refuses a question it can answer"
        )

    def test_the_threshold_has_a_real_margin(self, retriever: LexicalRetriever) -> None:
        """A threshold that only just separates the two classes is one corpus edit from
        breaking. Assert the gap is wide enough to be a decision rather than a coincidence."""
        answerable = ["How much equity do I need to day trade?", "what counts as a day trade"]
        unanswerable = ["What's the capital of France?", "how do I bake bread"]

        lowest_answer = min(retriever.search(q, k=1)[0].score for q in answerable)
        highest_refusal = max(
            (retriever.search(q, k=1) or [None])[0].score if retriever.search(q, k=1) else 0.0
            for q in unanswerable
        )
        assert lowest_answer - highest_refusal > 0.2


class TestOutOfVocabularyPenalty:
    """REGRESSION — found by running the demo, not by the unit tests.

    `_idf.get(term, 0.0)` gave out-of-vocabulary terms zero weight, so they vanished from the
    score ceiling. "capital of France" collapsed to just "capital", matched "capital cushion" in
    the PDT doc, and scored 0.386 — over the threshold. The retriever grew MORE confident the
    more unusual the question was. Exactly backwards.
    """

    def test_an_unknown_word_costs_the_query_confidence(self, retriever: LexicalRetriever) -> None:
        # "capital" alone is in the corpus ("capital cushion"). Adding a word that appears
        # nowhere must reduce confidence, not leave it untouched.
        with_known_word_only = retriever.search("capital", k=1)
        with_unknown_word = retriever.search("capital of France", k=1)

        assert with_known_word_only, "fixture drift: 'capital' no longer appears in the corpus"
        assert with_unknown_word
        assert with_unknown_word[0].score < with_known_word_only[0].score

    def test_oov_idf_exceeds_every_known_term(self, retriever: LexicalRetriever) -> None:
        """An unseen term must be the strongest possible evidence the corpus can't answer."""
        assert retriever._oov_idf > max(retriever._idf.values())


class TestStemming:
    """REGRESSION: 'why does the PDT rule exist' missed the section titled 'Why this rule
    exists'. The corpus says 'exists', the user typed 'exist'."""

    def test_singular_and_plural_tokenize_alike(self) -> None:
        assert tokenize("exists") == tokenize("exist")
        assert tokenize("fees") == tokenize("fee")
        assert tokenize("orders") == tokenize("order")

    def test_short_words_are_not_mangled(self) -> None:
        assert tokenize("gas") == ["gas"]

    def test_double_s_survives(self) -> None:
        """'loss' must not become 'los'."""
        assert tokenize("loss") == ["loss"]


class TestSymbolExtraction:
    """REGRESSION — also found by running the demo. Uppercasing the message first made
    "What's AAPL trading at?" yield "S" (word boundary after the apostrophe) and
    "what is the PDT rule" yield "RULE". Both were then quoted as tickers."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("What's AAPL trading at?", "AAPL"),
            ("buy 10 AAPL", "AAPL"),
            ("buy 10 aapl", "AAPL"),  # lowercase resolves via the allowlist
            ("should I buy NVDA?", "NVDA"),
            ("what is the PDT rule", None),  # PDT is not a ticker
            ("how do fees work", None),
            ("what are my positions", None),
            ("tell me about settlement", None),
        ],
    )
    def test_extracts_only_real_tickers(self, message: str, expected: str | None) -> None:
        assert _extract_symbol(message, ALLOWLIST) == expected


class TestClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "Should I buy NVDA?",
            "Is TSLA a good buy right now?",
            "what should I invest in?",
            "would you recommend AAPL?",
            "is NVDA going up?",
            "is AAPL worth buying?",
        ],
    )
    def test_advice_seeking_never_reaches_the_trade_path(self, message: str) -> None:
        """These contain trade verbs and tickers. Any rule checking 'buy' before checking for
        advice phrasing routes a request for financial advice into the order path."""
        assert KeywordClassifier().classify(message).intent is Intent.OUT_OF_SCOPE

    @pytest.mark.parametrize(
        "message", ["buy 10 AAPL", "sell 5 MSFT", "buy 20 NVDA at a limit of 180"]
    )
    def test_real_orders_classify_as_trade(self, message: str) -> None:
        assert KeywordClassifier().classify(message).intent is Intent.TRADE

    @pytest.mark.parametrize(
        "message", ["what are my positions", "show me my portfolio", "what's my balance"]
    )
    def test_portfolio_questions(self, message: str) -> None:
        assert KeywordClassifier().classify(message).intent is Intent.PORTFOLIO

    def test_empty_message_is_out_of_scope(self) -> None:
        assert KeywordClassifier().classify("   ").intent is Intent.OUT_OF_SCOPE


class TestOrderExtraction:
    def test_extracts_a_market_order(self) -> None:
        order = extract_order("buy 10 AAPL", ALLOWLIST)
        assert order is not None
        assert (order.symbol, str(order.quantity), order.side.value) == ("AAPL", "10", "buy")

    def test_extracts_a_limit_order(self) -> None:
        order = extract_order("buy 20 NVDA at a limit of 180.50", ALLOWLIST)
        assert order is not None
        assert order.order_type.value == "limit"
        assert str(order.limit_price) == "180.50"

    def test_missing_quantity_returns_none_rather_than_guessing(self) -> None:
        """Defaulting a missing quantity to 1 — or to anything — invents an order the user did
        not place. Refusing costs one clarifying question."""
        assert extract_order("buy AAPL", ALLOWLIST) is None

    def test_missing_symbol_returns_none(self) -> None:
        assert extract_order("buy 10 shares", ALLOWLIST) is None

    def test_quantity_beyond_schema_limits_returns_none(self) -> None:
        """OrderRequest caps quantity at 10,000. A rejected schema must produce None, not a
        half-built order."""
        assert extract_order("buy 999999 AAPL", ALLOWLIST) is None

    @pytest.mark.parametrize(
        "message",
        [
            "buy AAPL at a limit of 50",  # the number is a PRICE, not a share count
            "buy AAPL at $50",
            "buy AAPL limit 50",
            "buy AAPL at limit price 50.25",
        ],
    )
    def test_a_price_is_never_read_as_a_quantity(self, message: str) -> None:
        """'buy AAPL at a limit of 50' names a price and no quantity. Extracting BUY 50 @ $50
        from it invents a 50-share order the user never placed — the contract is None, so the
        graph asks."""
        assert extract_order(message, ALLOWLIST) is None

    @pytest.mark.parametrize(
        ("message", "quantity", "limit_price"),
        [
            ("buy 10 AAPL at limit 50", "10", "50"),
            ("buy 10 AAPL at a limit of 50", "10", "50"),
            ("sell 5 MSFT at $420.50", "5", "420.50"),
            ("buy 7 shares of NVDA limit price 180", "7", "180"),
        ],
    )
    def test_quantity_and_limit_both_present_parse_to_the_right_fields(
        self, message: str, quantity: str, limit_price: str
    ) -> None:
        """Stripping price phrases before quantity matching must not eat a real share count."""
        order = extract_order(message, ALLOWLIST)
        assert order is not None
        assert str(order.quantity) == quantity
        assert order.order_type.value == "limit"
        assert str(order.limit_price) == limit_price
