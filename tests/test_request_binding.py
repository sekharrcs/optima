"""Tests for canonical request identity used by semantic-cache reuse."""

from collections.abc import Mapping

import pytest
from pydantic import JsonValue, ValidationError

from optima.cache import SemanticCacheLookupRequest
from optima.domain.quality_contract import (
    OptimizationMode,
    QualityContract,
    QualityProfile,
    RiskTier,
)
from optima.domain.request_binding import RequestBinding, build_request_binding
from optima.domain.request_profile import Complexity, RequestProfile, TaskType


def binding(
    *,
    input_text: str = "Summarize incident ARC-9",
    context: str | None = "Incident ARC-9 is resolved.",
    reference_output: str | None = "Incident resolved",
    criteria: tuple[str, ...] = ("Preserve outcome", "Be concise"),
    metadata: Mapping[str, JsonValue] | None = None,
    task_type: TaskType = TaskType.SUMMARIZATION,
    complexity: Complexity = Complexity.LOW,
) -> RequestBinding:
    """Build one binding while exposing every canonical input dimension."""
    return build_request_binding(
        input_text=input_text,
        context=context,
        reference_output=reference_output,
        criteria=criteria,
        metadata={} if metadata is None else metadata,
        task_type=task_type,
        complexity=complexity,
    )


def test_metadata_object_key_order_does_not_change_binding() -> None:
    """Canonicalize object keys recursively without changing their meaning."""
    first = binding(
        metadata={"z": 3, "nested": {"b": 2, "a": 1}},
    )
    second = binding(
        metadata={"nested": {"a": 1, "b": 2}, "z": 3},
    )

    assert first == second


@pytest.mark.parametrize(
    "changed_criteria",
    [
        ("Be concise", "Preserve outcome"),
        ("Preserve outcome", "Use JSON"),
        ("Preserve outcome",),
        ("Preserve outcome", "Be concise", "Be concise"),
    ],
)
def test_criteria_order_membership_and_duplicates_change_binding(
    changed_criteria: tuple[str, ...],
) -> None:
    """Treat criteria as ordered, membership-sensitive, and duplicate-preserving."""
    assert binding(criteria=changed_criteria) != binding()


@pytest.mark.parametrize(
    ("task_type", "complexity"),
    [
        (TaskType.LOG_ANALYSIS, Complexity.LOW),
        (TaskType.SUMMARIZATION, Complexity.MEDIUM),
    ],
)
def test_task_type_and_complexity_change_binding(
    task_type: TaskType,
    complexity: Complexity,
) -> None:
    """Bind profile facts injected into generation and evaluation metadata."""
    assert binding(task_type=task_type, complexity=complexity) != binding()


def test_run_identity_and_quality_contract_are_excluded_from_binding() -> None:
    """Allow a new run and compatible current contract to assess the same source key."""
    request_binding = binding()
    profile = RequestProfile(
        task_type=TaskType.SUMMARIZATION,
        complexity=Complexity.LOW,
        input_tokens=100,
        risk_tier=RiskTier.LOW,
        cache_eligible=True,
        has_large_context=False,
    )
    first = SemanticCacheLookupRequest(
        run_id="run-current-1",
        input_text="Summarize incident ARC-9",
        context="Incident ARC-9 is resolved.",
        reference_output="Incident resolved",
        criteria=("Preserve outcome", "Be concise"),
        metadata={},
        quality_contract=QualityContract(
            quality_profile=QualityProfile.STANDARD,
            minimum_quality_score=0.80,
            optimization_mode=OptimizationMode.COST,
            risk_tier=RiskTier.LOW,
        ),
        request_profile=profile,
        request_binding=request_binding,
    )
    second = first.model_copy(
        update={
            "run_id": "run-current-2",
            "quality_contract": QualityContract(
                quality_profile=QualityProfile.CRITICAL,
                minimum_quality_score=0.95,
                optimization_mode=OptimizationMode.QUALITY,
                risk_tier=RiskTier.HIGH,
            ),
        }
    )

    assert first.request_binding == second.request_binding == request_binding


def test_null_empty_and_absent_json_values_remain_distinct() -> None:
    """Do not collapse null, empty, absent, or present optional input values."""
    digests = {
        binding(context=None, metadata={}).digest,
        binding(context="Incident ARC-9 is resolved.", metadata={}).digest,
        binding(metadata={"value": None}).digest,
        binding(metadata={"value": ""}).digest,
    }

    assert len(digests) == 4


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_metadata_numbers_are_rejected(value: float) -> None:
    """Reject values that canonical JSON cannot represent portably."""
    with pytest.raises(ValidationError, match="finite"):
        binding(metadata={"value": value})


def test_request_binding_round_trips_through_json() -> None:
    """Preserve the versioned digest contract across JSON transport."""
    original = binding(metadata={"audience": "operations"})

    restored = RequestBinding.model_validate_json(original.model_dump_json())

    assert restored == original


def test_request_binding_serialization_contains_no_raw_request_fields() -> None:
    """Expose profile identity but no raw generation or evaluation content."""
    request_binding = binding(
        input_text="sensitive input",
        context="sensitive context",
        reference_output="sensitive reference",
        metadata={"tenant": "sensitive tenant"},
    )
    serialized = request_binding.model_dump(mode="json")
    encoded = request_binding.model_dump_json()

    assert set(serialized) == {
        "schema_version",
        "algorithm",
        "task_type",
        "complexity",
        "digest",
    }
    assert not {
        "input_text",
        "context",
        "reference_output",
        "criteria",
        "metadata",
    }.intersection(serialized)
    assert "sensitive" not in encoded
