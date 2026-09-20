"""Static security and ordering contracts for the production deployment workflow."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
BASH = shutil.which("bash")


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
    pytest_command = "uv run --no-sync python -m pytest"

    assert content.count("id-token: write") == 1
    assert content.index("uv sync --frozen --all-groups") < content.index(
        pytest_command
    )
    assert content.index(pytest_command) < content.index("id-token: write")
    assert "uv run pytest" not in content
    assert "PYTHONPATH" not in content
    assert content.index("id-token: write") < content.index("azure/login@")
    assert content.index("azure/login@") < content.index(
        "--phase production-foundation"
    )
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
    session = content.index("--phase production-session")
    parameters = content.index(
        "Generate one canonical effective runtime parameter artifact"
    )
    whatif = content.index(
        "az deployment group what-if --name optima-production-foundation-whatif"
    )
    classification = content.index("scripts/whatif_classification.py classify")
    foundation = content.index("--phase production-foundation")
    foundation_create = content.index("az deployment group create", foundation)
    publish = content.index("--phase publish", foundation_create)
    image_push = content.index('docker push "$api_image"', publish)
    artifacts = content.index("--phase artifacts", image_push)
    rollout = content.index("--phase rollout", artifacts)
    applications = content.index("classify-rollout", rollout)

    assert session < parameters < whatif < classification < foundation
    assert foundation < foundation_create < publish < image_push
    assert image_push < artifacts < rollout < applications
    assert "deployRuntimeAccess=true" not in content
    assert "az deployment sub what-if" not in content
    assert "az deployment sub create" not in content


def test_classified_whatif_runs_before_preflight_and_all_mutation() -> None:
    """The authoritative foundation what-if and classifier precede every mutation."""
    content = workflow()
    login = content.index("azure/login@")
    whatif = content.index(
        "az deployment group what-if --name optima-production-foundation-whatif"
    )
    classify = content.index("scripts/whatif_classification.py classify", whatif)
    evidence = content.index("production-foundation-evidence.json", classify)
    preflight = content.index("--phase production-foundation", evidence)
    consume = content.index(
        '--classified-evidence "$RUNNER_TEMP/production-foundation-evidence.json"',
        preflight,
    )
    create = content.index("az deployment group create", consume)
    image_push = content.index('docker push "$api_image"', create)

    # login -> authoritative what-if -> classify -> preflight consumes evidence
    assert login < whatif < classify < evidence < preflight < consume
    # no foundation deployment or image push before classification and preflight
    assert consume < create < image_push
    assert content.index("--expected-commit-sha", preflight) < create
    assert "- uses:" not in content[classify:consume]


def test_exactly_one_foundation_whatif_and_create_share_parameters() -> None:
    """One exact parameter artifact authorizes one foundation create."""
    content = workflow()

    assert (
        content.count(
            "az deployment group what-if --name optima-production-foundation-whatif"
        )
        == 1
    )
    assert content.count("Deploy the exactly authorized runtime foundation") == 1
    # F6: both the foundation what-if and create consume the same immutable
    # artifact through the descriptor-pinning helper, never a re-openable path.
    assert content.count('--parameter-file "$parameters"') == 2
    assert content.count('--parameters "@$RUNNER_TEMP/production-foundation') == 0
    assert content.count("python scripts/production_parameters.py") == 1
    assert content.count("parameter_sha256=") == 1
    assert content.count("infra/environments/hackathon.foundation.bicepparam") == 0
    authorization = content[
        content.index(
            "Generate one canonical effective runtime parameter artifact"
        ) : content.index("Require foundation resources and ACR publication access")
    ]
    assert authorization.count("infra/environments/hackathon.runtime.bicepparam") == 2
    assert "--validation-level ProviderNoRbac" in content
    assert "--result-format FullResourcePayloads" in content


def test_every_three_role_preflight_consumes_classified_evidence() -> None:
    """Each ACR-consuming preflight binds the same-job classified evidence."""
    content = workflow()
    for phase in ("production-foundation", "publish", "artifacts", "rollout"):
        phase_index = content.index(f"--phase {phase}")
        window = content[phase_index : phase_index + 1100]
        assert "production-foundation-evidence.json" in window
        assert "production-foundation-whatif.json" in window
        assert "production-foundation.parameters.json" in window
        assert "--classified-evidence-sha256" in window
        assert "--raw-whatif-sha256" in window
        assert "--effective-parameters-sha256" in window
        assert "--expected-commit-sha" in window
    # The evidence is generated exactly once before it is consumed.
    assert content.count('--output "$evidence"') == 1


def test_production_reverifies_main_and_workflow_before_mutation() -> None:
    """Environment delay or workflow-file drift invalidates authorization."""
    content = workflow()
    deploy = content[content.index("  deploy:") :]
    first = content.index("Reverify current protected main and workflow")
    final = content.index(
        "Reverify authorization immediately before foundation mutation"
    )
    create = content.index("Deploy the exactly authorized runtime foundation")

    assert first < content.index("actions/download-artifact@")
    assert final < create
    # F2: five distinct freshness checkpoints (three inline steps plus the two
    # reverify_protected_main function bodies used before the publication and
    # rollout mutations) each fetch current main and pin the workflow blob.
    assert deploy.count("refs/heads/main:refs/remotes/origin/main") == 5
    assert deploy.count('test "$GITHUB_SHA" = "$(git rev-parse origin/main)"') == 5
    assert deploy.count('git hash-object "$workflow"') == 5
    assert deploy.count('test "$CONFIRMED_SHA" = "$GITHUB_SHA"') == 5
    assert deploy.count("--phase production-foundation") == 2
    freshness = deploy.index("foundation-freshness-preflight.json")
    mutation = deploy.index("Deploy the exactly authorized runtime foundation")
    assert freshness < mutation


def test_post_approval_main_advancement_fails_before_mutation() -> None:
    """The final gate compares fetched current main with the authorized SHA."""
    content = workflow()
    final = content.index(
        "Reverify authorization immediately before foundation mutation"
    )
    create = content.index("Deploy the exactly authorized runtime foundation", final)
    commands = content[final:create]

    assert "+refs/heads/main:refs/remotes/origin/main" in commands
    assert 'test "$GITHUB_SHA" = "$(git rev-parse origin/main)"' in commands
    assert 'test "$CONFIRMED_SHA" = "$GITHUB_SHA"' in commands


def test_wrong_workflow_blob_fails_at_both_freshness_gates() -> None:
    """The checked-out workflow must equal the authorized commit's blob."""
    content = workflow()
    deploy = content[content.index("  deploy:") :]

    assert deploy.count('expected_workflow_blob="$(git rev-parse') == 1
    assert deploy.count('git hash-object "$workflow"') == 5
    assert deploy.count('git rev-parse "$GITHUB_SHA:$workflow"') == 5


def test_production_requires_existing_successful_foundation() -> None:
    """Subscription bootstrap belongs only to the dedicated foundation workflow."""
    content = workflow()

    assert "Production requires a successfully deployed foundation" in content
    assert "properties.provisioningState=='Succeeded'" in content
    assert "az deployment sub create" not in content
    assert "--template-file infra/main.bicep" not in content


def test_runbook_requires_foundation_and_complete_evidence_arguments() -> None:
    """Keep operator examples aligned with the production trust boundary."""
    runbook = (ROOT / "docs" / "PRODUCTION_DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "successful foundation apply and convergence is a production" in runbook
    assert "Production has no subscription deployment branch" in runbook
    assert "--phase production-session" in runbook
    assert "--phase production-foundation" in runbook
    for argument in (
        "--raw-whatif",
        "--raw-whatif-sha256",
        "--effective-parameters",
        "--effective-parameters-sha256",
        "--classified-evidence",
        "--classified-evidence-sha256",
        "--expected-commit-sha",
    ):
        assert runbook.count(argument) >= 1
    assert "does not cryptographically attest" in runbook
    assert "Windows lacks equivalent `O_NOFOLLOW`" in runbook


def test_cache_mode_is_configuration_controlled_and_propagated() -> None:
    """Read one protected canonical Boolean; add cache-only values only when enabled."""
    content = workflow()

    assert (
        'OPTIMA_SEMANTIC_CACHE_ENABLED: "${{ vars.OPTIMA_SEMANTIC_CACHE_ENABLED }}"'
        in content
    )
    assert 'OPTIMA_SEMANTIC_CACHE_ENABLED: "false"' not in content
    assert 'OPTIMA_SEMANTIC_CACHE_ENABLED: "true"' not in content
    parameter_builder = (ROOT / "scripts" / "production_parameters.py").read_text(
        encoding="utf-8"
    )
    # The canonical cache Boolean is resolved into every immutable parameter
    # artifact by the shared builder, never by a workflow-owned literal, so the
    # foundation and both rollout artifacts stay in exact agreement.
    assert "OPTIMA_SEMANTIC_CACHE_ENABLED" in parameter_builder
    assert '"semanticCacheEnabled=' not in content
    # The cache-only Bicep bindings live only in the builder's enabled branch;
    # the workflow never hardcodes a Redis or embedding parameter.
    for parameter in (
        "redisEmbeddingDeployment",
        "redisEmbeddingModel",
        "redisEmbeddingDimension",
        "pricingEmbeddingInputRatePerMillionTokens",
    ):
        assert f'"{parameter}=$OPTIMA_' not in content
        assert parameter in parameter_builder
    # The deployed API revision is inspected for the exact mode and the exact
    # cache-environment count before exposure.
    assert content.count('if test "$OPTIMA_SEMANTIC_CACHE_ENABLED" = "true"; then') == 1
    assert 'test "$cache_environment_count" -eq 9' in content
    assert 'test "$cache_environment_count" -eq 0' in content


def test_reviewed_model_versions_reach_both_bicep_deployment_phases() -> None:
    """Pass each protected role version through foundation and rollout unchanged."""
    content = workflow()
    parameter_builder = (ROOT / "scripts" / "production_parameters.py").read_text(
        encoding="utf-8"
    )
    expected = {
        "foundrySmallModelVersion": "OPTIMA_FOUNDRY_SMALL_MODEL_VERSION",
        "foundryStrongModelVersion": "OPTIMA_FOUNDRY_STRONG_MODEL_VERSION",
        "judgeModelVersion": "OPTIMA_JUDGE_MODEL_VERSION",
    }

    for parameter, variable in expected.items():
        assert f'{variable}: "${{{{ vars.{variable} }}}}"' in content
        assert f'"{parameter}=$' not in content
        assert parameter in parameter_builder
        assert variable in parameter_builder

    assert "EXPECTED_RESPONSE_MODEL" not in content


def test_cache_mode_is_not_hardcoded_to_a_literal_boolean() -> None:
    """Reject any workflow-owned cache mode that would need a code edit to change."""
    content = workflow()

    literal_mode = re.search(
        r'OPTIMA_SEMANTIC_CACHE_ENABLED:\s*"(?:true|false)"', content
    )
    assert literal_mode is None
    assert re.search(
        r"OPTIMA_SEMANTIC_CACHE_ENABLED:\s*"
        r'"\$\{\{\s*vars\.OPTIMA_SEMANTIC_CACHE_ENABLED\s*\}\}"',
        content,
    )
    # A bare `test ... = "true|false"` assertion would force one mode; the
    # conditional `if test ... = "true"; then` branch that adds cache-only
    # parameters is mode-generic and must remain allowed.
    forced_mode = re.search(
        r'^\s*test "\$OPTIMA_SEMANTIC_CACHE_ENABLED" = "(?:true|false)"',
        content,
        re.MULTILINE,
    )
    assert forced_mode is None


def test_cache_mode_is_strictly_canonically_validated_before_azure_login() -> None:
    """Fail closed on any non-canonical cache mode before Azure identity or mutation."""
    content = workflow()

    assert content.count('case "$OPTIMA_SEMANTIC_CACHE_ENABLED" in') == 1
    canonical_gate = content.index('case "$OPTIMA_SEMANTIC_CACHE_ENABLED" in')
    error_message = content.index(
        "OPTIMA_SEMANTIC_CACHE_ENABLED must be exactly true or false",
        canonical_gate,
    )
    assert canonical_gate < error_message
    assert canonical_gate < content.index("azure/login@")
    assert canonical_gate < content.index("--phase production-foundation")


def test_disabled_cache_is_verified_before_pre_exposure_smoke() -> None:
    """Inspect the deployed revision for exact mode and absent cache environment."""
    content = workflow()
    rollout = content.index(
        "Classify and deploy the digest-qualified Container Apps rollout"
    )
    cache_mode = content.index('configured_cache_mode="$(az containerapp', rollout)
    cache_environment = content.index(
        'cache_environment_count="$(az containerapp', cache_mode
    )
    absent_gate = content.index(
        'test "$cache_environment_count" -eq 0', cache_environment
    )
    smoke = content.index("az containerapp job start", absent_gate)
    public = content.index("--stage public-ui", smoke)

    assert cache_mode < cache_environment < absent_gate < smoke < public
    assert (
        "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS"
        in content[cache_environment:absent_gate]
    )
    assert (
        "starts_with(name, 'OPTIMA_REDIS_')" in content[cache_environment:absent_gate]
    )


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
    assert '--api-image-digest "$API_DIGEST"' in content
    assert '--ui-image-digest "$UI_DIGEST"' in content
    assert 'test "$api_image" = "$REGISTRY/optima-api@$API_DIGEST"' in content
    assert 'test "$ui_image" = "$REGISTRY/optima-ui@$UI_DIGEST"' in content


def test_rollout_records_source_and_verifies_runtime_contracts() -> None:
    """Trace revisions to source and verify readiness, routing, and telemetry."""
    content = workflow()
    smoke = (ROOT / "src" / "ui" / "deployment_smoke.py").read_text(encoding="utf-8")
    parameter_builder = (ROOT / "scripts" / "production_parameters.py").read_text(
        encoding="utf-8"
    )

    assert "deploymentCommitSha" in parameter_builder
    assert "deploymentWorkflowRunId" in parameter_builder
    assert '"smokeRunMarker=$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"' in content
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
    assert "SemanticCacheOutcome.DISABLED_BYPASSED" in smoke
    assert "ExecutionStepType.SEMANTIC_CACHE" in smoke
    assert "strong.total_calculated_cost != sum" in smoke
    assert 'response.headers.get(PERSISTENCE_HEADER) != "PERSISTED"' in smoke
    assert "result.contract_met is not True" in smoke
    assert "result.final_evaluation.passed is not True" in smoke
    assert "result.total_calculated_cost is None" in smoke
    assert "GITHUB_STEP_SUMMARY" in content


def test_ui_is_internal_until_easy_auth_is_verified() -> None:
    """Prevent a partial ARM failure from exposing unauthenticated Streamlit."""
    content = workflow()
    internal = content.index("--stage internal", content.index("--phase rollout"))
    auth = content.index("authConfigs/current", internal)
    live_execution = content.index("az containerapp job start", auth)
    telemetry = content.index("operation_Id == '$trace_id'", live_execution)
    public = content.index("--stage public-ui", auth)

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
    public = content.index("--stage public-ui", gate)
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
    assert "PREVIOUS_API_SEMANTIC_CACHE_ENABLED" in content
    assert "api_semantic_cache_enabled" in content
    assert "OPTIMA_SEMANTIC_CACHE_ENABLED'].value" in content
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


def test_smoke_job_receives_the_same_explicit_cache_mode_as_api() -> None:
    """Prevent the deployment verifier from assuming a cache mode default."""
    module = (ROOT / "infra" / "modules" / "container-apps.bicep").read_text(
        encoding="utf-8"
    )
    smoke_job = module[module.index("resource deploymentSmokeJob") :]

    assert module.count("name: 'OPTIMA_SEMANTIC_CACHE_ENABLED'") == 2
    assert "name: 'OPTIMA_SEMANTIC_CACHE_ENABLED'" in smoke_job
    assert "value: validatedSemanticCacheEnabled ? 'true' : 'false'" in smoke_job


IMAGE_LOAD_STEP_NAME = "Load the exact verified image objects"


def _image_load_run_body() -> str:
    """Return the exact indented shell body of the image-load step."""
    content = workflow()
    start = content.index(f"- name: {IMAGE_LOAD_STEP_NAME}")
    after_run = content[start:].split("run: |\n", 1)[1]
    body: list[str] = []
    for line in after_run.splitlines():
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line)
    return "\n".join(body)


def test_image_load_step_passes_valid_go_template_without_backslash() -> None:
    """The docker --format operands must reach the Go-template parser unescaped.

    The single-quoted format string is passed to docker verbatim, so a literal
    backslash inside it reaches Go's text/template and fails with
    ``unexpected "\\" in operand`` (production run 35208289537). This asserts the
    corrected, backslash-free operands and that they round-trip through a POSIX
    shell tokenizer exactly as GitHub Actions executes them.
    """
    body = _image_load_run_body()

    # bash single-quoted content is literal and terminated by the next quote
    formats = re.findall(r"docker image inspect --format '([^']*)'", body)
    assert len(formats) == 3, f"expected three --format operands, got {formats!r}"

    for tmpl in formats:
        assert "\\" not in tmpl, f"backslash reaches Go-template operand: {tmpl!r}"
        # a real POSIX tokenizer must yield the identical literal operand
        argv = shlex.split(f"docker image inspect --format '{tmpl}' img")
        assert argv[argv.index("--format") + 1] == tmpl

    assert '{{index .Config.Labels "org.opencontainers.image.revision"}}' in formats
    assert "{{.Id}}" in formats
    assert "{{.Os}}/{{.Architecture}}" in formats

    # API and UI are verified by the same shared loop
    assert "for component in api ui; do" in body

    # integrity, identity, platform, revision, and cardinality checks remain
    assert "set -euo pipefail" in body
    assert "sha256sum --check --strict exact-images.sha256" in body
    assert "docker image load --input exact-images.tar" in body
    assert 'expected_id="$(tr -d ' in body
    assert 'test "$actual_id" = "$expected_id"' in body
    assert '= "linux/amd64"' in body
    assert '= "$GITHUB_SHA"' in body

    # no mutable tag is accepted in place of the immutable checked identity
    assert body.count("optima-$component:production-check") == 3


def _deploy_job() -> str:
    """Return the deploy-job region of the production workflow."""
    content = workflow()
    return content[content.index("  deploy:") :]


def test_internal_rollout_classifies_its_whatif_before_the_create() -> None:
    """F1: the internal what-if is captured and classified before its create."""
    deploy = _deploy_job()
    whatif = deploy.index("optima-internal-rollout-what-if")
    capture = deploy.index('> "$internal_whatif"', whatif)
    classify = deploy.index("classify-rollout", capture)
    stage = deploy.index("--stage internal", classify)
    create = deploy.index('deployment_name="optima-internal-', stage)
    azure_create = deploy.index("az deployment group create", create)

    assert whatif < capture < classify < stage < create < azure_create
    # The classifier consumes the exact captured what-if and its digest.
    window = deploy[classify:create]
    assert '--whatif "$internal_whatif"' in window
    assert '--whatif-sha256 "$internal_whatif_sha256"' in window


def test_public_rollout_classifies_its_whatif_before_the_create() -> None:
    """F2: the public what-if is captured and classified before its create."""
    deploy = _deploy_job()
    whatif = deploy.index("optima-public-ui-what-if")
    capture = deploy.index('> "$public_whatif"', whatif)
    classify = deploy.index("classify-rollout", capture)
    stage = deploy.index("--stage public-ui", classify)
    create = deploy.index('deployment_name="optima-rollout-', stage)
    azure_create = deploy.index("az deployment group create", create)

    assert whatif < capture < classify < stage < create < azure_create
    window = deploy[classify:create]
    assert '--whatif "$public_whatif"' in window
    assert '--whatif-sha256 "$public_whatif_sha256"' in window


def test_every_irreversible_mutation_reverifies_protected_main() -> None:
    """F3: image push and both rollout creates reverify current origin/main."""
    deploy = _deploy_job()
    fresh = 'test "$GITHUB_SHA" = "$(git rev-parse origin/main)"'

    # The image push step reverifies protected main immediately before pushing.
    push_step = deploy.index("Push the exact verified images")
    push = deploy.index('docker push "$api_image"', push_step)
    assert fresh in deploy[push_step:push]

    # Both rollout creates are guarded by a reverify_protected_main call.
    internal_create = deploy.index('deployment_name="optima-internal-')
    public_create = deploy.index('deployment_name="optima-rollout-')
    assert "reverify_protected_main()" in deploy
    assert deploy[:internal_create].rstrip().endswith("reverify_protected_main") or (
        "reverify_protected_main"
        in deploy[deploy.index("internal_whatif") - 400 : internal_create]
    )
    assert (
        "reverify_protected_main"
        in deploy[deploy.index("public_whatif") - 400 : public_create]
    )
    # The reverify body still fetches current main and pins the workflow blob.
    fn = deploy[
        deploy.index("reverify_protected_main() {") : deploy.index(
            "# --- Internal rollout"
        )
    ]
    assert "+refs/heads/main:refs/remotes/origin/main" in fn
    assert 'git hash-object "$workflow"' in fn


def test_bootstrap_and_runtime_access_precede_the_first_mutation() -> None:
    """F8: the bootstrap flag and runtime-access gate precede any mutation."""
    deploy = _deploy_job()
    bootstrap = deploy.index('test "$OPTIMA_RUNTIME_ACCESS_BOOTSTRAPPED" = "true"')
    runtime_phase = deploy.index("--phase production-foundation", bootstrap - 2000)
    foundation_create = deploy.index("Deploy the exactly authorized runtime foundation")
    image_push = deploy.index('docker push "$api_image"')

    assert bootstrap < foundation_create
    assert runtime_phase < foundation_create
    # No Azure mutation may precede the first bootstrap+runtime-access gate.
    before_gate = deploy[:bootstrap]
    assert "az deployment group create" not in before_gate
    assert 'docker push "$api_image"' not in before_gate
    assert bootstrap < image_push


def test_rollout_creates_consume_the_classified_parameter_artifact() -> None:
    """Point 5: the create uses the exact artifact digest the what-if classified."""
    deploy = _deploy_job()
    for parameters, sha in (
        ("internal_parameters", "internal_sha256"),
        ("public_parameters", "public_sha256"),
    ):
        classify = deploy.index(f'--parameters-file "${parameters}"')
        # The classifier binds the exact artifact digest the create will consume.
        assert f'--parameters-sha256 "${sha}"' in deploy[classify : classify + 400]
        # The create rechecks that digest and then deploys the same file through
        # the descriptor-pinning helper (F6), never a re-openable pathname.
        strict = deploy.index("sha256sum --check --strict", classify)
        pinned = deploy.index(f'--parameter-file "${parameters}"', strict)
        create = deploy.index("az deployment group create", pinned)
        assert classify < strict < pinned < create


def test_rollout_whatif_is_incremental_full_resource_payload_json() -> None:
    """The rollout what-ifs feed the classifier the exact FullResourcePayloads JSON."""
    deploy = _deploy_job()
    for name in ("optima-internal-rollout-what-if", "optima-public-ui-what-if"):
        whatif = deploy.index(name)
        window = deploy[whatif : whatif + 600]
        assert "--mode Incremental" in window
        assert "--validation-level ProviderNoRbac" in window
        assert "--result-format FullResourcePayloads" in window
        assert "--no-pretty-print" in window
    # The unclassified single what-if of the pre-classification head must not return.
    assert "optima-rollout-what-if " not in workflow()


# --- F2: a final freshness check immediately precedes every mutation ----------


def _final_check_precedes(
    deploy: str, mutation_marker: str, *, search_from: int
) -> bool:
    """Return whether a freshness check immediately precedes a mutation marker.

    Only local assignments (no networked command) may appear between the final
    origin/main equality check or reverify call and the mutation.
    """
    mutation = deploy.index(mutation_marker, search_from)
    window = deploy[:mutation]
    inline = window.rfind('test "$GITHUB_SHA" = "$(git rev-parse origin/main)"')
    call = window.rfind("\n          reverify_protected_main\n")
    anchor = max(inline, call)
    assert anchor != -1, f"no freshness check precedes {mutation_marker!r}"
    between = deploy[anchor:mutation]
    # No networked az/docker/git-fetch command may sit between the check and the
    # mutation; only local variable assignments and the pinned-exec invocation.
    for forbidden in (
        "az deployment group what-if",
        "az acr ",
        "az containerapp show",
        "git fetch",
    ):
        assert forbidden not in between, (
            f"{forbidden!r} runs between the final check and {mutation_marker!r}"
        )
    return True


def test_final_freshness_check_precedes_every_mutation() -> None:
    """F2: foundation create, both pushes, both rollout creates, and the smoke
    job start are each immediately preceded by a final freshness check."""
    deploy = _deploy_job()
    # Foundation create (inline freshness check in the same step).
    assert _final_check_precedes(
        deploy,
        "python scripts/pinned_parameter_exec.py",
        search_from=deploy.index("Deploy the exactly authorized runtime foundation"),
    )
    # API and UI pushes.
    assert _final_check_precedes(
        deploy, 'docker push "$api_image"', search_from=deploy.index("Push the exact")
    )
    assert _final_check_precedes(
        deploy, 'docker push "$ui_image"', search_from=deploy.index("Push the exact")
    )
    # Internal rollout create.
    assert _final_check_precedes(
        deploy,
        "az deployment group create --name",
        search_from=deploy.index('deployment_name="optima-internal-'),
    )
    # Smoke job start.
    assert _final_check_precedes(
        deploy,
        "az containerapp job start",
        search_from=deploy.index("smoke_job="),
    )
    # Public rollout create.
    assert _final_check_precedes(
        deploy,
        "az deployment group create --name",
        search_from=deploy.index('deployment_name="optima-rollout-'),
    )


def _reverify_function_body() -> str:
    """Extract the reverify_protected_main function body from the deploy job."""
    deploy = _deploy_job()
    start = deploy.index("reverify_protected_main() {")
    end = deploy.index("\n          }\n", start) + len("\n          }\n")
    return deploy[start:end]


@pytest.mark.skipif(BASH is None, reason="bash is required for the command-double test")
def test_advancing_main_refuses_mutation(tmp_path: Path) -> None:
    """F2 command double: when origin/main advances the mutation is refused.

    The exact reverify_protected_main body from the workflow runs under bash with
    a fake git whose ``rev-parse origin/main`` reports an advanced commit; the
    following mutation marker is never reached. The fake is a bash function so it
    reliably shadows the real git on every platform.
    """
    body = _reverify_function_body()
    sha = "a" * 40
    marker = tmp_path / "mutated"
    fake_git = (
        "git() {\n"
        '  case "$*" in\n'
        '    "rev-parse HEAD") echo "$GITHUB_SHA" ;;\n'
        '    "rev-parse origin/main") echo "$ORIGIN_MAIN" ;;\n'
        "    fetch*) return 0 ;;\n"
        "    hash-object*) echo BLOB ;;\n"
        "    rev-parse*) echo BLOB ;;\n"
        "    *) echo UNEXPECTED >&2; return 3 ;;\n"
        "  esac\n"
        "}\n"
    )
    script = (
        f"set -euo pipefail\n{fake_git}{body}\nreverify_protected_main\n"
        f"touch '{marker}'\n"
    )
    base_env = {
        **os.environ,
        "GITHUB_REF": "refs/heads/main",
        "CONFIRMED_SHA": sha,
        "GITHUB_SHA": sha,
    }
    # Advanced main: the mutation is refused and the marker is never created.
    advanced = subprocess.run(
        [str(BASH), "-c", script],
        env={**base_env, "ORIGIN_MAIN": "b" * 40},
        capture_output=True,
        cwd=str(ROOT),
    )
    assert advanced.returncode != 0
    assert not marker.exists()
    # Control: a matching main permits the mutation exactly once.
    ok = subprocess.run(
        [str(BASH), "-c", script],
        env={**base_env, "ORIGIN_MAIN": sha},
        capture_output=True,
        cwd=str(ROOT),
    )
    assert ok.returncode == 0, ok.stderr.decode()
    assert marker.exists()


# --- F5: containment authoritative absence and deployment reconciliation ------


def _containment_body() -> str:
    """Return the shell body of the containment step."""
    content = workflow()
    start = content.index("- name: Contain a failed rollout")
    after_run = content[start:].split("run: |\n", 1)[1]
    body: list[str] = []
    for line in after_run.splitlines():
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line)
    return "\n".join(body)


def test_containment_only_treats_resource_not_found_as_absence() -> None:
    """F5: only an authoritative ResourceNotFound is treated as UI absence."""
    body = _containment_body()
    assert "is_resource_not_found" in body
    assert "ResourceNotFound" in body
    assert "existence is indeterminate; containment fails closed" in body
    # The step reconciles outstanding rollout deployments (cancel + wait) first.
    assert "az deployment group cancel" in body
    assert "optima-internal-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT" in body
    assert "optima-rollout-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT" in body
    # Truthful recovery reporting distinguishes absent from contained.
    assert "recovery=absent" in body
    assert "recovery=contained" in body


@pytest.mark.skipif(BASH is None, reason="bash is required for the containment test")
def test_containment_fails_closed_on_access_denied(tmp_path: Path) -> None:
    """F5 executable: an access-denied UI lookup fails containment, never absent."""
    body = _containment_body()
    fake = (
        "sleep() { return 0; }\n"
        "az() {\n"
        '  case "$*" in\n'
        # Outstanding rollout deployments are absent (authoritative not found).
        '    "deployment group show"*)\n'
        '      echo "ERROR: (ResourceNotFound) was not found" >&2; return 3 ;;\n'
        # The UI lookup is denied, not a not-found result.
        '    "containerapp show"*)\n'
        '      echo "ERROR: (AuthorizationFailed) access denied" >&2; return 1 ;;\n'
        "    *) return 0 ;;\n"
        "  esac\n"
        "}\n"
    )
    script = (
        f"export AZURE_RESOURCE_GROUP=rg GITHUB_RUN_ID=1 GITHUB_RUN_ATTEMPT=1 "
        f"RUNNER_TEMP='{tmp_path}' GITHUB_STEP_SUMMARY='{tmp_path / 'summary'}'\n"
        f"{fake}{body}\n"
    )
    result = subprocess.run(
        [str(BASH), "-c", script],
        env={**os.environ},
        capture_output=True,
        cwd=str(ROOT),
    )
    assert result.returncode != 0
    assert "indeterminate" in result.stderr.decode()


@pytest.mark.skipif(BASH is None, reason="bash is required for the containment test")
def test_containment_cancels_outstanding_public_deployment(tmp_path: Path) -> None:
    """F5 executable: an in-flight rollout deployment is cancelled before contain."""
    body = _containment_body()
    cancel_log = tmp_path / "cancelled"
    state_file = tmp_path / "state"
    fake = (
        "sleep() { return 0; }\n"
        "az() {\n"
        f'  local state_file="{state_file}"\n'
        '  case "$*" in\n'
        '    "deployment group show"*)\n'
        '      if [ -f "$state_file" ]; then echo Canceled; else echo Running; fi ;;\n'
        '    "deployment group cancel"*)\n'
        f'      touch "{cancel_log}"; touch "$state_file"; return 0 ;;\n'
        '    "containerapp show"*ingress.external*) echo false ;;\n'
        '    "containerapp show"*) return 0 ;;\n'
        '    "containerapp ingress update"*) return 0 ;;\n'
        "    *) return 0 ;;\n"
        "  esac\n"
        "}\n"
    )
    script = (
        f"export AZURE_RESOURCE_GROUP=rg GITHUB_RUN_ID=1 GITHUB_RUN_ATTEMPT=1 "
        f"RUNNER_TEMP='{tmp_path}' GITHUB_STEP_SUMMARY='{tmp_path / 'summary'}'\n"
        f"{fake}{body}\n"
    )
    result = subprocess.run(
        [str(BASH), "-c", script],
        env={**os.environ},
        capture_output=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr.decode()
    assert cancel_log.exists()
