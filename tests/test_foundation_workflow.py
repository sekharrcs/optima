"""Static safety contracts for the separated foundation plan and apply workflow."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "foundation.yml"
PRODUCTION_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"

APPLICATION_TOKENS = (
    "docker build",
    "docker push",
    "az acr login",
    "az containerapp",
    "deployContainerApps=true",
    "deployRuntimeAccess=true",
    "exposePublicUi=true",
    "authConfigs",
    "redisEmbedding",
    "uiAuthClientSecret",
)


def workflow() -> str:
    """Read the foundation workflow as normalized UTF-8 text."""
    return WORKFLOW.read_text(encoding="utf-8")


def _job_text(content: str, job: str, following: str | None) -> str:
    """Return the text region belonging to one job definition."""
    start = content.index(f"\n  {job}:")
    end = content.index(f"\n  {following}:") if following is not None else len(content)
    return content[start:end]


def plan_job() -> str:
    """Return the foundation-plan job text."""
    return _job_text(workflow(), "foundation-plan", "foundation-apply")


def apply_job() -> str:
    """Return the foundation-apply job text."""
    return _job_text(workflow(), "foundation-apply", None)


def test_workflow_is_manual_serialized_and_operation_gated() -> None:
    """Dispatch two mutually exclusive operations without automatic triggers."""
    content = workflow()

    assert "workflow_dispatch:" in content
    assert "push:" not in content
    assert "pull_request:" not in content
    assert "group: optima-foundation" in content
    assert "cancel-in-progress: false" in content
    assert "default: foundation-plan" in content
    assert "if: ${{ inputs.operation == 'foundation-plan' }}" in content
    assert "if: ${{ inputs.operation == 'foundation-apply' }}" in content


def test_plan_and_apply_conditions_are_mutually_exclusive() -> None:
    """Keep exactly one operation condition on each job so neither co-runs."""
    assert plan_job().count("if: ${{ inputs.operation ==") == 1
    assert "foundation-plan" in plan_job().split("steps:")[0]
    assert apply_job().count("if: ${{ inputs.operation ==") == 1
    assert "foundation-apply" in apply_job().split("steps:")[0]


def test_all_actions_are_pinned_to_full_commit_shas() -> None:
    """Reject mutable action tags across the foundation workflow."""
    references = re.findall(r"^\s*- uses: ([^\s]+)$", workflow(), re.MULTILINE)

    assert references
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference) for reference in references
    )


def test_oidc_permission_is_minimal_and_present_per_job() -> None:
    """Grant id-token only inside the two environment-gated foundation jobs."""
    content = workflow()

    assert content.count("id-token: write") == 2
    assert content.count("environment: hackathon") == 2
    assert "client-secret:" not in content
    assert "secrets." not in content
    assert "persist-credentials: false" in content


def test_plan_job_is_read_only() -> None:
    """Keep foundation planning free of confirmation, mutation, and publication."""
    plan = plan_job()

    assert "--phase foundation" in plan
    assert plan.count("az deployment group what-if") == 1
    assert "az deployment group create" not in plan
    assert "az deployment sub create" not in plan
    assert "whatif_classification.py classify" in plan
    assert '"DEPLOY"' not in plan
    assert "confirm_deployment" not in plan
    assert "actions: read" not in plan
    for token in APPLICATION_TOKENS:
        assert token not in plan
    assert "deployContainerApps=false" in plan
    assert "exposePublicUi=false" in plan
    assert "deployRuntimeAccess=false" in plan
    assert "semanticCacheEnabled=false" in plan


def test_plan_job_uploads_sanitized_plan_evidence() -> None:
    """Emit a commit-scoped plan evidence artifact for later review."""
    plan = plan_job()

    assert "foundation-plan-evidence.json" in plan
    assert "name: foundation-plan-evidence-${{ github.sha }}" in plan
    assert "if-no-files-found: error" in plan


def test_apply_job_requires_explicit_promotion_authorization() -> None:
    """Require the confirmation, commit, and plan-evidence promotion gates."""
    apply = apply_job()

    assert "APPLY-FOUNDATION" in apply
    assert 'test "${#CONFIRMED_SHA}" -eq 40' in apply
    assert 'test "$CONFIRMED_SHA" = "$GITHUB_SHA"' in apply
    assert "download-artifact@" in apply
    assert 'run-id: "${{ inputs.plan_run_id }}"' in apply
    assert "whatif_classification.py promote-check" in apply
    assert "actions: read" in apply


def test_apply_job_deploys_only_the_foundation_and_stops() -> None:
    """Deploy foundation resources exactly once and never continue to rollout."""
    apply = apply_job()

    assert apply.count("az deployment group create") == 1
    assert "--phase foundation" in apply
    assert "whatif_classification.py classify" in apply
    for token in APPLICATION_TOKENS:
        assert token not in apply
    assert "--phase publish" not in apply
    assert "--phase rollout" not in apply
    assert "deployContainerApps=false" in apply


def test_run_blocks_never_interpolate_workflow_inputs() -> None:
    """Route dispatch inputs through env to prevent shell injection."""
    run_blocks = re.findall(r"run: \|\n((?:\s{10}.*\n)+)", workflow())

    assert run_blocks
    assert all("${{ inputs." not in block for block in run_blocks)
    assert all("${{ secrets." not in block for block in run_blocks)


def test_production_workflow_remains_the_separate_full_rollout_path() -> None:
    """Keep the untouched full-production workflow as the later rollout stage."""
    production = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")

    assert 'test "$CONFIRM_DEPLOYMENT" = "DEPLOY"' in production
    assert 'docker push "$api_image"' in production
    assert production.count("id-token: write") == 1
