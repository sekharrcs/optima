"""Tests for deterministic context reduction contracts and benchmark evidence."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from optima.context import (
    ContextPreservationEvidence,
    ContextReductionRequest,
    ContextReductionResult,
    DeterministicExtractiveReducer,
    FakeContextReducer,
    RegexTokenCounter,
)
from optima.context.safety import (
    ContextReducerSafetyRequest,
    DeterministicExtractiveSafetyPolicy,
)
from optima.domain.request_profile import Complexity, TaskType

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "context_reduction" / "preservation_cases.json"
)


def preservation_evidence() -> ContextPreservationEvidence:
    """Build internally consistent extractive source evidence."""
    return ContextPreservationEvidence(
        source_order_preserved=True,
        original_segment_count=3,
        retained_segment_indexes=(0, 2),
        removed_duplicate_count=0,
        removed_irrelevant_count=1,
        task_terms_used=("incident",),
    )


def reduction_result() -> ContextReductionResult:
    """Build one valid measured reducer result."""
    return ContextReductionResult(
        reduced_context="Incident INC-204 closed.",
        original_token_count=10,
        reduced_token_count=5,
        reducer_name="test-reducer",
        method="EXTRACTIVE",
        token_counter_name="test-counter",
        preservation=preservation_evidence(),
    )


def fixture_cases() -> list[dict[str, Any]]:
    """Load deterministic preservation benchmark cases as structured data."""
    loaded = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, list)
    return loaded


def test_regex_token_counter_returns_repeatable_actual_counts() -> None:
    """Measure the supplied text through one named deterministic boundary."""
    counter = RegexTokenCounter()

    assert counter.counter_name == "regex-token-counter-v1"
    assert counter.count("Incident INC-204 affected 37 requests.") == 6
    assert counter.count("Incident INC-204 affected 37 requests.") == 6
    assert counter.count("") == 0


def safety_request(**updates: object) -> ContextReducerSafetyRequest:
    """Build one request inside the local deterministic safety envelope."""
    values: dict[str, object] = {
        "input_text": "Summarize the incident facts",
        "context": (
            "Priya Nair owns incident INC-204.\nPriya Nair owns incident INC-204."
        ),
        "task_type": TaskType.SUMMARIZATION,
        "complexity": Complexity.LOW,
    }
    values.update(updates)
    return ContextReducerSafetyRequest.model_validate(values)


def test_local_safety_policy_allows_only_duplicate_line_summarization() -> None:
    """Approve the narrow envelope where no unique source line is discarded."""
    decision = DeterministicExtractiveSafetyPolicy().evaluate(safety_request())

    assert decision.task_safe is True
    assert decision.policy_name == "local-deterministic-deduplication-v1"


@pytest.mark.parametrize(
    "updates",
    [
        {"task_type": TaskType.Q_AND_A},
        {"complexity": Complexity.MEDIUM},
        {"context": "Priya Nair owns incident INC-204."},
        {
            "context": (
                "Priya Nair owns incident INC-204.\nINC-204 affected 37 requests."
            )
        },
        {
            "context": (
                "Priya Nair owns incident INC-204.\n"
                "Priya Nair owns incident INC-204.\n"
                "Unrelated social update."
            )
        },
        {
            "context": (
                "Identifier Foo must be retained.\nIdentifier foo must be retained."
            )
        },
        {
            "context": (
                "Identifier Foo must be retained.\n Identifier Foo must be retained."
            )
        },
    ],
)
def test_local_safety_policy_rejects_unproven_request_shapes(
    updates: dict[str, object],
) -> None:
    """Default to unsafe outside the explicit duplicate-line envelope."""
    decision = DeterministicExtractiveSafetyPolicy().evaluate(safety_request(**updates))

    assert decision.task_safe is False


def test_reducer_preserves_case_distinct_identifier_lines() -> None:
    """Do not collapse lines whose identifier case may carry meaning."""
    counter = RegexTokenCounter()
    reducer = DeterministicExtractiveReducer(counter)
    context = (
        "Identifier Foo must be retained.\n"
        "Identifier foo must be retained.\n"
        "Identifier Foo must be retained."
    )

    result = asyncio.run(
        reducer.reduce(
            ContextReductionRequest(
                run_id="run-case-distinct",
                input_text="Summarize retained identifiers",
                context=context,
            )
        )
    )

    assert result.reduced_context == (
        "Identifier Foo must be retained.\nIdentifier foo must be retained."
    )
    assert result.preservation.retained_segment_indexes == (0, 1)
    assert result.preservation.removed_duplicate_count == 1


@pytest.mark.parametrize("case", fixture_cases(), ids=lambda case: case["case_id"])
def test_benchmark_fixture_required_facts_and_order_survive(
    case: dict[str, Any],
) -> None:
    """Prove only the named fixture facts survive deterministic extraction."""
    counter = RegexTokenCounter()
    reducer = DeterministicExtractiveReducer(counter)
    request = ContextReductionRequest(
        run_id=f"run-{case['case_id']}",
        input_text=case["input_text"],
        context=case["context"],
    )

    result = asyncio.run(reducer.reduce(request))

    assert result.original_token_count == counter.count(case["context"])
    assert result.reduced_token_count == counter.count(result.reduced_context)
    assert result.reduced_token_count < result.original_token_count
    assert result.method == "RELEVANCE_AND_FACT_EXTRACTIVE_V1"
    assert result.preservation.source_order_preserved is True
    required_facts = case["required_facts"]
    assert all(fact in result.reduced_context for fact in required_facts)
    assert [result.reduced_context.index(fact) for fact in required_facts] == sorted(
        result.reduced_context.index(fact) for fact in required_facts
    )
    assert all(
        material not in result.reduced_context for material in case["removed_material"]
    )
    assert result.reduced_context.count(required_facts[0]) == 1


def test_fake_reducer_records_calls_and_returns_configured_result() -> None:
    """Support exact deterministic runtime assertions without network calls."""
    expected = reduction_result()
    reducer = FakeContextReducer((expected,))
    request = ContextReductionRequest(
        run_id="run-fake",
        input_text="Summarize incident",
        context="Original incident context",
    )

    actual = asyncio.run(reducer.reduce(request))

    assert actual == expected
    assert reducer.calls == (request,)


def test_fake_reducer_raises_configured_timeout_without_sleeping() -> None:
    """Represent a timeout deterministically without wall-clock delay."""
    reducer = FakeContextReducer((TimeoutError("reducer timed out"),))
    request = ContextReductionRequest(
        run_id="run-timeout",
        input_text="Summarize incident",
        context="Original incident context",
    )

    with pytest.raises(TimeoutError, match="reducer timed out"):
        asyncio.run(reducer.reduce(request))
    assert reducer.calls == (request,)


def test_result_rejects_claim_without_fewer_tokens() -> None:
    """Prevent equal or larger token counts from being called a reduction."""
    payload = reduction_result().model_dump()
    payload["reduced_token_count"] = payload["original_token_count"]

    with pytest.raises(ValidationError, match="fewer measured tokens"):
        ContextReductionResult.model_validate(payload)


def test_preservation_evidence_rejects_inconsistent_segment_counts() -> None:
    """Require every source segment to be retained or explicitly removed."""
    payload = preservation_evidence().model_dump()
    payload["removed_irrelevant_count"] = 0

    with pytest.raises(ValidationError, match="account for every source segment"):
        ContextPreservationEvidence.model_validate(payload)


def test_contracts_reject_unknown_fields_and_coercion() -> None:
    """Keep reducer requests and measured counts strict at the boundary."""
    with pytest.raises(ValidationError):
        ContextReductionRequest.model_validate(
            {
                "run_id": "run-strict",
                "input_text": "Summarize",
                "context": "Context",
                "unknown": True,
            }
        )
    with pytest.raises(ValidationError):
        ContextReductionResult.model_validate(
            {
                **reduction_result().model_dump(),
                "original_token_count": "10",
            }
        )
