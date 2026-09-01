"""Static regression contracts for Slice 11B Azure deployment readiness."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    """Read one repository file as normalized UTF-8 text."""
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_application_parameter_files_target_eastus2_with_deployment_disabled() -> None:
    """Keep application resources in East US 2 without enabling deployment."""
    for relative_path in (
        "infra/environments/hackathon.bicepparam",
        "infra/environments/hackathon.runtime.bicepparam",
    ):
        content = read(relative_path)
        assert "param location = 'eastus2'" in content
        assert "param location = 'eastus'" not in content
        assert "param deployContainerApps = false" in content
        assert "param exposePublicUi = false" in content
        assert "param deployRuntimeAccess = false" in content


def test_container_apps_require_separate_immutable_image_digests() -> None:
    """Reference each runtime image by manifest digest rather than a mutable tag."""
    main = read("infra/main.bicep")
    resources = read("infra/resource-group.bicep")

    assert "param apiImageDigest string" in main
    assert "param uiImageDigest string" in main
    assert "imageTag" not in main
    assert "optima-api@${validatedApiImageDigest}" in resources
    assert "optima-ui@${validatedUiImageDigest}" in resources
    assert "optima-api:${" not in resources
    assert "optima-ui:${" not in resources
    assert "placeholderImageDigest" in main
    assert "validatedApiImageDigest" in main
    assert "validatedUiImageDigest" in main
    assert "fail('Container Apps deployment requires" in main
    assert "apiImageDigest == toLower(apiImageDigest)" in main
    assert "uiImageDigest == toLower(uiImageDigest)" in main
    compact_main = re.sub(r"\s+", "", main)
    for character in "0123456789abcdef":
        assert f"'{character}','')" in compact_main
    assert "placeholderImageDigest" in resources
    assert "fail('Container Apps deployment requires" in resources
    assert "apiImageDigest == toLower(apiImageDigest)" in resources
    assert "uiImageDigest == toLower(uiImageDigest)" in resources


def test_pre_exposure_smoke_runs_as_a_distroless_safe_job() -> None:
    """Run the pre-exposure smoke as a job because the UI image has no shell.

    The UI runtime image is distroless, so ``az containerapp exec`` cannot open a
    shell in it. The smoke must run the image's own Python entrypoint as a
    one-shot manual Container Apps job that is gated on its execution status.
    """
    module = read("infra/modules/container-apps.bicep")
    resources = read("infra/resource-group.bicep")

    assert "resource deploymentSmokeJob 'Microsoft.App/jobs@" in module
    assert "= if (deployApplications) {" in module
    assert "triggerType: 'Manual'" in module
    assert "replicaRetryLimit: 0" in module
    assert "image: uiImage" in module
    assert "'ui.deployment_smoke'" in module
    assert "'--traceparent'" in module
    assert "'--run-marker'" in module
    assert "value: 'https://${api!.properties.configuration.ingress.fqdn}'" in module
    assert "param smokeTraceparent string" in module
    assert "param smokeRunMarker string" in module
    assert "param smokeJobName string" in module

    assert "smokeJob: 'caj-optima-smoke-${environmentName}'" in resources
    assert "smokeJobName: resourceNames.smokeJob" in resources
    assert "smokeTraceparent: validatedSmokeTraceparent" in resources
    assert "fail('Container Apps deployment requires a pre-exposure smoke" in resources
    assert "output smokeJobName string = resourceNames.smokeJob" in resources


def test_bicep_entry_points_restrict_application_location_to_eastus2() -> None:
    """Reject an unreviewed application region instead of falling back silently."""
    for relative_path in ("infra/main.bicep", "infra/resource-group.bicep"):
        content = read(relative_path)
        assert "@allowed([\n  'eastus2'\n])" in content
        assert "param location string = 'eastus2'" in content
        assert "resourceGroup().location" not in content


def test_container_apps_map_production_runtime_environment_contract() -> None:
    """Supply the API and UI deployment environment with existing Azure settings."""
    module = read("infra/modules/container-apps.bicep")

    assert module.count("name: 'OPTIMA_DEPLOYMENT_ENVIRONMENT'") == 2
    assert "name: 'OPTIMA_PRODUCTION_EVALUATOR_MODE'" in module
    assert "name: 'OPTIMA_PRODUCTION_REQUIRE_REFERENCE_OUTPUT'" in module
    assert "name: 'OPTIMA_EXECUTION_CONCURRENCY_LIMIT'" in module
    assert "name: 'OPTIMA_EXECUTION_TIMEOUT_SECONDS'" in module
    assert "name: 'OPTIMA_REQUIRE_REFERENCE_OUTPUT'" in module
    assert "name: 'OPTIMA_JUDGE_DEPLOYMENT'" in module
    assert "name: 'OPTIMA_JUDGE_MODEL'" in module
    assert "name: 'OPTIMA_JUDGE_TIMEOUT_SECONDS'" in module
    assert "validatedEvaluatorMode == 'EXACT_REFERENCE' ? 'true' : 'false'" in module
    assert "name: 'OPTIMA_FOUNDRY_MANAGED_IDENTITY_CLIENT_ID'" in module
    assert "name: 'OPTIMA_FOUNDRY_SMALL_MODEL'" in module
    assert "name: 'OPTIMA_FOUNDRY_STRONG_MODEL'" in module
    assert "name: 'OPTIMA_COSMOS_MANAGED_IDENTITY_CLIENT_ID'" in module
    assert "name: 'OPTIMA_REDIS_MANAGED_IDENTITY_CLIENT_ID'" in module
    assert "name: 'OPTIMA_API_BASE_URL'" in module
    assert "name: 'OPTIMA_API_TIMEOUT_SECONDS'" in module
    assert "name: 'OPTIMA_UI_PRODUCTION_MODE'" in module


def test_ui_transport_timeout_exceeds_server_execution_and_persistence_budgets() -> (
    None
):
    """Prevent the UI from abandoning paid work that is still within server bounds."""
    module = read("infra/modules/container-apps.bicep")

    def configured_value(name: str) -> int:
        match = re.search(
            rf"name: '{name}'\s+value: '(\d+)'",
            module,
        )
        assert match is not None
        return int(match.group(1))

    assert configured_value("OPTIMA_API_TIMEOUT_SECONDS") > (
        configured_value("OPTIMA_EXECUTION_TIMEOUT_SECONDS")
        + configured_value("OPTIMA_COSMOS_TIMEOUT_SECONDS")
    )


def test_public_ui_requires_tenant_restricted_entra_authentication() -> None:
    """Reject anonymous public UI access while preserving an internal API."""
    module = read("infra/modules/container-apps.bicep")

    assert (
        "resource uiAuthentication 'Microsoft.App/containerApps/authConfigs@2025-07-01'"
        in module
    )
    assert "parent: ui" in module
    assert "platform: {\n      enabled: true" in module
    assert "azureActiveDirectory: {\n        enabled: true" in module
    assert (
        "openIdIssuer: '${environment().authentication.loginEndpoint}"
        "${uiAuthTenantId}/v2.0'" in module
    )
    assert "unauthenticatedClientAction: 'RedirectToLoginPage'" in module
    assert "requireHttps: true" in module
    assert "allowInsecure: false\n        external: exposePublicUi" in module
    assert "allowInsecure: false\n        external: false" in module
    # A confidential-client secret is required: without it Container Apps falls back
    # to the weaker implicit flow, so the secret reference is a security control.
    assert "clientSecretSettingName: 'ui-auth-client-secret'" in module
    assert "tokenStore: {\n        enabled: false" in module


def test_public_ui_entra_uses_secret_referenced_confidential_client() -> None:
    """Require a secret-referenced confidential client, never a plain env value."""
    module = read("infra/modules/container-apps.bicep")
    resources = read("infra/resource-group.bicep")
    main = read("infra/main.bicep")

    # The UI Entra registration references a Container Apps secret by name.
    assert "clientSecretSettingName: 'ui-auth-client-secret'" in module
    assert "name: 'ui-auth-client-secret'" in module
    assert "value: uiAuthClientSecret" in module

    # The credential is a secure parameter used only as the secret value, never as a
    # plain container environment value.
    assert "@secure()" in module
    assert "param uiAuthClientSecret string" in module
    assert module.count("uiAuthClientSecret") == 2

    # Both entry points thread the secure parameter with an empty default so committed
    # parameter files never carry the credential.
    for entry in (resources, main):
        assert "param uiAuthClientSecret string = ''" in entry
        assert "uiAuthClientSecret: uiAuthClientSecret" in entry
        output_lines = [
            line for line in entry.splitlines() if line.startswith("output ")
        ]
        assert all("uiAuthClientSecret" not in line for line in output_lines)


def test_ui_auth_client_secret_absent_from_committed_parameter_files() -> None:
    """Keep the confidential-client secret out of committed parameter files."""
    for relative_path in (
        "infra/environments/hackathon.bicepparam",
        "infra/environments/hackathon.runtime.bicepparam",
    ):
        assert "uiAuthClientSecret" not in read(relative_path)


def test_container_app_deployment_rejects_placeholder_entra_identity() -> None:
    """Reject deployment with placeholder identities or a missing client secret."""
    resources = read("infra/resource-group.bicep")

    assert "uiAuthClientId != placeholderIdentity" in resources
    assert "uiAuthTenantId != placeholderIdentity" in resources
    assert "!empty(uiAuthClientSecret)" in resources
    assert "!deployContainerApps || uiAuthConfigurationIsDeployable" in resources
    assert (
        "requires a non-placeholder UI Entra client ID, tenant ID, and "
        "confidential-client secret" in resources
    )


def test_application_insights_connection_uses_container_app_secret_reference() -> None:
    """Keep the telemetry destination out of plain container environment values."""
    module = read("infra/modules/container-apps.bicep")
    monitoring = read("infra/modules/monitoring.bicep")

    assert "@secure()\n@description('Application Insights connection string" in module
    assert "name: 'application-insights-connection-string'" in module
    assert "secretRef: 'application-insights-connection-string'" in module
    assert (
        "name: 'OPTIMA_APPLICATION_INSIGHTS_CONNECTION_STRING'\n              value:"
        not in module
    )
    assert (
        "@secure()\noutput connectionString string = "
        "applicationInsights.properties.ConnectionString" in monitoring
    )


def test_hackathon_parameters_select_reference_free_judge_without_deployment() -> None:
    """Require explicit Slice 11C judge identities while keeping Azure mutation off."""
    for relative_path in (
        "infra/environments/hackathon.bicepparam",
        "infra/environments/hackathon.runtime.bicepparam",
    ):
        content = read(relative_path)
        assert "param productionEvaluatorMode = 'LLM_JUDGE'" in content
        assert "param judgeDeployment = 'replace-judge-deployment'" in content
        assert "param judgeModel = 'replace-judge-model'" in content
        assert "param judgeTimeoutSeconds = 30" in content
        assert "param deployContainerApps = false" in content


def test_container_apps_reject_checked_in_judge_placeholders() -> None:
    """Block LLM_JUDGE deployment until Slice 11C supplies real identities."""
    module = read("infra/modules/container-apps.bicep")

    assert "judgeConfigurationIsDeployable" in module
    assert "judgeDeployment != 'replace-judge-deployment'" in module
    assert "judgeModel != 'replace-judge-model'" in module
    assert "requires deployable judge identities" in module


def test_container_apps_environment_precedes_gated_applications() -> None:
    """Create the environment before callback-gated API and UI resources."""
    resources = read("infra/resource-group.bicep")
    module = read("infra/modules/container-apps.bicep")

    assert "module containerApps 'modules/container-apps.bicep' = {" in resources
    assert "deployApplications: deployContainerApps" in resources
    assert "exposePublicUi: validatedExposePublicUi" in resources
    assert "resource managedEnvironment 'Microsoft.App/managedEnvironments@" in module
    assert (
        "resource api 'Microsoft.App/containerApps@2025-07-01' = if "
        "(deployApplications)" in module
    )
    assert (
        "resource ui 'Microsoft.App/containerApps@2025-07-01' = if "
        "(deployApplications)" in module
    )
    assert "output environmentDefaultDomain string" in module
    assert "Public UI exposure requires Container Apps deployment" in resources


def test_container_apps_require_complete_reviewed_production_pricing() -> None:
    """Supply every required role rate and reject deployment placeholders."""
    module = read("infra/modules/container-apps.bicep")
    resources = read("infra/resource-group.bicep")
    main = read("infra/main.bicep")

    required_environment_names = (
        "OPTIMA_PRODUCTION_COST_MEASUREMENT_REQUIRED",
        "OPTIMA_PRICING_CATALOG_VERSION",
        "OPTIMA_PRICING_CURRENCY",
        "OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS",
        "OPTIMA_PRICING_SMALL_OUTPUT_RATE_PER_MILLION_TOKENS",
        "OPTIMA_PRICING_STRONG_INPUT_RATE_PER_MILLION_TOKENS",
        "OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS",
        "OPTIMA_PRICING_JUDGE_INPUT_RATE_PER_MILLION_TOKENS",
        "OPTIMA_PRICING_JUDGE_OUTPUT_RATE_PER_MILLION_TOKENS",
        "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS",
    )
    for name in required_environment_names:
        assert f"name: '{name}'" in module

    assert "value: 'true'" in module
    assert "pricingConfigurationIsDeployable" in resources
    assert "!deployContainerApps || pricingConfigurationIsDeployable" in resources
    assert "requires complete reviewed pricing" in resources
    assert "!empty(pricingCatalogVersion)" in resources
    assert "!empty(pricingEmbeddingInputRatePerMillionTokens)" in resources
    assert "!empty(deploymentWorkflowRunId)" in resources
    assert "deploymentCommitInvalidCharacters" in resources
    assert "deploymentWorkflowRunInvalidCharacters" in resources
    assert "contains(deploymentWorkflowRunId,'-')" in re.sub(r"\s+", "", resources)
    for parameter in (
        "pricingCatalogVersion",
        "pricingCurrency",
        "pricingSmallInputRatePerMillionTokens",
        "pricingSmallOutputRatePerMillionTokens",
        "pricingStrongInputRatePerMillionTokens",
        "pricingStrongOutputRatePerMillionTokens",
        "pricingJudgeInputRatePerMillionTokens",
        "pricingJudgeOutputRatePerMillionTokens",
        "pricingEmbeddingInputRatePerMillionTokens",
    ):
        assert f"param {parameter} string" in main
        assert f"{parameter}: {parameter}" in main
        assert f"param {parameter} string" in resources
