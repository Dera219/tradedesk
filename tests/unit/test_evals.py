"""The eval suite runs in CI against the offline classifier.

Each scenario is its own test so a failure names the broken claim directly in the pytest
output — "test_scenario[inject-fake-authority]" — instead of one opaque suite failure.
"""

from __future__ import annotations

import pytest

from app.graph.classifier import KeywordClassifier
from evals.harness import run_scenario
from evals.scenarios import SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_scenario(scenario) -> None:  # noqa: ANN001
    result = await run_scenario(scenario, KeywordClassifier())
    assert result.passed, "\n".join(result.failures)
