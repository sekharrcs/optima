"""Static security and ordering contracts for the production deployment workflow."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"


def workflow() -> str:
    """Read the production workflow as normalized UTF-8 text."""
    return WORKFLOW.read_text(encoding="utf-8")


def test_production_deployment_is_manual_serialized_and_environment_gated() -> None:
    """Prevent automatic or concurrent production rollouts."""
    content = workflow()

    assert "workflow_dispatch:" in content
    assert "pull_request:" not in content
    assert "push:" not in content
    assert "group: optima-production" in content
    assert "cancel-in-progress: false" in content
    assert "environment: hackathon" in content
    assert "confirm_commit_sha:" in content
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in content
    assert "environment_url=" not in content
    assert 'CONFIRM_DEPLOYMENT: "${{ inputs.confirm_deployment }}"' in content
    assert 'test "$CONFIRM_DEPLOYMENT" = "DEPLOY"' in content
    run_blocks = re.findall(r"run: \|\n((?:\s{10}.*\n)+)", content)
    assert run_blocks
    assert all("${{ inputs." not in block for block in run_blocks)


def test_azure_oidc_permission_exists_only_after_validation() -> None:
    """Keep Azure identity unavailable to source validation and container builds."""
    content = workflow()

    assert content.count("id-token: write") == 1
    assert content.index("uv run pytest") < content.index("id-token: write")
    assert content.index("id-token: write") < content.index("azure/login@")
    assert content.index("azure/login@") < content.index("--phase foundation")
    assert "client-secret:" not in content
    assert "persist-credentials: false" in content


def test_all_actions_are_pinned_to_full_commit_shas() -> None:
    """Reject mutable GitHub Action tags and unpinned third-party actions."""
    action_references = re.findall(r"^\s*- uses: ([^\s]+)$", workflow(), re.MULTILINE)

    assert action_references
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference)
        for reference in action_references
    )
    assert "azure/login@a457da9ea143d694b1b9c7c869ebb04ebe844ef5" in action_references
    # eec3c95 is the v2.3.0 annotated tag object, not a commit; GitHub resolves
    # a `uses:` SHA as a commit, so an unpeeled tag-object pin must never return.
    assert "eec3c95657c1536435858eda1f3ff5437fee8474" not in workflow()


def test_preflight_and_mutation_order_is_fail_closed() -> None:
    """Run each read-only gate before the mutation it authorizes."""
    content = workflow()
    foundation = content.index("--phase foundation")
    foundation_create = content.index("az deployment group create", foundation)
    publish = content.index("--phase publish", foundation_create)
    image_push = content.index('docker push "$api_image"', publish)
    artifacts = content.index("--phase artifacts", image_push)
    rollout = content.index("--phase rollout", artifacts)
    applications = content.index('"deployContainerApps=true"', rollout)

    assert foundation < foundation_create < publish < image_push
    assert image_push < artifacts < rollout < applications
    assert "deployRuntimeAccess=true" not in content
    assert "az deployment group what-if" in content
    assert "az deployment sub what-if" in content


def test_images_are_built_for_amd64_and_deployed_by_registry_digest() -> None:
    """Use one commit tag for publication and only manifest digests for rollout."""
    content = workflow()

    assert content.count("--platform linux/amd64") == 2
    assert 'api_image="$registry/optima-api:$GITHUB_SHA"' in content
    assert 'ui_image="$registry/optima-ui:$GITHUB_SHA"' in content
    assert "optima-api:latest" not in content
    assert "optima-ui:latest" not in content
    assert "exact-images.tar" in content
    assert '"$tools/gitleaks" git --redact=100 .' in content
    assert "docker image load --input exact-images.tar" in content
    assert '"$evidence/$component-trivy.json"' in content
    assert "--exit-code 1" in content
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in content
    assert (
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in content
    )
    assert '"apiImageDigest=$API_DIGEST"' in content
    assert '"uiImageDigest=$UI_DIGEST"' in content
    assert 'test "$api_image" = "$REGISTRY/optima-api@$API_DIGEST"' in content
    assert 'test "$ui_image" = "$REGISTRY/optima-ui@$UI_DIGEST"' in content


def test_rollout_records_source_and_verifies_runtime_contracts() -> None:
    """Trace revisions to source and verify readiness, routing, and telemetry."""
    content = workflow()
    smoke = (ROOT / "src" / "ui" / "deployment_smoke.py").read_text(encoding="utf-8")

    assert '"deploymentCommitSha=$GITHUB_SHA"' in content
    assert '"deploymentWorkflowRunId=$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"' in content
    assert "properties.latestRevisionName" in content
    assert 'test "$api_health" = "Healthy"' in content
    assert 'test "$ui_health" = "Healthy"' in content
    assert 'test "$external_api" = "false"' in content
    assert 'test "$configured_api_url" = "$API_URL"' in content
    assert "az containerapp exec" not in content
    assert "az containerapp job start" in content
    assert "az containerapp job execution list" in content
    assert 'test "$smoke_status" = "Succeeded"' in content
    assert "az monitor app-insights query" in content
    assert "operation_Id == '$trace_id'" in content
    assert "small_roles != {ModelRole.SMALL, ModelRole.JUDGE}" in smoke
    assert "{ModelRole.STRONG, ModelRole.JUDGE}.issubset(strong_roles)" in smoke
    assert "embedding_attempt.usage is None" in smoke
    assert 'response.headers.get(PERSISTENCE_HEADER) != "PERSISTED"' in smoke
    assert "result.contract_met is not True" in smoke
    assert "result.final_evaluation.passed is not True" in smoke
    assert "result.total_calculated_cost is None" in smoke
    assert "GITHUB_STEP_SUMMARY" in content


def test_ui_is_internal_until_easy_auth_is_verified() -> None:
    """Prevent a partial ARM failure from exposing unauthenticated Streamlit."""
    content = workflow()
    internal = content.index("exposePublicUi=false", content.index("--phase rollout"))
    auth = content.index("authConfigs/current", internal)
    live_execution = content.index("az containerapp job start", auth)
    telemetry = content.index("operation_Id == '$trace_id'", live_execution)
    public = content.index("exposePublicUi=true", auth)

    assert internal < auth < live_execution < telemetry < public
    assert 'clientSecretSettingName == "ui-auth-client-secret"' in content
    assert "az containerapp ingress update" in content
    assert "--type internal" in content
    assert 'test "$contained" = "true"' in content


def test_pre_exposure_smoke_uses_a_container_apps_job() -> None:
    """Gate exposure on a job execution status, not a shell exec into distroless.

    The UI runtime image is distroless (no shell), so ``az containerapp exec``
    cannot run the smoke inside it. The workflow starts a one-shot job and
    requires a ``Succeeded`` execution before enabling external ingress.
    """
    content = workflow()

    assert "az containerapp exec" not in content
    assert '"smokeTraceparent=00-$trace_id-$parent_id-01"' in content
    assert '"smokeRunMarker=$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"' in content
    assert "properties.outputs.smokeJobName.value" in content
    start = content.index("az containerapp job start")
    status = content.index("properties.status", start)
    gate = content.index('test "$smoke_status" = "Succeeded"', status)
    telemetry = content.index("operation_Id == '$trace_id'", gate)
    public = content.index("exposePublicUi=true", gate)
    assert start < status < gate < telemetry < public


def test_failed_rollout_restores_previous_revision_pair() -> None:
    """Contain first failures and reactivate both prior immutable revisions."""
    content = workflow()

    assert "Capture the active revision pair for automatic rollback" in content
    assert "Restore the previous ready revision pair" in content
    assert "UI container app is absent" in content
    assert content.count("failure() || cancelled()") == 2
    assert content.count("az containerapp revision copy") == 2
    assert '--from-revision "$PREVIOUS_API_REVISION"' in content
    assert '--from-revision "$PREVIOUS_UI_REVISION"' in content
    assert content.count("az containerapp revision show --name") >= 6
    assert content.count('--revision "$') >= 6
    assert "--revision-weight" not in content
    assert (
        'restored_api_revision="ca-optima-api-hackathon--$rollback_suffix"' in content
    )
    assert 'restored_ui_revision="ca-optima-ui-hackathon--$rollback_suffix"' in content
    assert 'test "$state" = "Succeeded|Healthy"' in content
    assert 'test "$healthy" = "true"' in content
    assert "PREVIOUS_API_IMAGE" in content
    assert "PREVIOUS_UI_IMAGE" in content
    rollback = content.index("Restore the previous ready revision pair")
    assert "az containerapp ingress enable" not in content[rollback:]


def test_ui_secret_comes_only_from_the_environment_secret() -> None:
    """Keep the confidential-client credential out of ordinary variables."""
    content = workflow()

    assert (
        'OPTIMA_UI_AUTH_CLIENT_SECRET: "${{ secrets.OPTIMA_UI_AUTH_CLIENT_SECRET }}"'
        in content
    )
    assert "vars.OPTIMA_UI_AUTH_CLIENT_SECRET" not in content
    assert "uiAuthClientSecret=$OPTIMA_UI_AUTH_CLIENT_SECRET" in content
    output_commands = re.findall(r"^\s*(?:echo|printf)\b.*$", content, re.MULTILINE)
    assert all("OPTIMA_UI_AUTH_CLIENT_SECRET" not in line for line in output_commands)


def test_bicep_revisions_share_commit_and_workflow_provenance() -> None:
    """Keep API, UI, tags, telemetry, and revision suffix on one source identity."""
    module = (ROOT / "infra" / "modules" / "container-apps.bicep").read_text(
        encoding="utf-8"
    )
    resources = (ROOT / "infra" / "resource-group.bicep").read_text(encoding="utf-8")

    assert "sourceCommit: deploymentCommitSha" in module
    assert "workflowRun: deploymentWorkflowRunId" in module
    assert "var revisionSuffix = 'r-${take(deploymentCommitSha" in module
    assert module.count("revisionSuffix: revisionSuffix") == 2
    assert "name: 'OPTIMA_APPLICATION_INSIGHTS_SERVICE_VERSION'" in module
    assert "value: deploymentCommitSha" in module
    assert "output apiRevisionName string" in module
    assert "output uiRevisionName string" in module
    assert "external: exposePublicUi" in module
    assert "deploymentProvenanceIsDeployable" in resources
    assert "requires an exact commit SHA and workflow run ID" in resources
