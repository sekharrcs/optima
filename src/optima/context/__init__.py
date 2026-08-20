"""Provider-independent context reduction contracts and local implementations."""

from optima.context.contracts import (
    ContextPreservationEvidence,
    ContextReducer,
    ContextReductionRequest,
    ContextReductionResult,
    TokenCounter,
)
from optima.context.deterministic import (
    DeterministicExtractiveReducer,
    RegexTokenCounter,
)
from optima.context.fakes import FakeContextReducer, RecordingTokenCounter

__all__ = [
    "ContextPreservationEvidence",
    "ContextReducer",
    "ContextReductionRequest",
    "ContextReductionResult",
    "DeterministicExtractiveReducer",
    "FakeContextReducer",
    "RegexTokenCounter",
    "RecordingTokenCounter",
    "TokenCounter",
]
