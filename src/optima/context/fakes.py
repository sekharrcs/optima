"""Deterministic context reducer fake for tests and local composition."""

from optima.context.contracts import (
    ContextReductionRequest,
    ContextReductionResult,
    TokenCounter,
)


class FakeContextReducer:
    """Return configured results or raise configured errors in call order."""

    reducer_name = "fake-context-reducer"

    def __init__(
        self,
        outcomes: tuple[ContextReductionResult | Exception, ...],
    ) -> None:
        if not outcomes:
            raise ValueError("fake context reducer requires at least one outcome")
        self._outcomes = outcomes
        self._calls: list[ContextReductionRequest] = []

    @property
    def calls(self) -> tuple[ContextReductionRequest, ...]:
        """Return requests in exact invocation order."""
        return tuple(self._calls)

    async def reduce(
        self,
        request: ContextReductionRequest,
    ) -> ContextReductionResult:
        """Record the call and return or raise the configured outcome."""
        self._calls.append(request)
        outcome = self._outcomes[(len(self._calls) - 1) % len(self._outcomes)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RecordingTokenCounter:
    """Delegate deterministic counts while recording exact measured strings."""

    def __init__(self, counter: TokenCounter) -> None:
        self.counter_name = counter.counter_name
        self._counter = counter
        self._calls: list[str] = []

    @property
    def calls(self) -> tuple[str, ...]:
        """Return measured strings in exact invocation order."""
        return tuple(self._calls)

    def count(self, text: str) -> int:
        """Record one string and return its delegated deterministic count."""
        self._calls.append(text)
        return self._counter.count(text)
