"""Run the bounded pre-exposure OPTIMA production smoke check."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence

import httpx

from optima.domain.execution import ModelPolicy, ModelRole
from optima.domain.run import RunResult, RunStatus
from ui.api_client import API_BASE_URL_ENV, API_TIMEOUT_SECONDS_ENV

PERSISTENCE_HEADER = "X-OPTIMA-Run-History"
SUCCESS_MARKER = "OPTIMA_DEPLOYMENT_SMOKE_PASSED"
TRACEPARENT_PATTERN = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _request_payload(
    *, complexity: str, cache_eligible: bool, marker: str
) -> dict[str, object]:
    return {
        "input_text": f"Deployment trace {marker}. Return only DEPLOYMENT_SMOKE_OK.",
        "context": "This is an authorized production deployment smoke test.",
        "request_profile": {
            "task_type": "GENERAL_REASONING",
            "complexity": complexity,
            "input_tokens": 24,
            "risk_tier": "LOW",
            "cache_eligible": cache_eligible,
            "has_large_context": False,
        },
        "quality_profile": "STANDARD",
        "optimization_mode": "COST",
        "risk_tier": "LOW",
        "criteria": ["The output is exactly DEPLOYMENT_SMOKE_OK."],
        "metadata": {"scenario": "deployment-smoke"},
    }


def _execute(
    client: httpx.Client,
    *,
    traceparent: str,
    complexity: str,
    cache_eligible: bool,
    marker: str,
) -> RunResult:
    response = client.post(
        "/api/v1/runs",
        json=_request_payload(
            complexity=complexity,
            cache_eligible=cache_eligible,
            marker=marker,
        ),
        headers={"traceparent": traceparent},
    )
    response.raise_for_status()
    if response.headers.get(PERSISTENCE_HEADER) != "PERSISTED":
        raise RuntimeError("Production smoke run was not persisted")
    result = RunResult.model_validate(response.json())
    if result.status is not RunStatus.COMPLETED:
        raise RuntimeError("Production smoke run did not complete")
    if result.contract_met is not True:
        raise RuntimeError("Production smoke run did not meet its quality contract")
    if result.final_output != "DEPLOYMENT_SMOKE_OK":
        raise RuntimeError("Production smoke output did not match the exact contract")
    if result.final_evaluation is None or result.final_evaluation.passed is not True:
        raise RuntimeError("Production smoke evaluation did not pass")
    if result.final_evaluation.evaluator_type != "llm_judge":
        raise RuntimeError("Production smoke did not use the LLM judge")
    if result.total_calculated_cost is None or result.total_cost_provenance is None:
        raise RuntimeError("Production smoke cost evidence is incomplete")
    return result


def run_smoke(
    *,
    client: httpx.Client,
    traceparent: str,
    marker: str,
) -> None:
    """Exercise SMALL, STRONG, JUDGE, embedding, persistence, and pricing."""
    if TRACEPARENT_PATTERN.fullmatch(traceparent) is None:
        raise ValueError("Production smoke traceparent is invalid")
    _, trace_id, parent_id, _ = traceparent.split("-")
    if trace_id == ("0" * 32) or parent_id == ("0" * 16):
        raise ValueError("Production smoke traceparent contains an invalid zero ID")
    small = _execute(
        client,
        traceparent=traceparent,
        complexity="LOW",
        cache_eligible=False,
        marker=marker,
    )
    if small.final_output != "DEPLOYMENT_SMOKE_OK":
        raise RuntimeError("Production smoke output did not match the exact contract")
    small_roles = {usage.model_role for usage in small.model_usages}
    if small_roles != {ModelRole.SMALL, ModelRole.JUDGE} or small.escalated:
        raise RuntimeError("Production smoke did not exercise SMALL and JUDGE")

    strong = _execute(
        client,
        traceparent=traceparent,
        complexity="HIGH",
        cache_eligible=True,
        marker=marker,
    )
    if strong.final_output != "DEPLOYMENT_SMOKE_OK":
        raise RuntimeError("Production smoke output did not match the exact contract")
    strong_roles = {usage.model_role for usage in strong.model_usages}
    if not {ModelRole.STRONG, ModelRole.JUDGE}.issubset(strong_roles):
        raise RuntimeError("Production smoke did not exercise STRONG and JUDGE")
    if strong.execution_plan.model_policy is not ModelPolicy.STRONG_DIRECT:
        raise RuntimeError("Production smoke HIGH request was not strong-direct")
    semantic_cache = strong.semantic_cache
    embedding_attempt = (
        semantic_cache.embedding_attempt if semantic_cache is not None else None
    )
    if (
        embedding_attempt is None
        or embedding_attempt.outbound_attempted is not True
        or embedding_attempt.usage is None
        or embedding_attempt.usage.calculated_cost is None
    ):
        raise RuntimeError("Production smoke embedding evidence is incomplete")


def create_parser() -> argparse.ArgumentParser:
    """Create the bounded production smoke command parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traceparent", required=True)
    parser.add_argument("--run-marker", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Load environment configuration and execute the production smoke check."""
    arguments = create_parser().parse_args(argv)
    try:
        base_url = _required(os.environ, API_BASE_URL_ENV)
        timeout_seconds = float(_required(os.environ, API_TIMEOUT_SECONDS_ENV))
        with httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            follow_redirects=False,
        ) as client:
            run_smoke(
                client=client,
                traceparent=arguments.traceparent,
                marker=arguments.run_marker,
            )
        print(SUCCESS_MARKER)
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        print(f"Production smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
