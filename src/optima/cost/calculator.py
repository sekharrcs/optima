"""Exact model-usage cost calculation."""

from decimal import Decimal

from optima.cost.models import CalculatedCost, PriceCatalog
from optima.domain.run import ModelUsage, PricingProvenance

TOKENS_PER_MILLION = Decimal("1000000")


class CostCalculator:
    """Calculate actual usage cost from one immutable price catalog."""

    def __init__(self, catalog: PriceCatalog) -> None:
        self._catalog = catalog

    def calculate(self, usage: ModelUsage) -> CalculatedCost | None:
        """Return exact cost and catalog identity, or None when unavailable."""
        entry = self._catalog.find_entry(
            provider=usage.provider,
            deployment=usage.deployment,
        )
        if entry is None:
            return None

        cached_rate = entry.cached_input_rate_per_million_tokens
        if cached_rate is None:
            input_cost = usage.input_tokens * entry.input_rate_per_million_tokens
        else:
            if usage.cached_tokens is None:
                return None
            uncached_input_tokens = usage.input_tokens - usage.cached_tokens
            input_cost = (
                uncached_input_tokens * entry.input_rate_per_million_tokens
                + usage.cached_tokens * cached_rate
            )

        output_cost = usage.output_tokens * entry.output_rate_per_million_tokens
        return CalculatedCost(
            amount=(input_cost + output_cost) / TOKENS_PER_MILLION,
            provenance=PricingProvenance(
                catalog_version=self._catalog.version,
                currency=self._catalog.currency,
            ),
        )
