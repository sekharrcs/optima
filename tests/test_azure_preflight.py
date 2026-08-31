"""Behavioral tests for the read-only Azure deployment preflight."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.azure_preflight import (
    ACR_PUSH_ROLE_ID,
    CONTRIBUTOR_ROLE_ID,
    OPENAI_USER_ROLE_ID,
    READER_ROLE_ID,
    REQUIRED_PROVIDERS,
    DeploymentConfiguration,
    PreflightError,
    load_configuration,
    run_preflight,
    validate_configuration,
)

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
TENANT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_ID = "12345678-abcd-4321-abcd-123456789abc"
UI_AUTH_CLIENT_ID = "ui-auth-client-id"
UI_AUTH_CLIENT_SECRET_ENV = "".join(("OPTIMA_UI_AUTH_", "CLIENT_SECRET"))
OPENAI_RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-foundry/providers/"
    "Microsoft.CognitiveServices/accounts/aoai-optima"
)


def valid_environment() -> dict[str, str]:
    """Return a complete synthetic deployment environment."""
    return {
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "present",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.invalid/oidc",
        "AZURE_CLIENT_ID": CLIENT_ID,
        "AZURE_CONTAINER_REGISTRY_NAME": "acroptima123456789",
        "AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID": (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-identity/providers/"
            "Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-deploy"
        ),
        "AZURE_LOCATION": "eastus2",
        "AZURE_OPENAI_RESOURCE_ID": OPENAI_RESOURCE_ID,
        "AZURE_RESOURCE_GROUP": "rg-optima-hackathon",
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
        "AZURE_TENANT_ID": TENANT_ID,
        "GITHUB_REPOSITORY": "sekharrcs/optima",
        "OPTIMA_COST_REVIEWED_ON": date.today().isoformat(),
        "OPTIMA_EXPECTED_FIXED_MONTHLY_COST_INR": "1600",
        "OPTIMA_FOUNDRY_BASE_URL": ("https://aoai-optima.openai.azure.com/openai/v1"),
        "OPTIMA_FOUNDRY_SMALL_DEPLOYMENT": "optima-small",
        "OPTIMA_FOUNDRY_SMALL_MODEL": "gpt-4.1-mini",
        "OPTIMA_FOUNDRY_SMALL_MODEL_VERSION": "2025-04-14",
        "OPTIMA_FOUNDRY_STRONG_DEPLOYMENT": "optima-strong",
        "OPTIMA_FOUNDRY_STRONG_MODEL": "gpt-4.1",
        "OPTIMA_FOUNDRY_STRONG_MODEL_VERSION": "2025-04-14",
        "OPTIMA_GITHUB_ENVIRONMENT": "hackathon",
        "OPTIMA_JUDGE_DEPLOYMENT": "optima-judge",
        "OPTIMA_JUDGE_MODEL": "gpt-4.1-nano",
        "OPTIMA_JUDGE_MODEL_VERSION": "2025-04-14",
        "OPTIMA_PRICING_BINDING_SHA256": (
            "904c36f6deaf8ea867be97dbdd9fa57f54135d868c8bb8291586fc7c767284cd"
        ),
        "OPTIMA_PRICING_CATALOG_VERSION": "azure-global-2026-08-31",
        "OPTIMA_PRICING_CURRENCY": "USD",
        "OPTIMA_PRICING_SOURCE_URL": "https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/",
        "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS": "0.02",
        "OPTIMA_PRICING_EMBEDDING_MODEL": "text-embedding-3-small",
        "OPTIMA_PRICING_EMBEDDING_MODEL_VERSION": "1",
        "OPTIMA_PRICING_JUDGE_INPUT_RATE_PER_MILLION_TOKENS": "0.10",
        "OPTIMA_PRICING_JUDGE_MODEL": "gpt-4.1-nano",
        "OPTIMA_PRICING_JUDGE_MODEL_VERSION": "2025-04-14",
        "OPTIMA_PRICING_JUDGE_OUTPUT_RATE_PER_MILLION_TOKENS": "0.40",
        "OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS": "0.40",
        "OPTIMA_PRICING_SMALL_MODEL": "gpt-4.1-mini",
        "OPTIMA_PRICING_SMALL_MODEL_VERSION": "2025-04-14",
        "OPTIMA_PRICING_SMALL_OUTPUT_RATE_PER_MILLION_TOKENS": "1.60",
        "OPTIMA_PRICING_STRONG_INPUT_RATE_PER_MILLION_TOKENS": "2.00",
        "OPTIMA_PRICING_STRONG_MODEL": "gpt-4.1",
        "OPTIMA_PRICING_STRONG_MODEL_VERSION": "2025-04-14",
        "OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS": "8.00",
        "OPTIMA_REDIS_EMBEDDING_DEPLOYMENT": "optima-embedding",
        "OPTIMA_REDIS_EMBEDDING_DIMENSION": "1536",
        "OPTIMA_REDIS_EMBEDDING_MODEL": "text-embedding-3-small",
        "OPTIMA_REDIS_EMBEDDING_MODEL_VERSION": "1",
        "OPTIMA_UI_AUTH_CLIENT_ID": UI_AUTH_CLIENT_ID,
        "OPTIMA_UI_AUTH_REDIRECT_URI": (
            "https://ca-optima-ui-hackathon.synthetic.eastus2.azurecontainerapps.io/"
            ".auth/login/aad/callback"
        ),
        UI_AUTH_CLIENT_SECRET_ENV: "present",
        "OPTIMA_UI_AUTH_TENANT_ID": TENANT_ID,
    }


class FakeAzure:
    """Return deterministic Azure resource evidence for preflight tests."""

    def __init__(
        self,
        configuration: DeploymentConfiguration,
        *,
        foundation_exists: bool = False,
        acr_push: bool = True,
        forbidden_deployment_role: bool = False,
        lingering_subscription_contributor: bool = False,
    ) -> None:
        self.configuration = configuration
        self.foundation_exists = foundation_exists
        self.acr_push = acr_push
        self.forbidden_deployment_role = forbidden_deployment_role
        self.lingering_subscription_contributor = lingering_subscription_contributor
        self.calls: list[tuple[str, ...]] = []

    def json(self, *arguments: str, allow_missing: bool = False) -> Any:
        """Return one synthetic response selected by Azure CLI arguments."""
        del allow_missing
        self.calls.append(arguments)
        if arguments == ("account", "show"):
            return {
                "id": self.configuration.subscription_id,
                "state": "Enabled",
                "tenantId": self.configuration.tenant_id,
            }
        if arguments[:2] == ("provider", "show"):
            assert arguments[-1] in REQUIRED_PROVIDERS
            return {"registrationState": "Registered"}
        if arguments[:2] == ("identity", "show"):
            identity_name = arguments[arguments.index("--name") + 1]
            if identity_name == "id-optima-deploy":
                return {
                    "clientId": self.configuration.deployment_client_id,
                    "principalId": "deployment-principal-id",
                }
            if identity_name == "id-optima-api-hackathon":
                return {"principalId": "api-principal-id"}
            if identity_name == "id-optima-ui-hackathon":
                return {"principalId": "ui-principal-id"}
        if arguments[:3] == ("identity", "federated-credential", "list"):
            return [
                {
                    "audiences": ["api://AzureADTokenExchange"],
                    "issuer": "https://token.actions.githubusercontent.com",
                    "subject": "repo:sekharrcs/optima:environment:hackathon",
                }
            ]
        if arguments[:3] == ("ad", "app", "show"):
            return {
                "signInAudience": "AzureADMyOrg",
                "web": {"redirectUris": [self.configuration.ui_auth_redirect_uri]},
            }
        if arguments[:3] == ("ad", "sp", "show"):
            return {"appRoleAssignmentRequired": True}
        if arguments[:3] == ("rest", "--method", "get"):
            url = arguments[-1]
            if "/skus?" in url:
                return {
                    "value": [
                        {
                            "locations": ["East US 2"],
                            "name": "Balanced_B0",
                            "resourceType": "redisEnterprise",
                            "restrictions": [],
                        }
                    ]
                }
            if "/usages?" in url:
                return {
                    "value": [
                        {
                            "currentValue": 0,
                            "limit": 1,
                            "name": {"value": "Balanced B0"},
                        }
                    ]
                }
            if "/sqlRoleAssignments?" in url:
                cosmos_id = url.split("/sqlRoleAssignments?", maxsplit=1)[0]
                return {
                    "value": [
                        {
                            "properties": {
                                "principalId": "api-principal-id",
                                "roleDefinitionId": (
                                    f"{cosmos_id}/sqlRoleDefinitions/"
                                    "00000000-0000-0000-0000-000000000002"
                                ),
                                "scope": f"{cosmos_id}/dbs/optima/colls/runs",
                            }
                        }
                    ]
                }
            if "/accessPolicyAssignments?" in url:
                return {
                    "value": [
                        {
                            "properties": {
                                "accessPolicyName": "default",
                                "user": {"objectId": "api-principal-id"},
                            }
                        }
                    ]
                }
        if arguments[:3] == ("cognitiveservices", "account", "show"):
            return {"properties": {"endpoint": "https://aoai-optima.openai.azure.com/"}}
        if arguments[:4] == (
            "cognitiveservices",
            "account",
            "deployment",
            "show",
        ):
            deployment_name = arguments[arguments.index("--deployment-name") + 1]
            binding = next(
                binding
                for binding in self.configuration.models
                if binding.deployment == deployment_name
            )
            return {
                "properties": {
                    "model": {
                        "name": binding.model,
                        "version": binding.version,
                    },
                    "provisioningState": "Succeeded",
                },
                "sku": {"capacity": 1, "name": "GlobalStandard"},
            }
        if arguments[:2] == ("group", "show"):
            if not self.foundation_exists:
                return None
            return {"location": "eastus2"}
        if arguments[:2] == ("resource", "list"):
            resource_prefix = (
                f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/"
                "rg-optima-hackathon/providers"
            )
            return [
                {
                    "id": (
                        f"{resource_prefix}/Microsoft.App/managedEnvironments/"
                        "cae-optima-hackathon"
                    ),
                    "type": "Microsoft.App/managedEnvironments",
                },
                {
                    "id": (
                        f"{resource_prefix}/Microsoft.Cache/redisEnterprise/"
                        "redis-optima-test"
                    ),
                    "type": "Microsoft.Cache/redisEnterprise",
                },
                {
                    "id": (
                        f"{resource_prefix}/Microsoft.ContainerRegistry/registries/"
                        "acroptima123456789"
                    ),
                    "type": "Microsoft.ContainerRegistry/registries",
                },
                {
                    "id": (
                        f"{resource_prefix}/Microsoft.DocumentDB/databaseAccounts/"
                        "cosmos-optima-test"
                    ),
                    "type": "Microsoft.DocumentDB/databaseAccounts",
                },
                {
                    "id": (
                        f"{resource_prefix}/Microsoft.Insights/components/"
                        "appi-optima-hackathon"
                    ),
                    "type": "Microsoft.Insights/components",
                },
                {
                    "id": (
                        f"{resource_prefix}/Microsoft.ManagedIdentity/"
                        "userAssignedIdentities/id-optima-api-hackathon"
                    ),
                    "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
                },
                {
                    "id": (
                        f"{resource_prefix}/Microsoft.ManagedIdentity/"
                        "userAssignedIdentities/id-optima-ui-hackathon"
                    ),
                    "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
                },
            ]
        if arguments[:2] == ("acr", "show"):
            return {
                "adminUserEnabled": False,
                "id": (
                    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/"
                    "rg-optima-hackathon/providers/"
                    "Microsoft.ContainerRegistry/registries/acroptima123456789"
                ),
            }
        if arguments[:3] == ("role", "assignment", "list"):
            if (
                "--scope" in arguments
                and arguments[arguments.index("--scope") + 1] == OPENAI_RESOURCE_ID
            ):
                return [
                    {
                        "roleDefinitionId": (f"/providers/roles/{OPENAI_USER_ROLE_ID}"),
                        "scope": OPENAI_RESOURCE_ID,
                    }
                ]
            if "--scope" not in arguments:
                assignments = [
                    {
                        "roleDefinitionId": (f"/providers/roles/{CONTRIBUTOR_ROLE_ID}"),
                        "scope": (
                            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/"
                            "rg-optima-hackathon"
                            if self.foundation_exists
                            else f"/subscriptions/{SUBSCRIPTION_ID}"
                        ),
                    }
                ]
                if self.forbidden_deployment_role:
                    assignments.append(
                        {
                            "roleDefinitionId": (
                                "/providers/roles/8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
                            ),
                            "scope": f"/subscriptions/{SUBSCRIPTION_ID}",
                        }
                    )
                if self.foundation_exists:
                    assignments.append(
                        {
                            "roleDefinitionId": f"/providers/roles/{READER_ROLE_ID}",
                            "scope": f"/subscriptions/{SUBSCRIPTION_ID}",
                        }
                    )
                if self.foundation_exists and self.lingering_subscription_contributor:
                    assignments.append(
                        {
                            "roleDefinitionId": (
                                f"/providers/roles/{CONTRIBUTOR_ROLE_ID}"
                            ),
                            "scope": f"/subscriptions/{SUBSCRIPTION_ID}",
                        }
                    )
                return assignments
            if "--assignee-object-id" not in arguments:
                return [
                    {
                        "principalId": principal_id,
                        "roleDefinitionId": (
                            "/providers/roles/7f951dda-4ed3-4680-a7ca-43fe172d538d"
                        ),
                    }
                    for principal_id in ("api-principal-id", "ui-principal-id")
                ]
            return (
                [
                    {
                        "roleDefinitionId": f"/providers/roles/{ACR_PUSH_ROLE_ID}",
                        "scope": arguments[arguments.index("--scope") + 1],
                    }
                ]
                if self.acr_push
                else []
            )
        if arguments[:3] == ("acr", "manifest", "show-metadata"):
            reference = arguments[arguments.index("--name") + 1]
            return {"digest": reference.split("@", maxsplit=1)[1]}
        raise AssertionError(f"Unexpected Azure query: {arguments}")


def test_load_configuration_discards_secret_value() -> None:
    """Retain only secret presence so evidence cannot serialize the credential."""
    environment = valid_environment()

    configuration = load_configuration(environment)

    assert configuration.ui_auth_secret_present is True
    assert not hasattr(configuration, "ui_auth_client_secret")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "OPTIMA_FOUNDRY_SMALL_DEPLOYMENT",
            "replace-small-deployment",
            "still contains a placeholder",
        ),
        (
            "OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS",
            "0",
            "must be a positive decimal",
        ),
        (
            "AZURE_LOCATION",
            "westus3",
            "must remain eastus2",
        ),
    ],
)
def test_load_configuration_fails_closed(name: str, value: str, message: str) -> None:
    """Reject placeholders, misleading zero prices, and region substitution."""
    environment = valid_environment()
    environment[name] = value

    with pytest.raises(PreflightError, match=message):
        load_configuration(environment)


def test_configuration_rejects_shared_role_deployment() -> None:
    """Keep logical model roles bound to distinct Azure deployments."""
    environment = valid_environment()
    environment["OPTIMA_JUDGE_DEPLOYMENT"] = environment[
        "OPTIMA_FOUNDRY_STRONG_DEPLOYMENT"
    ]

    with pytest.raises(PreflightError, match="deployments must be distinct"):
        load_configuration(environment)


def test_configuration_rejects_stale_cost_review() -> None:
    """Require a recent fixed-infrastructure estimate before mutation."""
    configuration = load_configuration(valid_environment())
    stale = replace(configuration, cost_reviewed_on=date.today() - timedelta(days=32))

    with pytest.raises(PreflightError, match="no more than 31 days old"):
        validate_configuration(stale)


def test_configuration_rejects_infrastructure_cost_above_allocation() -> None:
    """Reserve the rest of the overall budget for variable model usage."""
    environment = valid_environment()
    environment["OPTIMA_EXPECTED_FIXED_MONTHLY_COST_INR"] = "5000.01"

    with pytest.raises(PreflightError, match="INR 5,000"):
        load_configuration(environment)


def test_configuration_rejects_pricing_source_query() -> None:
    """Keep signed URLs and query credentials out of preflight evidence."""
    environment = valid_environment()
    environment["OPTIMA_PRICING_SOURCE_URL"] += "?token=not-allowed"

    with pytest.raises(PreflightError, match="public HTTPS URL"):
        load_configuration(environment)


def test_configuration_rejects_foundry_url_credentials() -> None:
    """Keep user information out of ordinary Foundry endpoint configuration."""
    environment = valid_environment()
    environment["OPTIMA_FOUNDRY_BASE_URL"] = (
        "https://user:credential@aoai-optima.openai.azure.com/openai/v1"
    )

    with pytest.raises(PreflightError, match="HTTPS /openai/v1 API root"):
        load_configuration(environment)


def test_configuration_rejects_pricing_model_version_mismatch() -> None:
    """Bind each reviewed rate set to the exact live deployment model version."""
    environment = valid_environment()
    environment["OPTIMA_PRICING_JUDGE_MODEL_VERSION"] = "different-version"

    with pytest.raises(PreflightError, match="JUDGE pricing model/version"):
        load_configuration(environment)


def test_configuration_rejects_changed_reviewed_pricing_binding() -> None:
    """Invalidate the review digest when any exact rate changes."""
    environment = valid_environment()
    environment["OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS"] = "8.01"

    with pytest.raises(PreflightError, match="PRICING_BINDING_SHA256"):
        load_configuration(environment)


def test_foundation_preflight_is_read_only_and_redacts_identifiers() -> None:
    """Allow IaC-represented foundation creation only after all external gates pass."""
    configuration = load_configuration(valid_environment())
    azure = FakeAzure(configuration)

    evidence = run_preflight(
        configuration,
        azure,
        phase="foundation",
        repository_root=ROOT,
    )

    assert evidence["phase"] == "foundation"
    assert evidence["subscription_id"] == "1111...5555"
    assert evidence["tenant_id"] == "aaaa...eeee"
    assert evidence["artifacts"] == {}
    assert set(evidence["models"]) == {"SMALL", "STRONG", "JUDGE", "EMBEDDING"}
    assert not any(call[:2] == ("acr", "show") for call in azure.calls)


def test_foundation_preflight_rejects_forbidden_deployment_role() -> None:
    """Do not accept Owner or RBAC administration on the routine OIDC identity."""
    configuration = load_configuration(valid_environment())
    azure = FakeAzure(configuration, forbidden_deployment_role=True)

    with pytest.raises(PreflightError, match="forbidden Owner"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


def test_publish_preflight_rejects_lingering_subscription_contributor() -> None:
    """Remove bootstrap mutation scope before routine image publication."""
    configuration = load_configuration(valid_environment())
    azure = FakeAzure(
        configuration,
        foundation_exists=True,
        lingering_subscription_contributor=True,
    )

    with pytest.raises(PreflightError, match="Contributor only"):
        run_preflight(
            configuration,
            azure,
            phase="publish",
            repository_root=ROOT,
        )


def test_publish_preflight_requires_explicit_acr_push() -> None:
    """Do not infer data-plane publication rights from control-plane access."""
    configuration = load_configuration(valid_environment())
    azure = FakeAzure(configuration, foundation_exists=True, acr_push=False)

    with pytest.raises(PreflightError, match="lacks AcrPush"):
        run_preflight(
            configuration,
            azure,
            phase="publish",
            repository_root=ROOT,
        )


def test_artifact_preflight_verifies_both_distinct_registry_digests() -> None:
    """Require registry-generated API and UI manifest digests before rollout."""
    configuration = load_configuration(valid_environment())
    azure = FakeAzure(configuration, foundation_exists=True)
    api_digest = "sha256:" + ("a" * 64)
    ui_digest = "sha256:" + ("b" * 64)

    evidence = run_preflight(
        configuration,
        azure,
        phase="artifacts",
        repository_root=ROOT,
        api_digest=api_digest,
        ui_digest=ui_digest,
    )

    assert evidence["artifacts"] == {"api": api_digest, "ui": ui_digest}
    manifest_calls = [
        call for call in azure.calls if call[:3] == ("acr", "manifest", "show-metadata")
    ]
    assert len(manifest_calls) == 2


def test_rollout_preflight_verifies_runtime_access_and_artifacts() -> None:
    """Re-read every runtime grant immediately before Container Apps mutation."""
    configuration = load_configuration(valid_environment())
    azure = FakeAzure(configuration, foundation_exists=True)

    evidence = run_preflight(
        configuration,
        azure,
        phase="rollout",
        repository_root=ROOT,
        api_digest="sha256:" + ("a" * 64),
        ui_digest="sha256:" + ("b" * 64),
    )

    assert "runtime_access" in evidence["checks"]
    assert "immutable_artifacts" in evidence["checks"]
    assert any(
        call[:3] == ("rest", "--method", "get") and "/sqlRoleAssignments?" in call[-1]
        for call in azure.calls
    )


def test_artifact_preflight_rejects_image_id_or_shared_digest() -> None:
    """Reject mutable, local, placeholder, and cross-component identifiers."""
    configuration = load_configuration(valid_environment())
    azure = FakeAzure(configuration, foundation_exists=True)
    digest = "sha256:" + ("a" * 64)

    with pytest.raises(PreflightError, match="must be distinct"):
        run_preflight(
            configuration,
            azure,
            phase="artifacts",
            repository_root=ROOT,
            api_digest=digest,
            ui_digest=digest,
        )
