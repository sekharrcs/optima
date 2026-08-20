"""Tests for centralized exact model-usage cost calculation."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from optima.cost import CostCalculator, PriceCatalog, PriceCatalogEntry
from optima.domain.execution import ModelRole
from optima.domain.run import ModelUsage


def price_entry(**updates: object) -> PriceCatalogEntry:
    """Build one valid provider/deployment price entry."""
    values: dict[str, object] = {
        "provider": "foundry",
        "deployment": "small-deployment",
        "input_rate_per_million_tokens": Decimal("1.25"),
        "cached_input_rate_per_million_tokens": Decimal("0.125"),
        "output_rate_per_million_tokens": Decimal("5.00"),
    }
    values.update(updates)
    return PriceCatalogEntry.model_validate(values)


def price_catalog(*entries: PriceCatalogEntry) -> PriceCatalog:
    """Build one versioned USD catalog."""
    return PriceCatalog(version="2026-08-20", currency="USD", entries=entries)


def model_usage(**updates: object) -> ModelUsage:
    """Build one measured provider usage record."""
    values: dict[str, object] = {
        "request_id": "provider-request-1",
        "run_id": "run-1",
        "provider": "foundry",
        "deployment": "small-deployment",
        "model_role": ModelRole.SMALL,
        "input_tokens": 1_000,
        "output_tokens": 200,
        "cached_tokens": 100,
        "latency_ms": 125,
    }
    values.update(updates)
    return ModelUsage.model_validate(values)


def test_catalog_preserves_immutable_metadata_and_tuple_entries() -> None:
    """Keep exact catalog identity and tuple-backed rates immutable."""
    entry = price_entry()
    catalog = price_catalog(entry)

    assert catalog.version == "2026-08-20"
    assert catalog.currency == "USD"
    assert catalog.entries == (entry,)
    with pytest.raises(ValidationError, match="frozen"):
        catalog.currency = "EUR"
    with pytest.raises(ValidationError, match="frozen"):
        entry.provider = "other-provider"


@pytest.mark.parametrize("field", ["provider", "deployment"])
def test_catalog_entry_rejects_empty_identity(field: str) -> None:
    """Require a nonempty exact provider and deployment identity."""
    with pytest.raises(ValidationError):
        price_entry(**{field: ""})


@pytest.mark.parametrize("field", ["version", "currency"])
def test_catalog_rejects_empty_version_or_currency(field: str) -> None:
    """Require explicit nonempty catalog metadata."""
    values: dict[str, object] = {"version": "2026-08-20", "currency": "USD"}
    values[field] = ""

    with pytest.raises(ValidationError):
        PriceCatalog.model_validate(values)


@pytest.mark.parametrize(
    ("field", "rate"),
    [
        ("input_rate_per_million_tokens", Decimal("-0.01")),
        ("input_rate_per_million_tokens", Decimal("NaN")),
        ("output_rate_per_million_tokens", Decimal("-0.01")),
        ("output_rate_per_million_tokens", Decimal("Infinity")),
        ("cached_input_rate_per_million_tokens", Decimal("-0.01")),
        ("cached_input_rate_per_million_tokens", Decimal("-Infinity")),
    ],
)
def test_catalog_entry_rejects_negative_or_nonfinite_rates(
    field: str,
    rate: Decimal,
) -> None:
    """Reject every invalid configured per-million-token rate."""
    with pytest.raises(ValidationError):
        price_entry(**{field: rate})


def test_catalog_rejects_duplicate_provider_deployment_keys() -> None:
    """Prevent ambiguous pricing for an exact deployment identity."""
    with pytest.raises(ValidationError, match="price keys must be unique"):
        price_catalog(price_entry(), price_entry())


def test_calculator_uses_exact_decimal_rates_for_each_token_category() -> None:
    """Calculate uncached input, cached input, and output without float math."""
    calculator = CostCalculator(
        price_catalog(
            price_entry(
                input_rate_per_million_tokens=Decimal("0.123456789"),
                cached_input_rate_per_million_tokens=Decimal("0.0123456789"),
                output_rate_per_million_tokens=Decimal("0.987654321"),
            )
        )
    )

    calculation = calculator.calculate(
        model_usage(input_tokens=11, cached_tokens=3, output_tokens=7)
    )

    assert calculation is not None
    assert calculation.amount == Decimal("0.0000079382715957")
    assert isinstance(calculation.amount, Decimal)
    assert calculation.provenance.catalog_version == "2026-08-20"
    assert calculation.provenance.currency == "USD"


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "input_rate", "output_rate", "expected"),
    [
        (1_000, 0, Decimal("2"), Decimal("9"), Decimal("0.002")),
        (0, 1_000, Decimal("9"), Decimal("3"), Decimal("0.003")),
        (1_000, 1_000, Decimal("2"), Decimal("3"), Decimal("0.005")),
        (0, 0, Decimal("2"), Decimal("3"), Decimal("0")),
        (1_000, 1_000, Decimal("0"), Decimal("0"), Decimal("0")),
    ],
)
def test_calculator_handles_individual_categories_and_zero_values(
    input_tokens: int,
    output_tokens: int,
    input_rate: Decimal,
    output_rate: Decimal,
    expected: Decimal,
) -> None:
    """Cover input-only, output-only, combined, zero usage, and zero pricing."""
    calculator = CostCalculator(
        price_catalog(
            price_entry(
                input_rate_per_million_tokens=input_rate,
                cached_input_rate_per_million_tokens=None,
                output_rate_per_million_tokens=output_rate,
            )
        )
    )

    calculation = calculator.calculate(
        model_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=None,
        )
    )

    assert calculation is not None
    assert calculation.amount == expected


def test_calculator_does_not_double_count_cached_input_tokens() -> None:
    """Partition cached tokens out of input before applying distinct rates."""
    calculator = CostCalculator(
        price_catalog(
            price_entry(
                input_rate_per_million_tokens=Decimal("2"),
                cached_input_rate_per_million_tokens=Decimal("0.5"),
                output_rate_per_million_tokens=Decimal("4"),
            )
        )
    )

    calculation = calculator.calculate(
        model_usage(input_tokens=1_000, cached_tokens=250, output_tokens=100)
    )

    assert calculation is not None
    assert calculation.amount == Decimal("0.002025")


def test_calculator_requires_cached_measurement_for_distinct_cached_rate() -> None:
    """Return unavailable when distinct cached pricing lacks measured usage."""
    calculator = CostCalculator(price_catalog(price_entry()))

    assert calculator.calculate(model_usage(cached_tokens=None)) is None


def test_calculator_accepts_explicit_zero_cached_tokens() -> None:
    """Distinguish a measured zero cached subset from missing measurement."""
    calculator = CostCalculator(price_catalog(price_entry()))

    calculation = calculator.calculate(
        model_usage(input_tokens=1_000, cached_tokens=0, output_tokens=200)
    )

    assert calculation is not None
    assert calculation.amount == Decimal("0.00225")


@pytest.mark.parametrize("cached_tokens", [None, 100])
def test_calculator_uses_normal_input_rate_without_distinct_cached_pricing(
    cached_tokens: int | None,
) -> None:
    """Price all input normally when no separate cached rate is configured."""
    calculator = CostCalculator(
        price_catalog(price_entry(cached_input_rate_per_million_tokens=None))
    )

    calculation = calculator.calculate(model_usage(cached_tokens=cached_tokens))

    assert calculation is not None
    assert calculation.amount == Decimal("0.00225")


@pytest.mark.parametrize(
    ("provider", "deployment"),
    [
        ("unknown", "small-deployment"),
        ("foundry", "unknown"),
        ("Foundry", "small-deployment"),
    ],
)
def test_calculator_returns_none_for_unknown_exact_price_key(
    provider: str,
    deployment: str,
) -> None:
    """Keep absent exact catalog pricing unavailable rather than zero."""
    calculator = CostCalculator(price_catalog(price_entry()))

    assert (
        calculator.calculate(model_usage(provider=provider, deployment=deployment))
        is None
    )


def test_model_usage_rejects_cached_tokens_greater_than_input_tokens() -> None:
    """Require cached tokens to remain a subset of measured input tokens."""
    with pytest.raises(ValidationError, match="cached_tokens must not exceed"):
        model_usage(input_tokens=99, cached_tokens=100)


def test_model_usage_allows_cached_tokens_equal_to_input_tokens() -> None:
    """Allow a fully cached measured input without negative uncached usage."""
    usage = model_usage(input_tokens=100, cached_tokens=100)

    assert usage.cached_tokens == usage.input_tokens
