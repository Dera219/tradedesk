"""LLMClassifier tests — no API key, no network.

The one property that matters most here is fail-closed behavior: every failure mode must resolve
to OUT_OF_SCOPE, never to TRADE. A classifier that fails toward the order path is a safety bug;
these tests pin that it fails toward the harmless refusal instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.graph.llm_classifier import LLMClassifier
from app.schemas.intents import Classification, Intent


class FakeMessages:
    """Stand-in for `client.messages`. Either returns a canned response or raises."""

    def __init__(self, *, parsed: Any = "unset", raises: Exception | None = None) -> None:
        self._parsed = parsed
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return type("Resp", (), {"parsed_output": self._parsed})()


class FakeClient:
    def __init__(self, messages: FakeMessages) -> None:
        self.messages = messages


def classifier_returning(parsed: Any) -> tuple[LLMClassifier, FakeMessages]:
    msgs = FakeMessages(parsed=parsed)
    return LLMClassifier(model="test-model", client=FakeClient(msgs)), msgs


class TestHappyPath:
    def test_returns_the_models_classification(self) -> None:
        wanted = Classification(
            intent=Intent.TRADE, confidence=0.92, reasoning="explicit buy order"
        )
        clf, _ = classifier_returning(wanted)
        assert clf.classify("buy 10 AAPL") == wanted

    def test_advice_classification_passes_through(self) -> None:
        """The model does the advice→out_of_scope judgement; the classifier just relays it. This
        confirms the plumbing carries an out_of_scope result intact."""
        wanted = Classification(
            intent=Intent.OUT_OF_SCOPE, confidence=0.88, reasoning="asks for a recommendation"
        )
        clf, _ = classifier_returning(wanted)
        assert clf.classify("should I buy NVDA?").intent is Intent.OUT_OF_SCOPE

    def test_calls_parse_with_schema_and_system_prompt(self) -> None:
        clf, msgs = classifier_returning(
            Classification(intent=Intent.EDUCATE, confidence=0.7, reasoning="concept question")
        )
        clf.classify("what is a limit order?")

        assert len(msgs.calls) == 1
        call = msgs.calls[0]
        assert call["output_format"] is Classification  # structured output, not free text
        assert call["model"] == "test-model"
        assert "financial advice" in call["system"]  # the advice-guard instruction is present
        assert call["messages"][0]["content"] == "what is a limit order?"


class TestFailsClosed:
    """Every failure resolves to OUT_OF_SCOPE. None may reach TRADE."""

    def test_empty_message_is_out_of_scope_without_calling_the_api(self) -> None:
        msgs = FakeMessages(raises=AssertionError("must not call the API on empty input"))
        clf = LLMClassifier(model="m", client=FakeClient(msgs))
        assert clf.classify("   ").intent is Intent.OUT_OF_SCOPE
        assert msgs.calls == []

    def test_api_exception_fails_closed(self) -> None:
        msgs = FakeMessages(raises=RuntimeError("network down"))
        clf = LLMClassifier(model="m", client=FakeClient(msgs))
        result = clf.classify("buy 10 AAPL")
        assert result.intent is Intent.OUT_OF_SCOPE
        assert result.confidence == 0.0

    def test_refusal_none_output_fails_closed(self) -> None:
        """A safety refusal yields parsed_output=None. That must decline, not trade."""
        clf, _ = classifier_returning(None)
        assert clf.classify("buy 10 AAPL").intent is Intent.OUT_OF_SCOPE

    def test_wrong_type_output_fails_closed(self) -> None:
        clf, _ = classifier_returning({"intent": "trade"})  # a dict, not a Classification
        assert clf.classify("buy 10 AAPL").intent is Intent.OUT_OF_SCOPE

    @pytest.mark.parametrize("bad", [None, {"x": 1}, "trade", 42])
    def test_no_failure_mode_ever_reaches_trade(self, bad: Any) -> None:
        clf, _ = classifier_returning(bad)
        assert clf.classify("buy 50 NVDA now").intent is not Intent.TRADE


class TestInterchangeableWithKeyword:
    def test_satisfies_the_classifier_protocol(self) -> None:
        """Duck-typed check: LLMClassifier has the same callable surface the graph depends on."""
        clf, _ = classifier_returning(
            Classification(intent=Intent.PORTFOLIO, confidence=0.8, reasoning="positions")
        )
        result = clf.classify("show my positions")
        assert isinstance(result, Classification)
        assert result.intent in set(Intent)
