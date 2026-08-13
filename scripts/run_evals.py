"""Run the adversarial eval suite and print the scorecard.

    python scripts/run_evals.py                 # offline keyword classifier
    TRADEDESK_LLM_CLASSIFIER=1 python scripts/run_evals.py   # Claude-backed classifier

Exit code 0 only if every scenario passes. The JSON report lands in reports/evals.json so a
run's evidence can be attached to a slide or a PR rather than retyped.

The LLM run is the one that matters before the demo: it answers "did swapping the classifier
weaken the gate?" with evidence instead of confidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from evals.harness import run_all  # noqa: E402
from evals.scenarios import SCENARIOS  # noqa: E402

# Same convention as app.main: .env is read for convenience (ANTHROPIC_API_KEY for the LLM run),
# but variables already exported in the shell always win.
load_dotenv()

REPORT = Path(__file__).resolve().parent.parent / "reports" / "evals.json"


def build_classifier() -> tuple[object, str]:
    if os.getenv("TRADEDESK_LLM_CLASSIFIER") == "1":
        from app.graph.llm_classifier import LLMClassifier

        return LLMClassifier(), "llm"
    from app.graph.classifier import KeywordClassifier

    return KeywordClassifier(), "keyword"


async def main() -> int:
    classifier, kind = build_classifier()
    print(f"classifier: {kind}   scenarios: {len(SCENARIOS)}\n")

    results = await run_all(SCENARIOS, classifier)

    width = max(len(r.scenario_id) for r in results)
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  {mark}  [{r.category:<10}] {r.scenario_id:<{width}}")
        for failure in r.failures:
            print(f"        ! {failure}")

    by_category = Counter(r.category for r in results)
    failed = [r for r in results if not r.passed]
    print(
        f"\n{len(results) - len(failed)}/{len(results)} passed "
        f"({', '.join(f'{c}: {n}' for c, n in sorted(by_category.items()))})"
    )

    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {"classifier": kind, "passed": not failed, "results": [r.as_dict() for r in results]},
            indent=2,
        )
    )
    print(
        f"report: {REPORT.relative_to(Path.cwd()) if REPORT.is_relative_to(Path.cwd()) else REPORT}"
    )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
