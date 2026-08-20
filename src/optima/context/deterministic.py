"""Deterministic local token counting and extractive context reduction."""

import re

from optima.context.contracts import (
    ContextPreservationEvidence,
    ContextReductionRequest,
    ContextReductionResult,
    TokenCounter,
)

_TOKEN_PATTERN = re.compile(r"\w+(?:[-_/]\w+)*|[^\w\s]", re.UNICODE)
_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_IDENTIFIER_PATTERN = re.compile(r"\b[A-Z]{2,}(?:[-_][A-Z0-9]+)+\b")
_FULL_NAME_PATTERN = re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b")
_FACT_PATTERN = re.compile(
    r"\d|\b(?:must|shall|required?|requires?|constraint|depends?|before|after|"
    r"owns?|reports?|blocks?|blocked|only|never|not|without|unless|budget|"
    r"deadline|identifier|incident|phase)\b",
    re.IGNORECASE,
)
_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "before",
        "context",
        "extract",
        "from",
        "into",
        "please",
        "required",
        "summarize",
        "summary",
        "that",
        "their",
        "these",
        "this",
        "with",
    }
)


class RegexTokenCounter:
    """Count lexical and punctuation tokens with a stable local regex."""

    counter_name = "regex-token-counter-v1"

    def count(self, text: str) -> int:
        """Return the deterministic number of lexical and punctuation tokens."""
        return len(_TOKEN_PATTERN.findall(text))


class DeterministicExtractiveReducer:
    """Retain relevant or fact-bearing source lines without rewriting them."""

    reducer_name = "deterministic-extractive-reducer-v1"
    method = "RELEVANCE_AND_FACT_EXTRACTIVE_V1"

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    async def reduce(
        self,
        request: ContextReductionRequest,
    ) -> ContextReductionResult:
        """Select unique relevant or fact-bearing lines in source order."""
        segments = context_segments(request.context)
        if not segments:
            raise ValueError("context must contain a non-empty source segment")

        task_terms = extract_task_terms(request.input_text)
        retained: list[str] = []
        retained_indexes: list[int] = []
        seen: set[str] = set()
        duplicate_count = 0
        irrelevant_count = 0

        for index, segment in enumerate(segments):
            if segment in seen:
                duplicate_count += 1
                continue
            seen.add(segment)
            if not is_retainable_segment(segment, task_terms):
                irrelevant_count += 1
                continue
            retained.append(segment)
            retained_indexes.append(index)

        if not retained:
            raise ValueError("no task-relevant or fact-bearing context was retained")

        reduced_context = "\n".join(retained)
        return ContextReductionResult(
            reduced_context=reduced_context,
            original_token_count=self._token_counter.count(request.context),
            reduced_token_count=self._token_counter.count(reduced_context),
            reducer_name=self.reducer_name,
            method=self.method,
            token_counter_name=self._token_counter.counter_name,
            preservation=ContextPreservationEvidence(
                source_order_preserved=True,
                original_segment_count=len(segments),
                retained_segment_indexes=tuple(retained_indexes),
                removed_duplicate_count=duplicate_count,
                removed_irrelevant_count=irrelevant_count,
                task_terms_used=tuple(sorted(task_terms)),
            ),
        )


def context_segments(context: str) -> tuple[str, ...]:
    """Return non-blank source lines unchanged and in their original order."""
    return tuple(line for line in context.splitlines() if line.strip())


def extract_task_terms(input_text: str) -> frozenset[str]:
    """Return normalized task terms used by deterministic relevance checks."""
    return frozenset(
        term.casefold()
        for term in _WORD_PATTERN.findall(input_text)
        if len(term) >= 4 and term.casefold() not in _STOP_WORDS
    )


def is_retainable_segment(segment: str, task_terms: frozenset[str]) -> bool:
    """Return whether deterministic extraction retains one unique source line."""
    segment_terms = {term.casefold() for term in _WORD_PATTERN.findall(segment)}
    is_relevant = bool(task_terms & segment_terms)
    is_fact_bearing = bool(
        _FACT_PATTERN.search(segment)
        or _IDENTIFIER_PATTERN.search(segment)
        or _FULL_NAME_PATTERN.search(segment)
    )
    return is_relevant or is_fact_bearing
