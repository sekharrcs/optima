"""Tests for provider abstraction contracts and deterministic fake providers."""

import asyncio
from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from optima.domain.execution import ModelRole
from optima.providers import (
    FakeProviderResponse,
    ModelProvider,
    ModelProviderRequest,
    build_fake_small_provider,
    build_fake_strong_provider,
)


class ScriptedClock:
    """Deterministic monotonic clock that returns scripted timestamps."""

    def __init__(self, timestamps: Sequence[float]) -> None:
        self._timestamps = list(timestamps)
        self._index = 0

    def now(self) -> float:
        if self._index >= len(self._timestamps):
            raise AssertionError("scripted clock has no more timestamps")
        value = self._timestamps[self._index]
        self._index += 1
        return value


def provider_request(
    *, run_id: str, role: ModelRole, text: str
) -> ModelProviderRequest:
    """Build one provider-independent model request."""
    return ModelProviderRequest(run_id=run_id, model_role=role, input_text=text)


def test_fake_small_provider_returns_configured_output() -> None:
    """Return the deterministic output configured for SMALL calls."""
    provider = build_fake_small_provider(
        provider_name="fake-provider",
        deployment_name="small-deployment",
        responses=(
            FakeProviderResponse(
                output_text="small-output",
                input_tokens=12,
                output_tokens=8,
            ),
        ),
        clock=ScriptedClock([10.0, 10.005]),
    )

    result = asyncio.run(
        provider.generate(
            provider_request(run_id="run-small", role=ModelRole.SMALL, text="prompt")
        )
    )

    assert result.output_text == "small-output"
    assert result.usage.model_role is ModelRole.SMALL


def test_fake_strong_provider_returns_configured_output() -> None:
    """Return the deterministic output configured for STRONG calls."""
    provider = build_fake_strong_provider(
        provider_name="fake-provider",
        deployment_name="strong-deployment",
        responses=(
            FakeProviderResponse(
                output_text="strong-output",
                input_tokens=20,
                output_tokens=11,
                request_id="req-strong-1",
            ),
        ),
        clock=ScriptedClock([5.0, 5.002]),
    )

    result = asyncio.run(
        provider.generate(
            provider_request(run_id="run-strong", role=ModelRole.STRONG, text="prompt")
        )
    )

    assert result.output_text == "strong-output"
    assert result.usage.model_role is ModelRole.STRONG


def test_usage_contains_required_provider_and_token_facts() -> None:
    """Expose the full usage facts needed by future executor logic."""
    provider = build_fake_small_provider(
        provider_name="fake-provider",
        deployment_name="small-deployment",
        responses=(
            FakeProviderResponse(
                output_text="answer",
                input_tokens=101,
                output_tokens=17,
                cached_tokens=9,
                request_id="provider-request-7",
            ),
        ),
        clock=ScriptedClock([100.0, 100.012]),
    )

    run_id = "run-usage-facts"
    result = asyncio.run(
        provider.generate(
            provider_request(run_id=run_id, role=ModelRole.SMALL, text="prompt")
        )
    )

    assert result.usage.request_id == "provider-request-7"
    assert result.usage.run_id == run_id
    assert result.usage.provider == "fake-provider"
    assert result.usage.deployment == "small-deployment"
    assert result.usage.model_role is ModelRole.SMALL
    assert result.usage.input_tokens == 101
    assert result.usage.output_tokens == 17
    assert result.usage.cached_tokens == 9
    assert result.usage.calculated_cost is None


def test_cached_tokens_preserved_when_supplied_and_absent_when_unsupplied() -> None:
    """Preserve optional cached-token facts exactly as provided."""
    provider = build_fake_strong_provider(
        provider_name="fake-provider",
        deployment_name="strong-deployment",
        responses=(
            FakeProviderResponse(
                output_text="with-cache",
                input_tokens=30,
                output_tokens=6,
                cached_tokens=4,
            ),
            FakeProviderResponse(
                output_text="without-cache",
                input_tokens=30,
                output_tokens=6,
            ),
        ),
        clock=ScriptedClock([0.0, 0.001, 1.0, 1.001]),
    )

    first = asyncio.run(
        provider.generate(
            provider_request(run_id="run-1", role=ModelRole.STRONG, text="prompt")
        )
    )
    second = asyncio.run(
        provider.generate(
            provider_request(run_id="run-2", role=ModelRole.STRONG, text="prompt")
        )
    )

    assert first.usage.cached_tokens == 4
    assert second.usage.cached_tokens is None


def test_latency_uses_injected_monotonic_clock_without_sleep() -> None:
    """Measure latency from the deterministic injected monotonic clock."""
    provider = build_fake_small_provider(
        provider_name="fake-provider",
        deployment_name="small-deployment",
        responses=(
            FakeProviderResponse(
                output_text="timed",
                input_tokens=5,
                output_tokens=3,
            ),
        ),
        clock=ScriptedClock([42.0, 42.037]),
    )

    result = asyncio.run(
        provider.generate(
            provider_request(run_id="run-latency", role=ModelRole.SMALL, text="prompt")
        )
    )

    assert result.usage.latency_ms == 37


def test_multiple_calls_are_deterministic_and_recorded_in_order() -> None:
    """Cycle configured responses and preserve exact call order history."""
    provider = build_fake_small_provider(
        provider_name="fake-provider",
        deployment_name="small-deployment",
        responses=(
            FakeProviderResponse(
                output_text="response-1",
                input_tokens=1,
                output_tokens=1,
            ),
            FakeProviderResponse(
                output_text="response-2",
                input_tokens=2,
                output_tokens=2,
            ),
        ),
        clock=ScriptedClock([0.0, 0.001, 1.0, 1.001, 2.0, 2.001]),
    )

    first = asyncio.run(
        provider.generate(
            provider_request(run_id="run-1", role=ModelRole.SMALL, text="p1")
        )
    )
    second = asyncio.run(
        provider.generate(
            provider_request(run_id="run-2", role=ModelRole.SMALL, text="p2")
        )
    )
    third = asyncio.run(
        provider.generate(
            provider_request(run_id="run-3", role=ModelRole.SMALL, text="p3")
        )
    )

    assert (first.output_text, second.output_text, third.output_text) == (
        "response-1",
        "response-2",
        "response-1",
    )
    assert tuple(call.sequence for call in provider.calls) == (0, 1, 2)
    assert tuple(call.request.run_id for call in provider.calls) == (
        "run-1",
        "run-2",
        "run-3",
    )


def test_provider_rejects_request_with_wrong_model_role() -> None:
    """Fail fast when SMALL/STRONG role and provider configuration disagree."""
    provider = build_fake_small_provider(
        provider_name="fake-provider",
        deployment_name="small-deployment",
        responses=(
            FakeProviderResponse(
                output_text="x",
                input_tokens=1,
                output_tokens=1,
            ),
        ),
        clock=ScriptedClock([0.0, 0.001]),
    )

    with pytest.raises(ValueError, match="expected SMALL, got STRONG"):
        asyncio.run(
            provider.generate(
                provider_request(run_id="run-1", role=ModelRole.STRONG, text="prompt")
            )
        )


def test_fake_providers_require_no_live_cloud_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run successfully with Azure credential environment variables removed."""
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)

    provider = build_fake_strong_provider(
        provider_name="fake-provider",
        deployment_name="strong-deployment",
        responses=(
            FakeProviderResponse(
                output_text="offline",
                input_tokens=7,
                output_tokens=4,
            ),
        ),
        clock=ScriptedClock([1.0, 1.002]),
    )

    result = asyncio.run(
        provider.generate(
            provider_request(run_id="run-offline", role=ModelRole.STRONG, text="prompt")
        )
    )

    assert result.output_text == "offline"


def test_provider_request_contract_rejects_empty_values() -> None:
    """Validate required provider-independent request contract fields."""
    with pytest.raises(ValidationError):
        ModelProviderRequest(run_id="", model_role=ModelRole.SMALL, input_text="")


def test_fake_response_contract_rejects_negative_token_counts() -> None:
    """Reject impossible usage facts in fake response configuration."""
    with pytest.raises(ValidationError):
        FakeProviderResponse(output_text="x", input_tokens=-1, output_tokens=1)


def test_fake_provider_implements_model_provider_protocol() -> None:
    """Keep fake providers substitutable for the async provider interface."""
    provider = build_fake_small_provider(
        provider_name="fake-provider",
        deployment_name="small-deployment",
        responses=(
            FakeProviderResponse(output_text="x", input_tokens=1, output_tokens=1),
        ),
        clock=ScriptedClock([0.0, 0.001]),
    )

    assert isinstance(provider, ModelProvider)
