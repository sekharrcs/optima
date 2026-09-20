"""Behavioral tests for the read-only Azure deployment preflight."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import tempfile
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import azure_preflight, production_parameters
from scripts import whatif_classification as classifier
from scripts.azure_preflight import (
    ACR_PULL_ROLE_ID,
    ACR_PUSH_ROLE_ID,
    CONTRIBUTOR_ROLE_ID,
    GRAPH_APPLICATION_READ_ALL_ROLE_ID,
    MICROSOFT_GRAPH_APP_ID,
    MICROSOFT_GRAPH_HOST,
    OPENAI_USER_ROLE_ID,
    PREFLIGHT_CACHE_ONLY_SETTINGS,
    READER_ROLE_ID,
    REDIS_API_VERSION,
    REQUIRED_PROVIDERS,
    AzureQueryError,
    AzureQueryFailureKind,
    DeploymentConfiguration,
    PreflightError,
    PricingConfiguration,
    RedisPreflightError,
    RedisPreflightErrorCode,
    _classify_azure_query_failure,
    load_configuration,
    pricing_binding_sha256,
    run_preflight,
    validate_configuration,
)

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
TENANT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_ID = "12345678-abcd-4321-abcd-123456789abc"
DEPLOYMENT_PRINCIPAL_ID = "22222222-3333-4444-5555-666666666666"
FOUNDATION_PLAN_CLIENT_ID = "87654321-dcba-1234-dcba-cba987654321"
FOUNDATION_PLAN_PRINCIPAL_ID = "77777777-6666-5555-4444-333333333333"
FOUNDATION_PLAN_ROLE_DEFINITION_ID = "99999999-8888-7777-6666-555555555555"
TRANSITIVE_GROUP_ID = "88888888-7777-6666-5555-444444444444"
GRAPH_SERVICE_PRINCIPAL_ID = "aaaaaaaa-1111-2222-3333-444444444444"
GRAPH_ASSIGNMENT_ID = (
    base64.urlsafe_b64encode(b"synthetic-graph-assignment-primary")
    .decode("ascii")
    .rstrip("=")
)
SECOND_GRAPH_ASSIGNMENT_ID = (
    base64.urlsafe_b64encode(b"synthetic-graph-assignment-secondary")
    .decode("ascii")
    .rstrip("=")
)
UI_AUTH_CLIENT_ID = "ui-auth-client-id"
UI_AUTH_CLIENT_SECRET_ENV = "".join(("OPTIMA_UI_AUTH_", "CLIENT_SECRET"))
OPENAI_RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-foundry/providers/"
    "Microsoft.CognitiveServices/accounts/aoai-optima"
)
REDIS_SKU_URL = (
    f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}/providers/"
    f"Microsoft.Cache/skus?api-version={REDIS_API_VERSION}"
)
REDIS_USAGE_URL = (
    f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}/providers/"
    "Microsoft.Cache/locations/eastus2/usages"
    f"?api-version={REDIS_API_VERSION}"
)
# The approved container registry name is exactly ``acroptima`` followed by the
# 13-character ARM ``uniqueString`` suffix that the Bicep contract emits, so the
# fail-closed classifier accepts it as the ARM-evaluated identity.
APPROVED_REGISTRY_SUFFIX = "0123456789abc"
APPROVED_REGISTRY_NAME = f"acroptima{APPROVED_REGISTRY_SUFFIX}"
PRODUCTION_RESOURCE_GROUP = "rg-optima-hackathon"
COMMIT_SHA = "a" * 40


def role_definition_resource_id(role_id: str) -> str:
    """Return one canonical subscription role-definition ARM ID."""
    return (
        f"/subscriptions/{SUBSCRIPTION_ID}/providers/"
        f"Microsoft.Authorization/roleDefinitions/{role_id}"
    )


def synthetic_access_token(
    *,
    tenant_id: str,
    client_id: str,
    principal_id: str,
    client_claim_name: str = "appid",
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Build a non-secret JWT-shaped value for local claim parsing tests."""

    def encode(document: dict[str, Any]) -> str:
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    claims: dict[str, Any] = {
        client_claim_name: client_id,
        "oid": principal_id,
        "tid": tenant_id,
    }
    claims.update(extra_claims or {})
    return f"{encode({'alg': 'RS256', 'typ': 'JWT'})}.{encode(claims)}.signature"


def effective_assignment(
    role_id: str,
    scope: str,
    *,
    principal_id: str,
    principal_type: str = "ServicePrincipal",
) -> dict[str, str]:
    """Return one canonical effective Azure RBAC assignment."""
    return {
        "principalId": principal_id,
        "principalType": principal_type,
        "roleDefinitionId": role_definition_resource_id(role_id),
        "scope": scope,
    }


def graph_assignment(
    *,
    assignment_id: str = GRAPH_ASSIGNMENT_ID,
    app_role_id: str = GRAPH_APPLICATION_READ_ALL_ROLE_ID,
    principal_id: str = DEPLOYMENT_PRINCIPAL_ID,
    resource_id: str = GRAPH_SERVICE_PRINCIPAL_ID,
    odata_type: str = "#microsoft.graph.appRoleAssignment",
) -> dict[str, str]:
    """Return one complete Microsoft Graph app-role assignment fixture."""
    return {
        "@odata.type": odata_type,
        "appRoleId": app_role_id,
        "id": assignment_id,
        "principalId": principal_id,
        "resourceId": resource_id,
    }


def apply_effective_assignments() -> list[dict[str, str]]:
    """Return the exact post-bootstrap apply role set."""
    subscription_scope = f"/subscriptions/{SUBSCRIPTION_ID}"
    return [
        effective_assignment(
            READER_ROLE_ID,
            subscription_scope,
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
        effective_assignment(
            CONTRIBUTOR_ROLE_ID,
            f"{subscription_scope}/resourceGroups/rg-optima-hackathon",
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
    ]


def plan_effective_assignments() -> list[dict[str, str]]:
    """Return the exact read-only foundation plan role set."""
    subscription_scope = f"/subscriptions/{SUBSCRIPTION_ID}"
    return [
        effective_assignment(
            READER_ROLE_ID,
            subscription_scope,
            principal_id=FOUNDATION_PLAN_PRINCIPAL_ID,
        ),
        effective_assignment(
            FOUNDATION_PLAN_ROLE_DEFINITION_ID,
            f"{subscription_scope}/resourceGroups/rg-optima-hackathon",
            principal_id=FOUNDATION_PLAN_PRINCIPAL_ID,
        ),
    ]


def foundation_plan_role_definition(
    *,
    actions: list[str] | None = None,
    assignable_scopes: list[str] | None = None,
) -> dict[str, Any]:
    """Return the exact non-mutating custom role definition for planning."""
    return {
        "assignableScopes": assignable_scopes
        if assignable_scopes is not None
        else [f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-optima-hackathon"],
        "id": role_definition_resource_id(FOUNDATION_PLAN_ROLE_DEFINITION_ID),
        "permissions": [
            {
                "actions": actions
                if actions is not None
                else ["Microsoft.Resources/deployments/whatIf/action"],
                "dataActions": [],
                "notActions": [],
                "notDataActions": [],
            }
        ],
        "roleType": "CustomRole",
    }


def redis_provider_metadata(*, quota_advertised: bool = False) -> dict[str, Any]:
    """Return stable Microsoft.Cache regional resource-type metadata."""
    resource_types: list[dict[str, Any]] = [
        {
            "apiVersions": [REDIS_API_VERSION],
            "locations": ["East US 2"],
            "resourceType": "redisEnterprise",
            "zoneMappings": [{"location": "East US 2", "zones": ["1", "2", "3"]}],
        }
    ]
    if quota_advertised:
        resource_types.append(
            {
                "apiVersions": [REDIS_API_VERSION],
                "resourceType": "locations/usages",
            }
        )
    return {
        "registrationState": "Registered",
        "resourceTypes": resource_types,
    }


def redis_sku_item(**overrides: Any) -> dict[str, Any]:
    """Return one exact stable Redis Enterprise Balanced B0 SKU entry."""
    item: dict[str, Any] = {
        "locationInfo": [{"location": "East US 2", "zones": ["1", "2", "3"]}],
        "locations": ["East US 2"],
        "name": "Balanced_B0",
        "resourceType": "redisEnterprise",
        "restrictions": [],
        "tier": "Balanced",
    }
    item.update(overrides)
    return item


def valid_environment() -> dict[str, str]:
    """Return a complete synthetic deployment environment."""
    return {
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "present",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.invalid/oidc",
        "AZURE_CLIENT_ID": CLIENT_ID,
        "AZURE_CONTAINER_REGISTRY_NAME": APPROVED_REGISTRY_NAME,
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
        "OPTIMA_SEMANTIC_CACHE_ENABLED": "true",
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


def disabled_environment() -> dict[str, str]:
    """Return complete production inputs with cache-only values absent."""
    environment = valid_environment()
    environment["OPTIMA_SEMANTIC_CACHE_ENABLED"] = "false"
    for name in (
        "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS",
        "OPTIMA_PRICING_EMBEDDING_MODEL",
        "OPTIMA_PRICING_EMBEDDING_MODEL_VERSION",
        "OPTIMA_REDIS_EMBEDDING_DEPLOYMENT",
        "OPTIMA_REDIS_EMBEDDING_DIMENSION",
        "OPTIMA_REDIS_EMBEDDING_MODEL",
        "OPTIMA_REDIS_EMBEDDING_MODEL_VERSION",
    ):
        environment.pop(name)
    environment["OPTIMA_PRICING_BINDING_SHA256"] = pricing_binding_sha256(
        PricingConfiguration(
            catalog_version=environment["OPTIMA_PRICING_CATALOG_VERSION"],
            binding_sha256="0" * 64,
            source_url=environment["OPTIMA_PRICING_SOURCE_URL"],
            currency=environment["OPTIMA_PRICING_CURRENCY"],
            small_model=environment["OPTIMA_PRICING_SMALL_MODEL"],
            small_model_version=environment["OPTIMA_PRICING_SMALL_MODEL_VERSION"],
            small_input=Decimal(
                environment["OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS"]
            ),
            small_output=Decimal(
                environment["OPTIMA_PRICING_SMALL_OUTPUT_RATE_PER_MILLION_TOKENS"]
            ),
            small_cached_input=None,
            strong_model=environment["OPTIMA_PRICING_STRONG_MODEL"],
            strong_model_version=environment["OPTIMA_PRICING_STRONG_MODEL_VERSION"],
            strong_input=Decimal(
                environment["OPTIMA_PRICING_STRONG_INPUT_RATE_PER_MILLION_TOKENS"]
            ),
            strong_output=Decimal(
                environment["OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS"]
            ),
            strong_cached_input=None,
            judge_model=environment["OPTIMA_PRICING_JUDGE_MODEL"],
            judge_model_version=environment["OPTIMA_PRICING_JUDGE_MODEL_VERSION"],
            judge_input=Decimal(
                environment["OPTIMA_PRICING_JUDGE_INPUT_RATE_PER_MILLION_TOKENS"]
            ),
            judge_output=Decimal(
                environment["OPTIMA_PRICING_JUDGE_OUTPUT_RATE_PER_MILLION_TOKENS"]
            ),
            judge_cached_input=None,
            embedding_model=None,
            embedding_model_version=None,
            embedding_input=None,
        )
    )
    return environment


def foundation_environment() -> dict[str, str]:
    """Return only the inputs a read-only foundation plan genuinely needs.

    Deferred UI, image-publication, Azure OpenAI runtime, and pricing inputs are
    absent, matching the currently configured hackathon environment variables.
    """
    return {
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "present",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.invalid/oidc",
        "AZURE_CLIENT_ID": CLIENT_ID,
        "AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID": (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-identity/providers/"
            "Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-deploy"
        ),
        "AZURE_LOCATION": "eastus2",
        "AZURE_RESOURCE_GROUP": "rg-optima-hackathon",
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
        "AZURE_TENANT_ID": TENANT_ID,
        "GITHUB_REPOSITORY": "sekharrcs/optima",
        "OPTIMA_COST_REVIEWED_ON": date.today().isoformat(),
        "OPTIMA_EXPECTED_FIXED_MONTHLY_COST_INR": "1600",
        "OPTIMA_GITHUB_ENVIRONMENT": "hackathon",
        "OPTIMA_SEMANTIC_CACHE_ENABLED": "false",
    }


def foundation_plan_environment() -> dict[str, str]:
    """Return the minimal foundation contract with a distinct plan identity."""
    environment = foundation_environment()
    environment.update(
        {
            "AZURE_FOUNDATION_PLAN_CLIENT_ID": FOUNDATION_PLAN_CLIENT_ID,
            "AZURE_FOUNDATION_PLAN_IDENTITY_RESOURCE_ID": (
                f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-identity/"
                "providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
                "id-optima-foundation-plan"
            ),
            "AZURE_FOUNDATION_PLAN_ROLE_DEFINITION_ID": (
                FOUNDATION_PLAN_ROLE_DEFINITION_ID
            ),
        }
    )
    return environment


def foundation_apply_environment() -> dict[str, str]:
    """Return the minimal separated apply contract with both identities."""
    environment = foundation_environment()
    environment.update(
        {
            "AZURE_FOUNDATION_PLAN_CLIENT_ID": FOUNDATION_PLAN_CLIENT_ID,
            "AZURE_FOUNDATION_PLAN_IDENTITY_RESOURCE_ID": (
                f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-identity/"
                "providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
                "id-optima-foundation-plan"
            ),
        }
    )
    return environment


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
        lingering_redis: bool = False,
    ) -> None:
        self.configuration = configuration
        self.foundation_exists = foundation_exists
        self.acr_push = acr_push
        self.forbidden_deployment_role = forbidden_deployment_role
        self.lingering_subscription_contributor = lingering_subscription_contributor
        self.lingering_redis = lingering_redis
        self.deployment_principal_id = (
            FOUNDATION_PLAN_PRINCIPAL_ID
            if configuration.foundation_plan_role_definition_id is not None
            else DEPLOYMENT_PRINCIPAL_ID
        )
        self.session_client_id = configuration.deployment_client_id
        self.session_principal_id = self.deployment_principal_id
        self.session_tenant_id = configuration.tenant_id
        self.account_user_type = "servicePrincipal"
        self.returned_identity_id = configuration.deployment_identity_resource_id
        self.returned_identity_client_id = configuration.deployment_client_id
        self.effective_assignments: list[dict[str, Any]] | None = None
        self.access_token: str | None = None
        self.role_definition_response: Any = None
        self.transitive_group_ids: list[str] = []
        self.transitive_group_response: Any = None
        self.group_role_assignments: dict[str, list[dict[str, Any]]] = {}
        # Live-inventory registry identity is independent of both the configured
        # registry name and the AcrPush role-assignment scope so tests can prove
        # the approved registry is bound from inventory, not configuration.
        self.inventory_registry_name = APPROVED_REGISTRY_NAME
        self.inventory_registry_resource_group: str | None = None
        self.inventory_registry_id: str | None = None
        self.inventory_registry_type = "Microsoft.ContainerRegistry/registries"
        self.inventory_registry_provisioning_state: Any = "Succeeded"
        self.inventory_registries: list[dict[str, Any]] | None = None
        self.acr_assignment_registry_name: str | None = None
        # Microsoft Graph application-permission evidence for the executing
        # identity, defaulting to exactly the approved Application.Read.All role.
        self.graph_service_principal_id = GRAPH_SERVICE_PRINCIPAL_ID
        self.graph_service_principal_response: Any = None
        self.graph_app_role_assignments: list[dict[str, Any]] | None = None
        self.graph_app_role_assignments_response: Any = None
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
                "user": {
                    "name": self.session_client_id,
                    "type": self.account_user_type,
                },
            }
        if arguments[:2] == ("account", "get-access-token"):
            return {
                "accessToken": self.access_token
                or synthetic_access_token(
                    tenant_id=self.session_tenant_id,
                    client_id=self.session_client_id,
                    principal_id=self.session_principal_id,
                ),
                "tokenType": "Bearer",
            }
        if arguments[:2] == ("provider", "show"):
            assert arguments[-1] in REQUIRED_PROVIDERS
            if arguments[-1] == "Microsoft.Cache":
                return redis_provider_metadata()
            return {"registrationState": "Registered"}
        if arguments[:2] == ("identity", "show"):
            identity_name = arguments[arguments.index("--name") + 1]
            if identity_name in {"id-optima-deploy", "id-optima-foundation-plan"}:
                return {
                    "clientId": self.returned_identity_client_id,
                    "id": self.returned_identity_id,
                    "principalId": self.deployment_principal_id,
                }
            if identity_name == "id-optima-api-hackathon":
                return {"principalId": "api-principal-id"}
            if identity_name == "id-optima-ui-hackathon":
                return {"principalId": "ui-principal-id"}
        if arguments[:3] == ("identity", "federated-credential", "list"):
            return [
                {
                    "id": (
                        f"{self.configuration.deployment_identity_resource_id}/"
                        "federatedIdentityCredentials/github-optima-hackathon"
                    ),
                    "name": "github-optima-hackathon",
                    "audiences": ["api://AzureADTokenExchange"],
                    "issuer": "https://token.actions.githubusercontent.com",
                    "subject": (
                        "repo:sekharrcs@45002138/optima@1333906197:environment:hackathon"
                    ),
                }
            ]
        if arguments[:3] == ("role", "definition", "list"):
            if self.role_definition_response is not None:
                return self.role_definition_response
            role_id = self.configuration.foundation_plan_role_definition_id
            assert role_id is not None
            definition = foundation_plan_role_definition()
            definition["id"] = role_definition_resource_id(role_id)
            return [definition]
        if arguments[:3] == ("ad", "app", "show"):
            return {
                "signInAudience": "AzureADMyOrg",
                "web": {"redirectUris": [self.configuration.ui_auth_redirect_uri]},
            }
        if arguments[:3] == ("ad", "sp", "show"):
            return {"appRoleAssignmentRequired": True}
        if arguments[:3] == ("rest", "--method", "get"):
            url = arguments[arguments.index("--url") + 1]
            if url.startswith(f"https://{MICROSOFT_GRAPH_HOST}/v1.0/"):
                if "servicePrincipals(appId=" in url:
                    if self.graph_service_principal_response is not None:
                        return self.graph_service_principal_response
                    return {
                        "id": self.graph_service_principal_id,
                        "appId": MICROSOFT_GRAPH_APP_ID,
                        "appRoles": [
                            {
                                "id": GRAPH_APPLICATION_READ_ALL_ROLE_ID,
                                "isEnabled": True,
                                "allowedMemberTypes": ["Application"],
                            }
                        ],
                    }
                if "/appRoleAssignments" in url:
                    if self.graph_app_role_assignments_response is not None:
                        return self.graph_app_role_assignments_response
                    values = self.graph_app_role_assignments
                    if values is None:
                        values = [
                            graph_assignment(
                                principal_id=self.deployment_principal_id,
                                resource_id=self.graph_service_principal_id,
                            )
                        ]
                    return {
                        "@odata.count": len(values),
                        "value": values,
                    }
                if self.transitive_group_response is not None:
                    return self.transitive_group_response
                values = [
                    {"@odata.type": "#microsoft.graph.group", "id": group_id}
                    for group_id in self.transitive_group_ids
                ]
                return {
                    "@odata.count": len(values),
                    "value": values,
                }
            if "/skus?" in url:
                assert url == REDIS_SKU_URL
                return {"value": [redis_sku_item()]}
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
            resources = [
                {
                    "id": (
                        f"{resource_prefix}/Microsoft.App/managedEnvironments/"
                        "cae-optima-hackathon"
                    ),
                    "type": "Microsoft.App/managedEnvironments",
                },
                {
                    "id": (
                        f"{resource_prefix}/Microsoft.ContainerRegistry/registries/"
                        f"{APPROVED_REGISTRY_NAME}"
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
            if self.configuration.semantic_cache_enabled or self.lingering_redis:
                resources.append(
                    {
                        "id": (
                            f"{resource_prefix}/Microsoft.Cache/redisEnterprise/"
                            "redis-optima-test"
                        ),
                        "type": "Microsoft.Cache/redisEnterprise",
                    }
                )
            return resources
        if arguments[:2] == ("acr", "list"):
            if self.inventory_registries is not None:
                return self.inventory_registries
            registry_resource_group = (
                self.inventory_registry_resource_group
                or self.configuration.resource_group
            )
            registry_id = self.inventory_registry_id or (
                f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/"
                f"{registry_resource_group}/providers/"
                "Microsoft.ContainerRegistry/registries/"
                f"{self.inventory_registry_name}"
            )
            return [
                {
                    "id": registry_id,
                    "name": self.inventory_registry_name,
                    "type": self.inventory_registry_type,
                    "resourceGroup": registry_resource_group,
                    "provisioningState": self.inventory_registry_provisioning_state,
                }
            ]
        if arguments[:2] == ("acr", "show"):
            return {
                "adminUserEnabled": False,
                "id": (
                    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/"
                    "rg-optima-hackathon/providers/"
                    f"Microsoft.ContainerRegistry/registries/{APPROVED_REGISTRY_NAME}"
                ),
            }
        if arguments[:3] == ("role", "assignment", "list"):
            if (
                "--scope" in arguments
                and arguments[arguments.index("--scope") + 1] == OPENAI_RESOURCE_ID
            ):
                return [
                    {
                        "roleDefinitionId": role_definition_resource_id(
                            OPENAI_USER_ROLE_ID
                        ),
                        "scope": OPENAI_RESOURCE_ID,
                    }
                ]
            assignee = (
                arguments[arguments.index("--assignee-object-id") + 1]
                if "--assignee-object-id" in arguments
                else None
            )
            if assignee in self.group_role_assignments:
                assignments = self.group_role_assignments[assignee]
                if "--scope" not in arguments:
                    return [
                        assignment
                        for assignment in assignments
                        if assignment["scope"]
                        .casefold()
                        .startswith(f"/subscriptions/{SUBSCRIPTION_ID}".casefold())
                    ]
                return assignments
            subscription_scope = f"/subscriptions/{SUBSCRIPTION_ID}"
            if (
                "--scope" in arguments
                and arguments[arguments.index("--scope") + 1] == subscription_scope
                and "--include-inherited" in arguments
            ):
                source = self.effective_assignments
                if source is None:
                    role_id = (
                        READER_ROLE_ID
                        if self.foundation_exists
                        or self.configuration.foundation_plan_role_definition_id
                        is not None
                        else CONTRIBUTOR_ROLE_ID
                    )
                    source = [
                        effective_assignment(
                            role_id,
                            subscription_scope,
                            principal_id=self.deployment_principal_id,
                        )
                    ]
                return [
                    assignment
                    for assignment in source
                    if assignment["scope"].casefold() == subscription_scope.casefold()
                    or assignment["scope"]
                    .casefold()
                    .startswith("/providers/microsoft.management/managementgroups/")
                ]
            if "--scope" not in arguments:
                if self.effective_assignments is not None:
                    return self.effective_assignments
                resource_group_scope = (
                    f"{subscription_scope}/resourceGroups/rg-optima-hackathon"
                )
                if self.configuration.foundation_plan_role_definition_id is not None:
                    assignments = [
                        effective_assignment(
                            READER_ROLE_ID,
                            subscription_scope,
                            principal_id=self.deployment_principal_id,
                        ),
                        effective_assignment(
                            self.configuration.foundation_plan_role_definition_id,
                            resource_group_scope,
                            principal_id=self.deployment_principal_id,
                        ),
                    ]
                elif self.foundation_exists:
                    assignments = [
                        effective_assignment(
                            READER_ROLE_ID,
                            subscription_scope,
                            principal_id=self.deployment_principal_id,
                        ),
                        effective_assignment(
                            CONTRIBUTOR_ROLE_ID,
                            resource_group_scope,
                            principal_id=self.deployment_principal_id,
                        ),
                    ]
                    if self.configuration.registry_name is not None and self.acr_push:
                        assignment_registry_name = (
                            self.acr_assignment_registry_name or APPROVED_REGISTRY_NAME
                        )
                        assignments.append(
                            effective_assignment(
                                ACR_PUSH_ROLE_ID,
                                (
                                    f"{resource_group_scope}/providers/"
                                    "Microsoft.ContainerRegistry/registries/"
                                    f"{assignment_registry_name}"
                                ),
                                principal_id=self.deployment_principal_id,
                            )
                        )
                else:
                    assignments = [
                        effective_assignment(
                            CONTRIBUTOR_ROLE_ID,
                            subscription_scope,
                            principal_id=self.deployment_principal_id,
                        )
                    ]
                if self.forbidden_deployment_role:
                    assignments.append(
                        effective_assignment(
                            "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
                            subscription_scope,
                            principal_id=self.deployment_principal_id,
                        )
                    )
                if self.foundation_exists and self.lingering_subscription_contributor:
                    assignments.append(
                        effective_assignment(
                            CONTRIBUTOR_ROLE_ID,
                            subscription_scope,
                            principal_id=self.deployment_principal_id,
                        )
                    )
                return assignments
            if "--assignee-object-id" not in arguments:
                registry_scope = arguments[arguments.index("--scope") + 1]
                return [
                    {
                        "principalId": principal_id,
                        "roleDefinitionId": role_definition_resource_id(
                            "7f951dda-4ed3-4680-a7ca-43fe172d538d"
                        ),
                        "scope": registry_scope,
                    }
                    for principal_id in ("api-principal-id", "ui-principal-id")
                ]
            return (
                [
                    {
                        "roleDefinitionId": role_definition_resource_id(
                            ACR_PUSH_ROLE_ID
                        ),
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


class RedisContractAzure(FakeAzure):
    """Inject exact Redis metadata and failures without making live calls."""

    def __init__(
        self,
        configuration: DeploymentConfiguration,
        *,
        provider: dict[str, Any] | None = None,
        sku_documents: dict[str, Any] | None = None,
        quota_document: Any = None,
        quota_failure: AzureQueryFailureKind | None = None,
        query_failures: dict[str, AzureQueryFailureKind] | None = None,
    ) -> None:
        super().__init__(configuration)
        self.provider = provider if provider is not None else redis_provider_metadata()
        self.sku_documents = (
            sku_documents
            if sku_documents is not None
            else {REDIS_SKU_URL: {"value": [redis_sku_item()]}}
        )
        self.quota_document = quota_document
        self.quota_failure = quota_failure
        self.query_failures = query_failures or {}

    def json(self, *arguments: str, allow_missing: bool = False) -> Any:
        """Return focused Redis responses before using shared fake behavior."""
        if arguments == ("provider", "show", "--namespace", "Microsoft.Cache"):
            self.calls.append(arguments)
            return self.provider
        if arguments[:3] == ("rest", "--method", "get"):
            url = arguments[-1]
            if url in self.query_failures:
                self.calls.append(arguments)
                raise AzureQueryError(self.query_failures[url], "rest")
            if url in self.sku_documents:
                self.calls.append(arguments)
                return self.sku_documents[url]
            if url == REDIS_USAGE_URL:
                self.calls.append(arguments)
                if self.quota_failure is not None:
                    raise AzureQueryError(self.quota_failure, "rest")
                if self.quota_document is not None:
                    return self.quota_document
        return super().json(*arguments, allow_missing=allow_missing)


def run_foundation_preflight(azure: FakeAzure) -> dict[str, Any]:
    """Run the complete foundation preflight around one Redis test fixture."""
    return run_preflight(
        azure.configuration,
        azure,
        phase="foundation",
        repository_root=ROOT,
    )


def assert_redis_failure(
    azure: FakeAzure, expected_code: RedisPreflightErrorCode
) -> RedisPreflightError:
    """Assert one hard-blocking typed Redis outcome and return it."""
    with pytest.raises(RedisPreflightError) as captured:
        run_foundation_preflight(azure)
    assert captured.value.code == expected_code
    return captured.value


def test_load_configuration_discards_secret_value() -> None:
    """Retain only secret presence so evidence cannot serialize the credential."""
    environment = valid_environment()

    configuration = load_configuration(environment)

    assert configuration.ui_auth_secret_present is True
    assert not hasattr(configuration, "ui_auth_client_secret")


def test_load_configuration_accepts_explicit_disabled_cache_profile() -> None:
    """Keep only active model and pricing bindings when caching is disabled."""
    configuration = load_configuration(disabled_environment())

    assert configuration.semantic_cache_enabled is False
    assert configuration.embedding_dimension is None
    assert {binding.role for binding in configuration.models} == {
        "SMALL",
        "STRONG",
        "JUDGE",
    }
    assert configuration.pricing is not None
    assert configuration.pricing.embedding_model is None
    assert configuration.pricing.embedding_model_version is None
    assert configuration.pricing.embedding_input is None


def test_model_versions_remain_separate_from_pricing_binding_contract() -> None:
    """Keep deployment and pricing identities explicit without changing the digest."""
    environment = valid_environment()
    configuration = load_configuration(environment)

    assert {
        binding.role: (binding.model, binding.version)
        for binding in configuration.models
    } == {
        "SMALL": ("gpt-4.1-mini", "2025-04-14"),
        "STRONG": ("gpt-4.1", "2025-04-14"),
        "JUDGE": ("gpt-4.1-nano", "2025-04-14"),
        "EMBEDDING": ("text-embedding-3-small", "1"),
    }
    assert configuration.pricing is not None
    assert (
        pricing_binding_sha256(configuration.pricing)
        == environment["OPTIMA_PRICING_BINDING_SHA256"]
        == "904c36f6deaf8ea867be97dbdd9fa57f54135d868c8bb8291586fc7c767284cd"
    )


def test_load_configuration_requires_explicit_cache_decision() -> None:
    """Reject an omitted cache mode instead of inferring it from cache settings."""
    environment = valid_environment()
    environment.pop("OPTIMA_SEMANTIC_CACHE_ENABLED")

    with pytest.raises(PreflightError, match="SEMANTIC_CACHE_ENABLED is missing"):
        load_configuration(environment)


@pytest.mark.parametrize(
    "name",
    sorted(PREFLIGHT_CACHE_ONLY_SETTINGS),
)
def test_disabled_configuration_rejects_cache_only_setting(name: str) -> None:
    """Reject stale cache-only deployment inputs in the disabled profile."""
    environment = disabled_environment()
    environment[name] = "contradictory"

    with pytest.raises(PreflightError, match=f"settings: {name}"):
        load_configuration(environment)


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
    assert evidence["redis"]["api_version"] == REDIS_API_VERSION
    assert evidence["redis"]["quota"]["status"] == "NOT_EXPOSED"
    assert evidence["redis"]["allocation"]["status"] == "NOT_PROVABLE_BEFORE_CREATION"
    assert "redis_balanced_b0_availability_and_quota" not in evidence["checks"]
    assert {
        "redis_allocation_not_provable",
        "redis_applicable_restrictions",
        "redis_exact_sku_advertisement",
        "redis_provider_registration",
        "redis_quota_exposure",
        "redis_regional_resource_type",
    } <= set(evidence["checks"])
    assert not any(call[:2] == ("acr", "show") for call in azure.calls)


def test_disabled_foundation_preflight_skips_only_cache_dependencies() -> None:
    """Prove Redis and embedding are unreachable while other gates still run."""
    configuration = load_configuration(disabled_environment())
    azure = FakeAzure(configuration)

    evidence = run_foundation_preflight(azure)

    assert evidence["semantic_cache"] == {
        "embedding_configuration": "NOT_CONFIGURED",
        "enabled": False,
        "redis_resource": "NOT_PROVISIONED",
        "status": "DISABLED",
    }
    assert evidence["redis"] is None
    assert set(evidence["models"]) == {"SMALL", "STRONG", "JUDGE"}
    assert {
        "semantic_cache_explicitly_disabled",
        "redis_resource_absent",
        "embedding_configuration_absent",
    } <= set(evidence["checks"])
    assert not any(
        call == ("provider", "show", "--namespace", "Microsoft.Cache")
        for call in azure.calls
    )
    assert not any(
        call[:3] == ("rest", "--method", "get") and "/skus?" in call[-1]
        for call in azure.calls
    )
    deployment_calls = [
        call
        for call in azure.calls
        if call[:4] == ("cognitiveservices", "account", "deployment", "show")
    ]
    assert len(deployment_calls) == 3


def test_disabled_publish_preflight_rejects_lingering_redis_resource() -> None:
    """Fail before incremental deployment when Redis is not actually absent."""
    configuration = load_configuration(disabled_environment())
    azure = FakeAzure(
        configuration,
        foundation_exists=True,
        lingering_redis=True,
    )

    with pytest.raises(PreflightError, match="Managed Redis to be absent"):
        run_preflight(
            configuration,
            azure,
            phase="publish",
            repository_root=ROOT,
        )


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


def test_disabled_rollout_omits_redis_resource_and_access_queries() -> None:
    """Keep ACR and Cosmos access gates without requiring a Redis assignment."""
    configuration = load_configuration(disabled_environment())
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
    assert any(
        call[:3] == ("rest", "--method", "get") and "/sqlRoleAssignments?" in call[-1]
        for call in azure.calls
    )
    assert not any(
        call[:3] == ("rest", "--method", "get")
        and "/accessPolicyAssignments?" in call[-1]
        for call in azure.calls
    )


def test_production_foundation_verifies_runtime_access_before_mutation() -> None:
    """F8: the exact runtime-access check runs before the first foundation create."""
    configuration = load_configuration(valid_environment())
    azure = FakeAzure(configuration, foundation_exists=True)

    evidence = run_preflight(
        configuration,
        azure,
        phase="production-foundation",
        repository_root=ROOT,
    )

    assert "runtime_access" in evidence["checks"]
    assert any(
        call[:3] == ("rest", "--method", "get") and "/sqlRoleAssignments?" in call[-1]
        for call in azure.calls
    )


def test_publish_verifies_runtime_access_before_image_push() -> None:
    """F8: the exact runtime-access check runs before image publication."""
    configuration = load_configuration(valid_environment())
    azure = FakeAzure(configuration, foundation_exists=True)

    evidence = run_preflight(
        configuration,
        azure,
        phase="publish",
        repository_root=ROOT,
    )

    assert "runtime_access" in evidence["checks"]
    assert any(
        call[:3] == ("rest", "--method", "get") and "/sqlRoleAssignments?" in call[-1]
        for call in azure.calls
    )


def test_production_foundation_rejects_missing_ui_acr_pull() -> None:
    """F8: a UI identity without AcrPull fails the early runtime-access gate."""

    class MissingUiAcrPull(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if (
                arguments[:3] == ("role", "assignment", "list")
                and "--all" in arguments
                and "--assignee-object-id" not in arguments
                and isinstance(result, list)
            ):
                return [
                    assignment
                    for assignment in result
                    if assignment.get("principalId") != "ui-principal-id"
                ]
            return result

    configuration = load_configuration(valid_environment())
    azure = MissingUiAcrPull(configuration, foundation_exists=True)

    with pytest.raises(PreflightError, match="UI identity must have exactly AcrPull"):
        run_preflight(
            configuration,
            azure,
            phase="production-foundation",
            repository_root=ROOT,
        )


def test_production_foundation_rejects_wrong_scope_cosmos_data_role() -> None:
    """F8: a Cosmos data role at the wrong scope fails the early runtime-access gate."""

    class WrongCosmosScope(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if (
                arguments[:3] == ("rest", "--method", "get")
                and "/sqlRoleAssignments?" in arguments[-1]
                and isinstance(result, dict)
            ):
                for assignment in result.get("value", []):
                    assignment["properties"]["scope"] = assignment["properties"][
                        "scope"
                    ].replace("/colls/runs", "/colls/other")
            return result

    configuration = load_configuration(valid_environment())
    azure = WrongCosmosScope(configuration, foundation_exists=True)

    with pytest.raises(PreflightError, match="container-scoped Cosmos data"):
        run_preflight(
            configuration,
            azure,
            phase="production-foundation",
            repository_root=ROOT,
        )


_OWNER_ROLE_ID = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"


def _reject_production_foundation(azure: FakeAzure, match: str) -> None:
    with pytest.raises(PreflightError, match=match):
        run_preflight(
            azure.configuration,
            azure,
            phase="production-foundation",
            repository_root=ROOT,
        )


def _is_registry_pull_query(arguments: tuple[str, ...]) -> bool:
    return (
        arguments[:3] == ("role", "assignment", "list")
        and "--all" in arguments
        and "--assignee-object-id" not in arguments
    )


def test_runtime_access_rejects_broader_api_registry_grant() -> None:
    """An API identity with a broader registry role than AcrPull fails closed."""

    class BroaderApiRegistryGrant(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if _is_registry_pull_query(arguments) and isinstance(result, list):
                scope = arguments[arguments.index("--scope") + 1]
                return [
                    *result,
                    {
                        "principalId": "api-principal-id",
                        "roleDefinitionId": role_definition_resource_id(_OWNER_ROLE_ID),
                        "scope": scope,
                    },
                ]
            return result

    configuration = load_configuration(valid_environment())
    _reject_production_foundation(
        BroaderApiRegistryGrant(configuration, foundation_exists=True),
        "API identity must have exactly AcrPull",
    )


def test_runtime_access_rejects_duplicate_api_registry_grant() -> None:
    """A duplicated AcrPull grant for a runtime identity fails closed."""

    class DuplicateApiRegistryGrant(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if _is_registry_pull_query(arguments) and isinstance(result, list):
                duplicate = next(
                    dict(entry)
                    for entry in result
                    if entry.get("principalId") == "api-principal-id"
                )
                return [*result, duplicate]
            return result

    configuration = load_configuration(valid_environment())
    _reject_production_foundation(
        DuplicateApiRegistryGrant(configuration, foundation_exists=True),
        "API identity must have exactly AcrPull",
    )


def test_runtime_access_rejects_conditional_registry_grant() -> None:
    """A conditional registry grant for a runtime identity fails closed."""

    class ConditionalRegistryGrant(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if _is_registry_pull_query(arguments) and isinstance(result, list):
                conditioned = []
                for entry in result:
                    updated = dict(entry)
                    if updated.get("principalId") == "api-principal-id":
                        updated["condition"] = "@Resource[x] StringEquals 'y'"
                    conditioned.append(updated)
                return conditioned
            return result

    configuration = load_configuration(valid_environment())
    _reject_production_foundation(
        ConditionalRegistryGrant(configuration, foundation_exists=True),
        "conditional role assignment",
    )


def test_runtime_access_rejects_wrong_scope_registry_grant() -> None:
    """A registry grant reported at an unexpected scope fails closed."""

    class WrongScopeRegistryGrant(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if _is_registry_pull_query(arguments) and isinstance(result, list):
                rewritten = []
                for entry in result:
                    updated = dict(entry)
                    if updated.get("principalId") == "api-principal-id":
                        updated["scope"] = f"/subscriptions/{SUBSCRIPTION_ID}"
                    rewritten.append(updated)
                return rewritten
            return result

    configuration = load_configuration(valid_environment())
    _reject_production_foundation(
        WrongScopeRegistryGrant(configuration, foundation_exists=True),
        "unexpected scope",
    )


def test_runtime_access_rejects_broader_deployer_acr_push() -> None:
    """A deployer with a broader registry role than AcrPush fails closed."""

    class BroaderDeployerAcrPush(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if (
                arguments[:3] == ("role", "assignment", "list")
                and "--assignee-object-id" in arguments
                and "--scope" in arguments
                and "ContainerRegistry/registries"
                in arguments[arguments.index("--scope") + 1]
                and isinstance(result, list)
            ):
                scope = arguments[arguments.index("--scope") + 1]
                return [
                    *result,
                    {
                        "roleDefinitionId": role_definition_resource_id(_OWNER_ROLE_ID),
                        "scope": scope,
                    },
                ]
            return result

    configuration = load_configuration(valid_environment())
    _reject_production_foundation(
        BroaderDeployerAcrPush(configuration, foundation_exists=True),
        "must have exactly AcrPush",
    )


def test_runtime_access_rejects_broader_foundry_grant() -> None:
    """An API identity with a broader AOAI role than OpenAI User fails closed."""

    class BroaderFoundryGrant(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if (
                "--scope" in arguments
                and arguments[arguments.index("--scope") + 1] == OPENAI_RESOURCE_ID
                and isinstance(result, list)
            ):
                return [
                    *result,
                    {
                        "roleDefinitionId": role_definition_resource_id(_OWNER_ROLE_ID),
                        "scope": OPENAI_RESOURCE_ID,
                    },
                ]
            return result

    configuration = load_configuration(valid_environment())
    _reject_production_foundation(
        BroaderFoundryGrant(configuration, foundation_exists=True),
        "must have exactly Cognitive Services OpenAI User",
    )


def test_runtime_access_rejects_duplicate_cosmos_data_grant() -> None:
    """A duplicated container-scoped Cosmos grant for the API fails closed."""

    class DuplicateCosmosGrant(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if (
                arguments[:3] == ("rest", "--method", "get")
                and "/sqlRoleAssignments?" in arguments[-1]
                and isinstance(result, dict)
            ):
                values = result.get("value", [])
                result["value"] = [*values, dict(values[0])]
            return result

    configuration = load_configuration(valid_environment())
    _reject_production_foundation(
        DuplicateCosmosGrant(configuration, foundation_exists=True),
        "container-scoped Cosmos data",
    )


def test_runtime_access_rejects_broader_cosmos_account_grant() -> None:
    """An API Cosmos grant at the broader account scope fails closed."""

    class AccountScopedCosmosGrant(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            result = super().json(*arguments, allow_missing=allow_missing)
            if (
                arguments[:3] == ("rest", "--method", "get")
                and "/sqlRoleAssignments?" in arguments[-1]
                and isinstance(result, dict)
            ):
                for assignment in result.get("value", []):
                    properties = assignment["properties"]
                    properties["scope"] = properties["scope"].split(
                        "/dbs/", maxsplit=1
                    )[0]
            return result

    configuration = load_configuration(valid_environment())
    _reject_production_foundation(
        AccountScopedCosmosGrant(configuration, foundation_exists=True),
        "container-scoped Cosmos data",
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


def test_redis_sku_catalog_follows_safe_pagination() -> None:
    """Search all bounded Microsoft.Cache SKU pages before deciding eligibility."""
    configuration = load_configuration(valid_environment())
    next_url = f"{REDIS_SKU_URL}&$skiptoken=page-2"
    azure = RedisContractAzure(
        configuration,
        sku_documents={
            REDIS_SKU_URL: {
                "nextLink": next_url,
                "value": [redis_sku_item(name="Balanced_B1")],
            },
            next_url: {"value": [redis_sku_item()]},
        },
    )

    evidence = run_foundation_preflight(azure)

    assert evidence["redis"]["sku"]["catalog_page_count"] == 2
    assert evidence["redis"]["sku"]["catalog_item_count"] == 2
    assert [
        call[-1]
        for call in azure.calls
        if call[:3] == ("rest", "--method", "get") and "/skus?" in call[-1]
    ] == [REDIS_SKU_URL, next_url]


@pytest.mark.parametrize(
    ("diagnostic", "expected_kind"),
    [
        ("ERROR: status code: 404", AzureQueryFailureKind.NOT_FOUND),
        ("ERROR: Not Found", AzureQueryFailureKind.NOT_FOUND),
        ("ERROR: status code: 403", AzureQueryFailureKind.UNAUTHORIZED),
        ("ERROR: status code: 429", AzureQueryFailureKind.TRANSIENT),
        ("ERROR: status code: 503", AzureQueryFailureKind.TRANSIENT),
        ("ERROR: unexpected provider failure", AzureQueryFailureKind.OTHER),
    ],
    ids=(
        "status-not-found",
        "cli-not-found",
        "unauthorized",
        "throttled",
        "server",
        "other",
    ),
)
def test_azure_query_failure_classification_is_sanitized_and_typed(
    diagnostic: str, expected_kind: AzureQueryFailureKind
) -> None:
    """Classify status evidence without preserving response text in the outcome."""
    assert _classify_azure_query_failure(diagnostic) == expected_kind


@pytest.mark.parametrize(
    "next_url",
    [
        (
            "https://example.invalid/subscriptions/"
            f"{SUBSCRIPTION_ID}/providers/Microsoft.Cache/skus"
            f"?api-version={REDIS_API_VERSION}"
        ),
        REDIS_SKU_URL.replace("management.azure.com", "management.azure.com:8443"),
        "https://[malformed/subscriptions/continuation",
    ],
    ids=("wrong-host", "non-default-port", "malformed-authority"),
)
def test_redis_sku_pagination_rejects_unapproved_continuation(
    next_url: str,
) -> None:
    """Reject pagination that changes or malforms the approved ARM authority."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        sku_documents={
            REDIS_SKU_URL: {
                "nextLink": next_url,
                "value": [],
            }
        },
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.SKU_PAGINATION_INVALID)


def test_redis_sku_accepts_locations_field() -> None:
    """Treat the exact SKU locations array as regional advertisement evidence."""
    configuration = load_configuration(valid_environment())
    item = redis_sku_item()
    item.pop("locationInfo")
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"value": [item]}},
    )

    evidence = run_foundation_preflight(azure)

    assert evidence["redis"]["sku"]["advertised_locations"] == ["eastus2"]
    assert evidence["redis"]["sku"]["target_region_zones"] == []


def test_redis_sku_accepts_location_info_and_preserves_zones() -> None:
    """Use locationInfo regional evidence and retain zones only as metadata."""
    configuration = load_configuration(valid_environment())
    item = redis_sku_item()
    item.pop("locations")
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"value": [item]}},
    )

    evidence = run_foundation_preflight(azure)

    assert evidence["redis"]["sku"]["advertised_locations"] == ["eastus2"]
    assert evidence["redis"]["sku"]["target_region_zones"] == ["1", "2", "3"]
    assert evidence["redis"]["allocation"]["status"] == "NOT_PROVABLE_BEFORE_CREATION"


def test_redis_exact_fields_are_case_normalized() -> None:
    """Normalize casing and surrounding whitespace without weakening exact tokens."""
    configuration = load_configuration(valid_environment())
    provider = redis_provider_metadata()
    provider["registrationState"] = " registered "
    provider_resource_type = provider["resourceTypes"][0]
    provider_resource_type["resourceType"] = " REDISENTERPRISE "
    provider_resource_type["locations"] = [" EAST US 2 "]
    azure = RedisContractAzure(
        configuration,
        provider=provider,
        sku_documents={
            REDIS_SKU_URL: {
                "value": [
                    redis_sku_item(
                        locations=[" EAST US 2 "],
                        locationInfo=[],
                        name=" balanced_b0 ",
                        resourceType=" REDISENTERPRISE ",
                        tier=" balanced ",
                    )
                ]
            }
        },
    )

    evidence = run_foundation_preflight(azure)

    assert evidence["redis"]["sku"]["name"] == "Balanced_B0"
    assert evidence["redis"]["sku"]["tier"] == "Balanced"


@pytest.mark.parametrize(
    "item",
    [
        redis_sku_item(name="Balanced_B01"),
        redis_sku_item(resourceType="redisEnterprise/databases"),
        redis_sku_item(tier="Premium"),
    ],
    ids=("substring-name", "database-child", "wrong-tier"),
)
def test_redis_requires_exact_cluster_sku_identity(item: dict[str, Any]) -> None:
    """Reject substring names, child resources, alternate tiers, and other SKUs."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"value": [item]}},
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.REQUESTED_SKU_ABSENT)


def test_redis_explicit_target_region_restriction_hard_blocks() -> None:
    """Block an exact East US 2 location restriction with a typed code."""
    configuration = load_configuration(valid_environment())
    item = redis_sku_item(
        restrictions=[
            {
                "reasonCode": "RestrictedByPolicy",
                "type": "Location",
                "values": ["East US 2"],
            }
        ]
    )
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"value": [item]}},
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.TARGET_REGION_RESTRICTED)


def test_redis_unrelated_region_and_zone_restrictions_do_not_block() -> None:
    """Ignore restrictions scoped to another region or to an unrequested zone."""
    configuration = load_configuration(valid_environment())
    item = redis_sku_item(
        restrictions=[
            {
                "reasonCode": "NotAvailableForSubscription",
                "type": "Location",
                "values": ["West US"],
            },
            {
                "reasonCode": "QuotaId",
                "restrictionInfo": {
                    "locations": ["East US 2"],
                    "zones": ["1"],
                },
                "type": "Zone",
                "values": ["1"],
            },
        ]
    )
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"value": [item]}},
    )

    evidence = run_foundation_preflight(azure)

    assert evidence["redis"]["restrictions"] == {
        "applicable": 0,
        "ignored_unrelated": 2,
        "status": "NONE_APPLICABLE",
    }


@pytest.mark.parametrize(
    ("restriction", "expected_code"),
    [
        (
            {
                "reasonCode": "NotAvailableForSubscription",
                "type": "Location",
                "values": [],
            },
            RedisPreflightErrorCode.SUBSCRIPTION_RESTRICTED,
        ),
        (
            {
                "reasonCode": "NotAvailableForSubscription",
                "type": "Location",
                "values": ["East US 2"],
            },
            RedisPreflightErrorCode.SUBSCRIPTION_RESTRICTED,
        ),
        (
            {
                "reasonCode": "QuotaId",
                "type": "Location",
                "values": ["East US 2"],
            },
            RedisPreflightErrorCode.QUOTA_RESTRICTED,
        ),
    ],
    ids=("global-subscription", "regional-subscription", "regional-quota"),
)
def test_redis_subscription_and_quota_restrictions_hard_block(
    restriction: dict[str, Any], expected_code: RedisPreflightErrorCode
) -> None:
    """Keep subscription and quota restriction outcomes typed and distinct."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        sku_documents={
            REDIS_SKU_URL: {"value": [redis_sku_item(restrictions=[restriction])]}
        },
    )

    assert_redis_failure(azure, expected_code)


def test_redis_exact_sku_absent_from_target_region_hard_blocks() -> None:
    """Do not infer East US 2 eligibility from an exact SKU in another region."""
    configuration = load_configuration(valid_environment())
    item = redis_sku_item(
        locations=["Australia Central"],
        locationInfo=[{"location": "Australia Central", "zones": []}],
    )
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"value": [item]}},
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.REQUESTED_SKU_REGION_ABSENT)


def test_redis_quota_route_is_not_queried_when_metadata_does_not_advertise_it() -> None:
    """Report quota as not exposed without probing an unadvertised route."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(configuration)

    evidence = run_foundation_preflight(azure)

    assert evidence["redis"]["quota"] == {
        "source": "PROVIDER_METADATA",
        "status": "NOT_EXPOSED",
    }
    assert not any("/usages?" in call[-1] for call in azure.calls)


def test_redis_advertised_quota_route_404_is_not_exposed() -> None:
    """Classify a typed 404 as non-exposure rather than available quota."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        provider=redis_provider_metadata(quota_advertised=True),
        quota_failure=AzureQueryFailureKind.NOT_FOUND,
    )

    evidence = run_foundation_preflight(azure)

    assert evidence["redis"]["quota"] == {
        "source": "ADVERTISED_ROUTE_NOT_FOUND",
        "status": "NOT_EXPOSED",
    }
    assert REDIS_USAGE_URL in [call[-1] for call in azure.calls]


def test_redis_quota_continuation_404_hard_blocks() -> None:
    """Do not downgrade a failed continuation after receiving quota evidence."""
    configuration = load_configuration(valid_environment())
    next_url = f"{REDIS_USAGE_URL}&$skiptoken=page-2"
    azure = RedisContractAzure(
        configuration,
        provider=redis_provider_metadata(quota_advertised=True),
        quota_document={
            "nextLink": next_url,
            "value": [
                {
                    "currentValue": 0,
                    "limit": 1,
                    "name": {"value": "Balanced_B0"},
                }
            ],
        },
        query_failures={next_url: AzureQueryFailureKind.NOT_FOUND},
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.QUOTA_QUERY_FAILED)


def test_redis_authoritative_quota_available() -> None:
    """Accept one exact invariant meter only when current usage is below limit."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        provider=redis_provider_metadata(quota_advertised=True),
        quota_document={
            "value": [
                {
                    "currentValue": 1,
                    "limit": 2,
                    "name": {"localizedValue": "Balanced B0", "value": "Balanced_B0"},
                }
            ]
        },
    )

    evidence = run_foundation_preflight(azure)

    assert evidence["redis"]["quota"] == {
        "current_value": 1,
        "limit": 2,
        "page_count": 1,
        "source": "MICROSOFT_CACHE_LOCATIONS_USAGES",
        "status": "AVAILABLE",
    }


@pytest.mark.parametrize(("current", "limit"), [(1, 1), (2, 1)])
def test_redis_authoritative_quota_exhausted_hard_blocks(
    current: int, limit: int
) -> None:
    """Block exact authoritative quota when current usage meets or exceeds limit."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        provider=redis_provider_metadata(quota_advertised=True),
        quota_document={
            "value": [
                {
                    "currentValue": current,
                    "limit": limit,
                    "name": {"value": "Balanced_B0"},
                }
            ]
        },
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.QUOTA_EXHAUSTED)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            AzureQueryFailureKind.UNAUTHORIZED,
            RedisPreflightErrorCode.QUOTA_QUERY_UNAUTHORIZED,
        ),
        (
            AzureQueryFailureKind.TRANSIENT,
            RedisPreflightErrorCode.QUOTA_QUERY_TRANSIENT,
        ),
        (
            AzureQueryFailureKind.OTHER,
            RedisPreflightErrorCode.QUOTA_QUERY_FAILED,
        ),
    ],
    ids=("unauthorized", "transient", "unclassified"),
)
def test_redis_quota_query_failures_hard_block_distinctly(
    failure: AzureQueryFailureKind, expected_code: RedisPreflightErrorCode
) -> None:
    """Never convert authorization, transient, or unknown failures into availability."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        provider=redis_provider_metadata(quota_advertised=True),
        quota_failure=failure,
    )

    assert_redis_failure(azure, expected_code)


@pytest.mark.parametrize(
    "quota_document",
    [
        {
            "value": [
                {
                    "currentValue": False,
                    "limit": True,
                    "name": {"value": "Balanced_B0"},
                }
            ]
        },
        {
            "value": [
                {
                    "currentValue": 0,
                    "limit": 1,
                    "name": {"value": "Balanced_B01"},
                }
            ]
        },
        {
            "value": [
                {
                    "currentValue": -1,
                    "limit": 1,
                    "name": {"value": "Balanced_B0"},
                }
            ]
        },
    ],
    ids=("boolean-values", "substring-meter", "negative-value"),
)
def test_redis_malformed_quota_response_hard_blocks(
    quota_document: dict[str, Any],
) -> None:
    """Reject non-integer, non-exact, and negative authoritative quota evidence."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        provider=redis_provider_metadata(quota_advertised=True),
        quota_document=quota_document,
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.QUOTA_RESPONSE_MALFORMED)


def test_redis_malformed_locations_hard_block() -> None:
    """Do not treat a null regional location collection as absent evidence."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        sku_documents={
            REDIS_SKU_URL: {
                "value": [redis_sku_item(locations=None, locationInfo=None)]
            }
        },
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.SKU_RESPONSE_MALFORMED)


def test_redis_provider_requires_target_region_and_stable_api() -> None:
    """Block unsupported regions and API versions without probing fallbacks."""
    configuration = load_configuration(valid_environment())
    region_provider = redis_provider_metadata()
    region_provider["resourceTypes"][0]["locations"] = ["West US"]
    region_azure = RedisContractAzure(configuration, provider=region_provider)
    version_provider = redis_provider_metadata()
    version_provider["resourceTypes"][0]["apiVersions"] = ["2024-11-01"]
    version_azure = RedisContractAzure(configuration, provider=version_provider)

    assert_redis_failure(region_azure, RedisPreflightErrorCode.REGION_NOT_ADVERTISED)
    assert_redis_failure(
        version_azure, RedisPreflightErrorCode.API_VERSION_NOT_ADVERTISED
    )
    assert not any(
        call[:3] == ("rest", "--method", "get")
        and "management.azure.com" in call[call.index("--url") + 1]
        for call in region_azure.calls
    )
    assert not any(
        call[:3] == ("rest", "--method", "get")
        and "management.azure.com" in call[call.index("--url") + 1]
        for call in version_azure.calls
    )


@pytest.mark.parametrize(
    ("provider", "expected_code"),
    [
        (
            {**redis_provider_metadata(), "registrationState": "Unregistered"},
            RedisPreflightErrorCode.PROVIDER_NOT_REGISTERED,
        ),
        (
            {**redis_provider_metadata(), "registrationState": None},
            RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
        ),
        (
            {"registrationState": "Registered", "resourceTypes": []},
            RedisPreflightErrorCode.RESOURCE_TYPE_NOT_ADVERTISED,
        ),
    ],
    ids=("unregistered", "malformed-registration", "resource-type-absent"),
)
def test_redis_provider_registration_and_resource_type_fail_closed(
    provider: dict[str, Any], expected_code: RedisPreflightErrorCode
) -> None:
    """Require registered, well-formed Microsoft.Cache Redis provider metadata."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(configuration, provider=provider)

    assert_redis_failure(azure, expected_code)


def test_redis_evidence_never_claims_precreation_allocation() -> None:
    """Keep catalog and zone evidence separate from physical capacity allocation."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(configuration)

    redis_evidence = run_foundation_preflight(azure)["redis"]

    assert redis_evidence["provider"]["regional_support"] == "ADVERTISED"
    assert redis_evidence["sku"]["status"] == "ADVERTISED"
    assert redis_evidence["allocation"] == {
        "statement": (
            "Provider and SKU metadata do not reserve or guarantee current "
            "physical capacity."
        ),
        "status": "NOT_PROVABLE_BEFORE_CREATION",
    }


def test_redis_failure_does_not_query_region_sku_service_or_version_fallbacks() -> None:
    """Stop after the exact East US 2 B0 catalog result without fallback probes."""
    configuration = load_configuration(valid_environment())
    item = redis_sku_item(
        locations=["Australia Central"],
        locationInfo=[{"location": "Australia Central", "zones": []}],
    )
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"value": [item]}},
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.REQUESTED_SKU_REGION_ABSENT)

    rest_urls = [
        call[call.index("--url") + 1]
        for call in azure.calls
        if call[:3] == ("rest", "--method", "get")
        and "management.azure.com" in call[call.index("--url") + 1]
    ]
    assert rest_urls == [REDIS_SKU_URL]
    assert all("2024-11-01" not in url for url in rest_urls)
    assert all("Microsoft.Compute" not in url for url in rest_urls)
    assert all("/providers/Microsoft.Cache/redis?" not in url for url in rest_urls)


def test_redis_sku_pagination_rejects_cycle() -> None:
    """Refuse a continuation that returns to an already-read SKU page."""
    configuration = load_configuration(valid_environment())
    next_url = f"{REDIS_SKU_URL}&$skiptoken=page-2"
    azure = RedisContractAzure(
        configuration,
        sku_documents={
            REDIS_SKU_URL: {"nextLink": next_url, "value": []},
            next_url: {"nextLink": REDIS_SKU_URL, "value": []},
        },
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.SKU_PAGINATION_INVALID)

    sku_calls = [
        call[-1]
        for call in azure.calls
        if call[:3] == ("rest", "--method", "get") and "/skus?" in call[-1]
    ]
    assert sku_calls == [REDIS_SKU_URL, next_url]


def test_redis_sku_pagination_rejects_excessive_pages() -> None:
    """Stop after the bounded page limit instead of following endless links."""
    configuration = load_configuration(valid_environment())
    urls = [REDIS_SKU_URL] + [
        f"{REDIS_SKU_URL}&$skiptoken=page-{index}" for index in range(1, 40)
    ]
    sku_documents: dict[str, Any] = {
        url: {"nextLink": urls[position + 1], "value": []}
        for position, url in enumerate(urls[:-1])
    }
    sku_documents[urls[-1]] = {"value": []}
    azure = RedisContractAzure(configuration, sku_documents=sku_documents)

    assert_redis_failure(azure, RedisPreflightErrorCode.SKU_PAGINATION_INVALID)

    sku_calls = [
        call[-1]
        for call in azure.calls
        if call[:3] == ("rest", "--method", "get") and "/skus?" in call[-1]
    ]
    assert len(sku_calls) == 32


@pytest.mark.parametrize(
    "next_url",
    [
        (
            "https://management.azure.com/subscriptions/"
            "99999999-9999-9999-9999-999999999999/providers/Microsoft.Cache/skus"
            f"?api-version={REDIS_API_VERSION}"
        ),
        (
            f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}/providers/"
            f"Microsoft.Cache/redis?api-version={REDIS_API_VERSION}"
        ),
        (
            f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}/providers/"
            "Microsoft.Cache/skus?api-version=2024-11-01"
        ),
    ],
    ids=("cross-subscription", "route-mutation", "api-version-downgrade"),
)
def test_redis_sku_pagination_rejects_authority_preserving_mutations(
    next_url: str,
) -> None:
    """Reject same-host continuations that change subscription, route, or version."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"nextLink": next_url, "value": []}},
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.SKU_PAGINATION_INVALID)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            AzureQueryFailureKind.NOT_FOUND,
            RedisPreflightErrorCode.SKU_QUERY_NOT_FOUND,
        ),
        (
            AzureQueryFailureKind.UNAUTHORIZED,
            RedisPreflightErrorCode.SKU_QUERY_UNAUTHORIZED,
        ),
        (
            AzureQueryFailureKind.TRANSIENT,
            RedisPreflightErrorCode.SKU_QUERY_TRANSIENT,
        ),
        (
            AzureQueryFailureKind.OTHER,
            RedisPreflightErrorCode.SKU_QUERY_FAILED,
        ),
    ],
    ids=("not-found", "unauthorized", "transient", "unclassified"),
)
def test_redis_sku_query_failures_hard_block_distinctly(
    failure: AzureQueryFailureKind, expected_code: RedisPreflightErrorCode
) -> None:
    """Never turn a failed SKU catalog query into regional eligibility evidence."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        query_failures={REDIS_SKU_URL: failure},
    )

    assert_redis_failure(azure, expected_code)


def _duplicate_redis_resource_type_provider() -> dict[str, Any]:
    """Return provider metadata that advertises redisEnterprise twice."""
    provider = redis_provider_metadata()
    provider["resourceTypes"].append(
        {
            "apiVersions": [REDIS_API_VERSION],
            "locations": ["East US 2"],
            "resourceType": "redisEnterprise",
        }
    )
    return provider


def _duplicate_quota_resource_type_provider() -> dict[str, Any]:
    """Return provider metadata that advertises the quota route twice."""
    provider = redis_provider_metadata(quota_advertised=True)
    provider["resourceTypes"].append(
        {"apiVersions": [REDIS_API_VERSION], "resourceType": "locations/usages"}
    )
    return provider


@pytest.mark.parametrize(
    "provider_factory",
    [
        _duplicate_redis_resource_type_provider,
        _duplicate_quota_resource_type_provider,
    ],
    ids=("duplicate-redis-enterprise", "duplicate-quota"),
)
def test_redis_duplicate_resource_types_fail_closed(
    provider_factory: Any,
) -> None:
    """Refuse contradictory provider metadata that advertises a type twice."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(configuration, provider=provider_factory())

    assert_redis_failure(azure, RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED)


def test_redis_global_unknown_restriction_hard_blocks() -> None:
    """Block an applicable restriction whose reason code is not recognized."""
    configuration = load_configuration(valid_environment())
    item = redis_sku_item(
        restrictions=[{"reasonCode": "SomeNewReason", "type": "Location", "values": []}]
    )
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"value": [item]}},
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.RESTRICTION_UNKNOWN)


def test_redis_malformed_restriction_fails_closed() -> None:
    """Reject a restriction that omits its type or reason code."""
    configuration = load_configuration(valid_environment())
    item = redis_sku_item(restrictions=[{"type": "Location", "values": ["East US 2"]}])
    azure = RedisContractAzure(
        configuration,
        sku_documents={REDIS_SKU_URL: {"value": [item]}},
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.RESTRICTION_MALFORMED)


def test_redis_quota_route_wrong_api_version_fails_closed() -> None:
    """Reject an advertised quota route that omits the approved API version."""
    configuration = load_configuration(valid_environment())
    provider = redis_provider_metadata(quota_advertised=True)
    for resource_type in provider["resourceTypes"]:
        if resource_type["resourceType"] == "locations/usages":
            resource_type["apiVersions"] = ["2024-11-01"]
    azure = RedisContractAzure(configuration, provider=provider)

    assert_redis_failure(
        azure, RedisPreflightErrorCode.QUOTA_API_VERSION_NOT_ADVERTISED
    )


def test_redis_quota_pagination_rejects_unapproved_continuation() -> None:
    """Reject a quota continuation that leaves the approved usages route."""
    configuration = load_configuration(valid_environment())
    cross_route = (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}/providers/"
        f"Microsoft.Cache/skus?api-version={REDIS_API_VERSION}"
    )
    azure = RedisContractAzure(
        configuration,
        provider=redis_provider_metadata(quota_advertised=True),
        quota_document={
            "nextLink": cross_route,
            "value": [
                {"currentValue": 0, "limit": 1, "name": {"value": "Balanced_B0"}}
            ],
        },
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.QUOTA_PAGINATION_INVALID)


def test_redis_multiple_quota_meters_fail_closed() -> None:
    """Refuse contradictory quota evidence that returns two exact B0 meters."""
    configuration = load_configuration(valid_environment())
    azure = RedisContractAzure(
        configuration,
        provider=redis_provider_metadata(quota_advertised=True),
        quota_document={
            "value": [
                {"currentValue": 0, "limit": 1, "name": {"value": "Balanced_B0"}},
                {"currentValue": 0, "limit": 2, "name": {"value": "Balanced_B0"}},
            ]
        },
    )

    assert_redis_failure(azure, RedisPreflightErrorCode.QUOTA_RESPONSE_MALFORMED)


def test_foundation_apply_loads_without_deferred_runtime_inputs() -> None:
    """Load foundation apply from only the inputs foundation resources need."""
    configuration = load_configuration(foundation_environment(), phase="foundation")

    assert configuration.pricing is None
    assert configuration.models == ()
    assert configuration.registry_name is None
    assert configuration.openai_resource_id is None
    assert configuration.foundry_base_url is None
    assert configuration.ui_auth_client_id is None
    assert configuration.ui_auth_tenant_id is None
    assert configuration.ui_auth_secret_present is False
    assert configuration.semantic_cache_enabled is False


def test_foundation_plan_selects_distinct_read_only_identity() -> None:
    """Keep planning credentials separate from the foundation apply identity."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )

    assert configuration.deployment_client_id == FOUNDATION_PLAN_CLIENT_ID
    assert configuration.deployment_identity_resource_id.endswith(
        "/id-optima-foundation-plan"
    )
    assert (
        configuration.foundation_plan_role_definition_id
        == FOUNDATION_PLAN_ROLE_DEFINITION_ID
    )


@pytest.mark.parametrize("shared_value", ["client", "resource"])
def test_foundation_plan_rejects_apply_identity_alias(shared_value: str) -> None:
    """Require separate client and managed-identity resource identities."""
    environment = foundation_plan_environment()
    if shared_value == "client":
        environment["AZURE_CLIENT_ID"] = FOUNDATION_PLAN_CLIENT_ID
        message = "client IDs must be distinct"
    else:
        environment["AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID"] = environment[
            "AZURE_FOUNDATION_PLAN_IDENTITY_RESOURCE_ID"
        ]
        message = "identity resource IDs must be distinct"

    with pytest.raises(PreflightError, match=message):
        load_configuration(environment, phase="foundation-plan")


def test_cache_enabled_foundation_plan_is_rejected() -> None:
    """Keep Redis provisioning unreachable from the read-only plan identity."""
    environment = foundation_plan_environment()
    environment["OPTIMA_SEMANTIC_CACHE_ENABLED"] = "true"

    with pytest.raises(PreflightError, match="foundation-plan does not support"):
        load_configuration(environment, phase="foundation-plan")


@pytest.mark.parametrize(
    "value",
    ["not-a-guid", "99999999-8888-7777-6666-55555555555z"],
)
def test_foundation_plan_requires_stable_custom_role_guid(value: str) -> None:
    """Reject mutable display names and malformed plan role identities."""
    environment = foundation_plan_environment()
    environment["AZURE_FOUNDATION_PLAN_ROLE_DEFINITION_ID"] = value

    with pytest.raises(PreflightError, match="canonical GUID"):
        load_configuration(environment, phase="foundation-plan")


def test_cache_enabled_foundation_loads_complete_runtime_configuration() -> None:
    """Validate the complete cache runtime contract before Redis can be created."""
    configuration = load_configuration(valid_environment(), phase="foundation")

    assert configuration.registry_name == APPROVED_REGISTRY_NAME
    assert configuration.pricing is not None
    assert {binding.role for binding in configuration.models} == {
        "SMALL",
        "STRONG",
        "JUDGE",
        "EMBEDDING",
    }


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "OPTIMA_PRICING_CATALOG_VERSION",
            None,
            "OPTIMA_PRICING_CATALOG_VERSION is missing",
        ),
        (
            "OPTIMA_PRICING_EMBEDDING_MODEL",
            "different-embedding-model",
            "EMBEDDING pricing model/version",
        ),
    ],
)
def test_cache_enabled_foundation_rejects_incomplete_runtime_configuration(
    name: str,
    value: str | None,
    message: str,
) -> None:
    """Reject missing or mismatched runtime evidence before Redis provisioning."""
    environment = valid_environment()
    if value is None:
        environment.pop(name)
    else:
        environment[name] = value

    with pytest.raises(PreflightError, match=message):
        load_configuration(environment, phase="foundation")


def test_foundation_apply_uses_the_same_phase_aware_input_contract() -> None:
    """Keep plan and apply on the same minimal disabled-cache foundation inputs."""
    plan = load_configuration(foundation_plan_environment(), phase="foundation-plan")
    apply = load_configuration(foundation_environment(), phase="foundation")

    assert plan.deployment_client_id != apply.deployment_client_id
    assert plan.models == apply.models == ()
    assert plan.pricing is apply.pricing is None
    assert plan.semantic_cache_enabled is apply.semantic_cache_enabled is False
    assert apply.pricing is None
    assert apply.ui_auth_secret_present is False


def test_foundation_plan_preflight_is_read_only_and_skips_runtime_checks() -> None:
    """Verify the foundation plan omits UI, ACR, model, and runtime checks."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)

    evidence = run_preflight(
        configuration,
        azure,
        phase="foundation-plan",
        repository_root=ROOT,
    )

    assert evidence["phase"] == "foundation-plan"
    assert "model_deployments" not in evidence["checks"]
    assert "ui_entra_authentication" not in evidence["checks"]
    assert "acr_push" not in evidence["checks"]
    assert "foundry_runtime_access" not in evidence["checks"]
    assert "foundation_resources" not in evidence["checks"]
    assert evidence["subscription_id"] != SUBSCRIPTION_ID
    assert evidence["subscription_id"].startswith(SUBSCRIPTION_ID[:4])
    assert not any(call[:1] == ("cognitiveservices",) for call in azure.calls)
    assert not any(call[:3] == ("ad", "app", "show") for call in azure.calls)
    assert not any(call[:2] == ("acr", "show") for call in azure.calls)


def test_foundation_apply_verifies_least_privilege_scopes() -> None:
    """Reject a lingering subscription Contributor during foundation apply."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(
        configuration,
        foundation_exists=True,
        lingering_subscription_contributor=True,
    )

    with pytest.raises(PreflightError, match="Contributor only at the approved scope"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


def test_foundation_apply_rejects_forbidden_owner_role() -> None:
    """Reject an Owner or RBAC role on the foundation apply identity."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(
        configuration,
        foundation_exists=True,
        forbidden_deployment_role=True,
    )

    with pytest.raises(PreflightError, match="forbidden Owner"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    "phase",
    [
        "foundation-plan",
        "foundation-apply",
        "foundation",
        "production-foundation",
        "publish",
        "artifacts",
        "rollout",
    ],
)
def test_legacy_subject_fails_before_role_checks_in_every_phase(
    phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_factory = {
        "foundation-plan": foundation_plan_environment,
        "foundation-apply": foundation_apply_environment,
    }.get(phase, disabled_environment)
    environment = environment_factory()
    configuration = load_configuration(environment, phase=phase)
    azure = FakeAzure(configuration, foundation_exists=True)
    original_json = azure.json
    azure.access_token = synthetic_access_token(
        tenant_id=configuration.tenant_id,
        client_id=configuration.deployment_client_id,
        principal_id=azure.deployment_principal_id,
    )

    def legacy_json(*arguments: str, allow_missing: bool = False) -> Any:
        value = original_json(*arguments, allow_missing=allow_missing)
        if arguments[:3] == ("identity", "federated-credential", "list"):
            value[0]["subject"] = "repo:sekharrcs/optima:environment:hackathon"
        return value

    monkeypatch.setattr(azure, "json", legacy_json)
    with pytest.raises(PreflightError, match="federated credential"):
        run_preflight(
            configuration,
            azure,
            phase=phase,
            repository_root=ROOT,
        )
    assert not any(call[:2] == ("role", "assignment") for call in azure.calls)


def test_foundation_plan_still_requires_cost_governance() -> None:
    """Keep foundation resources gated on their genuinely required inputs."""
    environment = foundation_plan_environment()
    environment.pop("OPTIMA_EXPECTED_FIXED_MONTHLY_COST_INR")

    with pytest.raises(PreflightError, match="OPTIMA_EXPECTED_FIXED_MONTHLY_COST_INR"):
        load_configuration(environment, phase="foundation-plan")


@pytest.mark.parametrize(
    ("phase", "environment_factory"),
    [
        ("foundation-plan", foundation_plan_environment),
        ("foundation-apply", foundation_apply_environment),
        ("foundation", foundation_environment),
        ("production-foundation", disabled_environment),
        ("publish", disabled_environment),
    ],
)
def test_valid_plan_apply_publish_identity_matrices(
    phase: str,
    environment_factory: Any,
) -> None:
    """Accept only the reviewed effective role set for each deployment phase."""
    configuration = load_configuration(environment_factory(), phase=phase)
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.access_token = synthetic_access_token(
        tenant_id=configuration.tenant_id,
        client_id=configuration.deployment_client_id,
        principal_id=azure.deployment_principal_id,
    )

    evidence = run_preflight(
        configuration,
        azure,
        phase=phase,
        repository_root=ROOT,
    )

    assert evidence["phase"] == phase
    assert azure.access_token not in json.dumps(evidence)
    assert (
        "account",
        "get-access-token",
        "--resource-type",
        "arm",
        "--subscription",
        SUBSCRIPTION_ID,
    ) in azure.calls
    assert any(
        call[:6]
        == (
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            azure.deployment_principal_id,
            "--all",
        )
        and call[6:]
        == (
            "--fill-principal-name",
            "false",
        )
        for call in azure.calls
    )
    assert any(
        call[:6]
        == (
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            azure.deployment_principal_id,
            "--scope",
        )
        and call[6:]
        == (
            f"/subscriptions/{SUBSCRIPTION_ID}",
            "--include-inherited",
            "--fill-principal-name",
            "false",
        )
        for call in azure.calls
    )
    assert any(
        call[:3] == ("rest", "--method", "get")
        and call[call.index("--url") + 1].startswith(
            f"https://{MICROSOFT_GRAPH_HOST}/v1.0/servicePrincipals/"
            f"{azure.deployment_principal_id}/transitiveMemberOf/"
        )
        and "$count=true" in call[call.index("--url") + 1]
        and "ConsistencyLevel=eventual" in call
        and call[call.index("--resource") + 1] == "https://graph.microsoft.com/"
        for call in azure.calls
    )


def test_foundation_bootstrap_retains_only_subscription_contributor() -> None:
    """Preserve the temporary first-bootstrap apply contract while the RG is absent."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration)

    evidence = run_preflight(
        configuration,
        azure,
        phase="foundation",
        repository_root=ROOT,
    )

    assert evidence["phase"] == "foundation"


def test_separated_foundation_apply_requires_existing_resource_group() -> None:
    """Do not let the separated apply inherit production bootstrap permissions."""
    configuration = load_configuration(
        foundation_apply_environment(), phase="foundation-apply"
    )
    azure = FakeAzure(configuration)

    with pytest.raises(PreflightError, match="requires the target resource group"):
        run_preflight(
            configuration,
            azure,
            phase="foundation-apply",
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    ("claim", "value", "message"),
    [
        ("tid", "bbbbbbbb-cccc-dddd-eeee-ffffffffffff", "token tenant"),
        ("appid", "cccccccc-dddd-eeee-ffff-000000000000", "token client"),
        ("oid", CLIENT_ID, "token object"),
    ],
)
def test_authenticated_session_claims_must_match_configured_identity(
    claim: str,
    value: str,
    message: str,
) -> None:
    """Bind tenant, client, and principal claims to the selected apply identity."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    claims = {
        "appid": CLIENT_ID,
        "oid": DEPLOYMENT_PRINCIPAL_ID,
        "tid": TENANT_ID,
    }
    claims[claim] = value
    azure.access_token = synthetic_access_token(
        tenant_id=str(claims["tid"]),
        client_id=str(claims["appid"]),
        principal_id=str(claims["oid"]),
    )

    with pytest.raises(PreflightError, match=message):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


def test_authenticated_session_accepts_azp_client_claim() -> None:
    """Accept the documented azp claim when appid is absent."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.access_token = synthetic_access_token(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        principal_id=DEPLOYMENT_PRINCIPAL_ID,
        client_claim_name="azp",
    )

    run_preflight(
        configuration,
        azure,
        phase="foundation",
        repository_root=ROOT,
    )


@pytest.mark.parametrize("token", ["not-a-jwt", "a.%%%%.c", "a.e30.c"])
def test_authenticated_session_rejects_malformed_or_incomplete_token_claims(
    token: str,
) -> None:
    """Fail closed when ARM token claims cannot be decoded and verified."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.access_token = token

    with pytest.raises(PreflightError, match="token"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


@pytest.mark.parametrize("user_type", ["user", "managedIdentity", ""])
def test_authenticated_account_requires_service_principal_type(
    user_type: str,
) -> None:
    """Reject interactive or otherwise unbound Azure CLI sessions."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.account_user_type = user_type

    with pytest.raises(PreflightError, match="service-principal"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


def test_identity_resource_id_subscription_must_match_configuration() -> None:
    """Reject a syntactically valid identity from a different subscription."""
    environment = foundation_environment()
    environment["AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID"] = environment[
        "AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID"
    ].replace(SUBSCRIPTION_ID, "00000000-1111-2222-3333-444444444444")
    configuration = load_configuration(environment, phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)

    with pytest.raises(PreflightError, match="identity subscription"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    "invalid_identity_id",
    [
        (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-identity/providers/"
            "Microsoft.ManagedIdentity/systemAssignedIdentities/id-optima-deploy"
        ),
        (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-identity/providers/"
            "Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-deploy/child"
        ),
        (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg%2fidentity/providers/"
            "Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-deploy"
        ),
    ],
    ids=("wrong-resource-type", "extra-child", "encoded-separator"),
)
def test_identity_resource_id_requires_exact_user_assigned_identity_grammar(
    invalid_identity_id: str,
) -> None:
    """Reject lookalike or noncanonical managed-identity resource IDs."""
    environment = foundation_environment()
    environment["AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID"] = invalid_identity_id
    configuration = load_configuration(environment, phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)

    with pytest.raises(
        PreflightError,
        match="user-assigned managed identity|malformed ARM path segment",
    ):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


def test_returned_identity_id_must_match_configured_resource_id() -> None:
    """Reject lookup substitution to another identity with a matching client ID."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.returned_identity_id = configuration.deployment_identity_resource_id.replace(
        "id-optima-deploy", "id-optima-other"
    )

    with pytest.raises(PreflightError, match="identity ID does not match"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


def test_returned_identity_client_id_must_not_be_principal_id() -> None:
    """Keep application/client identity distinct from object/principal identity."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.returned_identity_client_id = DEPLOYMENT_PRINCIPAL_ID

    with pytest.raises(PreflightError, match="does not match client ID"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    "role_id",
    ["8e3af657-a8ff-443c-a75c-2fe8c4bcb635", CONTRIBUTOR_ROLE_ID],
    ids=("owner", "contributor"),
)
def test_inherited_management_group_broad_roles_are_rejected(role_id: str) -> None:
    """Reject effective mutation access inherited above the subscription."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.effective_assignments = [
        *apply_effective_assignments(),
        effective_assignment(
            role_id,
            "/providers/Microsoft.Management/managementGroups/tenant-root",
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
    ]

    with pytest.raises(PreflightError, match="approved|Contributor|Owner"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


def test_group_derived_assignment_is_rejected() -> None:
    """Reject a transitive group role absent from direct identity assignments."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.transitive_group_ids = [TRANSITIVE_GROUP_ID]
    azure.group_role_assignments[TRANSITIVE_GROUP_ID] = [
        effective_assignment(
            "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
            "/providers/Microsoft.Management/managementGroups/tenant-root",
            principal_id=TRANSITIVE_GROUP_ID,
            principal_type="Group",
        ),
    ]

    with pytest.raises(PreflightError, match="group-derived Azure role"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    "response",
    [
        {"value": []},
        {"@odata.count": 1, "value": []},
        {
            "@odata.count": 1,
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
            "value": [{"id": TRANSITIVE_GROUP_ID}],
        },
        {
            "@odata.count": 2,
            "value": [
                {"id": TRANSITIVE_GROUP_ID},
                {"id": TRANSITIVE_GROUP_ID},
            ],
        },
    ],
    ids=("missing-count", "count-mismatch", "paginated", "duplicate-group"),
)
def test_transitive_group_evidence_fails_closed(response: Any) -> None:
    """Reject incomplete or contradictory Microsoft Graph membership evidence."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.transitive_group_response = response

    with pytest.raises(PreflightError, match="transitive membership"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    "spoofed_id",
    [
        f"/providers/roles/{READER_ROLE_ID}",
        f"/providers/Microsoft.Authorization/roleDefinitions/{READER_ROLE_ID}-suffix",
        (
            f"/subscriptions/{SUBSCRIPTION_ID}/providers/"
            f"Microsoft.Authorization/notRoleDefinitions/{READER_ROLE_ID}"
        ),
    ],
)
def test_malformed_or_spoofed_role_definition_ids_are_rejected(
    spoofed_id: str,
) -> None:
    """Never infer a role from a suffix or an unapproved ARM resource type."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.effective_assignments = apply_effective_assignments()
    azure.effective_assignments[0]["roleDefinitionId"] = spoofed_id

    with pytest.raises(PreflightError, match="role definition"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


def test_duplicate_effective_assignments_are_rejected() -> None:
    """Reject contradictory duplicate direct or inherited assignment evidence."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    assignments = apply_effective_assignments()
    azure.effective_assignments = [*assignments, dict(assignments[0])]

    with pytest.raises(PreflightError, match="duplicate role assignments"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    "scope",
    [
        f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-optima-hackathon/",
        (f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-optima%2fhackathon"),
        f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups",
    ],
    ids=("trailing-slash", "encoded-separator", "missing-group-name"),
)
def test_malformed_effective_assignment_scopes_are_rejected(scope: str) -> None:
    """Require every effective assignment scope to be a canonical ARM scope."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.effective_assignments = apply_effective_assignments()
    azure.effective_assignments[1]["scope"] = scope

    with pytest.raises(PreflightError, match="scope is malformed"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


def test_apply_rejects_any_custom_or_extra_effective_role() -> None:
    """Prove apply cannot acquire role-assignment administration through custom RBAC."""
    configuration = load_configuration(foundation_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.effective_assignments = [
        *apply_effective_assignments(),
        effective_assignment(
            "dddddddd-eeee-ffff-0000-111111111111",
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-optima-hackathon",
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
    ]

    with pytest.raises(PreflightError, match="exactly the approved effective roles"):
        run_preflight(
            configuration,
            azure,
            phase="foundation",
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"condition": None, "conditionVersion": None},
        {"condition": None},
        {"conditionVersion": None},
    ],
    ids=("original", "observed-cli", "condition-only", "version-only"),
)
def test_foundation_plan_role_accepts_optional_null_condition_metadata(
    metadata: dict[str, Any],
) -> None:
    """Accept CLI null metadata without removing fields from its response."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    definition = foundation_plan_role_definition()
    definition["permissions"][0].update(metadata)
    azure.role_definition_response = [definition]
    original = json.dumps(definition, sort_keys=True)

    run_preflight(configuration, azure, phase="foundation-plan", repository_root=ROOT)

    assert json.dumps(definition, sort_keys=True) == original


@pytest.mark.parametrize("field", ["condition", "conditionVersion"])
@pytest.mark.parametrize("value", ["", "2.0", "@Resource[foo]", False, 0, [], {}])
def test_foundation_plan_role_rejects_nonnull_condition_metadata(
    field: str, value: Any
) -> None:
    """Reject even falsey non-null conditions and version values."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    definition = foundation_plan_role_definition()
    definition["permissions"][0].update({"condition": None, "conditionVersion": None})
    definition["permissions"][0][field] = value
    azure.role_definition_response = [definition]

    with pytest.raises(PreflightError, match="permissions are malformed"):
        run_preflight(
            configuration, azure, phase="foundation-plan", repository_root=ROOT
        )


@pytest.mark.parametrize("with_metadata", [False, True])
@pytest.mark.parametrize(
    "field", ["actions", "notActions", "dataActions", "notDataActions"]
)
@pytest.mark.parametrize(
    "value", [None, "read", {}, [None], [1], [""], [" "], ["*", "*"]]
)
def test_foundation_plan_role_rejects_malformed_permission_lists(
    with_metadata: bool, field: str, value: Any
) -> None:
    """Retain strict list, element and duplicate checks in both response shapes."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    definition = foundation_plan_role_definition()
    if with_metadata:
        definition["permissions"][0].update(
            {"condition": None, "conditionVersion": None}
        )
    definition["permissions"][0][field] = value
    azure.role_definition_response = [definition]

    with pytest.raises(PreflightError, match="permissions are malformed"):
        run_preflight(
            configuration, azure, phase="foundation-plan", repository_root=ROOT
        )


@pytest.mark.parametrize("with_metadata", [False, True])
@pytest.mark.parametrize(
    "field", ["actions", "notActions", "dataActions", "notDataActions"]
)
def test_foundation_plan_role_requires_all_original_permission_fields(
    with_metadata: bool, field: str
) -> None:
    """Optional metadata cannot replace any required permission list."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    definition = foundation_plan_role_definition()
    if with_metadata:
        definition["permissions"][0].update(
            {"condition": None, "conditionVersion": None}
        )
    del definition["permissions"][0][field]
    azure.role_definition_response = [definition]

    with pytest.raises(PreflightError, match="permissions are malformed"):
        run_preflight(
            configuration, azure, phase="foundation-plan", repository_root=ROOT
        )


@pytest.mark.parametrize("field", ["unknown", "Condition", "conditions"])
def test_foundation_plan_role_rejects_unknown_permission_fields(field: str) -> None:
    """Do not strip arbitrary null fields or silently normalize key names."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    definition = foundation_plan_role_definition()
    definition["permissions"][0].update(
        {"condition": None, "conditionVersion": None, field: None}
    )
    azure.role_definition_response = [definition]

    with pytest.raises(PreflightError, match="permissions are malformed"):
        run_preflight(
            configuration, azure, phase="foundation-plan", repository_root=ROOT
        )


@pytest.mark.parametrize("permissions", [None, {}, [], [None], [{}, {}]])
def test_foundation_plan_role_rejects_malformed_permission_blocks(
    permissions: Any,
) -> None:
    """Require exactly one structured permission block."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    definition = foundation_plan_role_definition()
    definition["permissions"] = permissions
    azure.role_definition_response = [definition]

    with pytest.raises(PreflightError, match="permissions are malformed"):
        run_preflight(
            configuration, azure, phase="foundation-plan", repository_root=ROOT
        )


@pytest.mark.parametrize("with_metadata", [False, True])
@pytest.mark.parametrize(
    "actions",
    [
        [],
        ["Microsoft.Authorization/roleAssignments/write"],
        ["Microsoft.Authorization/*"],
        ["*"],
        [
            "Microsoft.Resources/deployments/whatIf/action",
            "Microsoft.Authorization/roleAssignments/write",
        ],
    ],
    ids=(
        "empty",
        "exact-role-write",
        "authorization-wildcard",
        "global-wildcard",
        "extra",
    ),
)
def test_foundation_plan_role_rejects_mutating_or_extra_actions(
    with_metadata: bool,
    actions: list[str],
) -> None:
    """Keep the plan identity structurally unable to mutate resources or RBAC."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    definition = foundation_plan_role_definition(actions=actions)
    if with_metadata:
        definition["permissions"][0].update(
            {"condition": None, "conditionVersion": None}
        )
    azure.role_definition_response = [definition]

    with pytest.raises(PreflightError, match="allow only deployments what-if"):
        run_preflight(
            configuration,
            azure,
            phase="foundation-plan",
            repository_root=ROOT,
        )


@pytest.mark.parametrize("with_metadata", [False, True])
@pytest.mark.parametrize(
    ("field", "action"),
    [
        ("dataActions", "Microsoft.Storage/storageAccounts/blobServices/read"),
        ("notDataActions", "Microsoft.Storage/*"),
        ("notActions", "Microsoft.Authorization/roleAssignments/write"),
    ],
)
def test_foundation_plan_role_rejects_nonempty_secondary_permissions(
    with_metadata: bool,
    field: str,
    action: str,
) -> None:
    """Require a closed single-action control-plane permission contract."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    definition = foundation_plan_role_definition()
    definition["permissions"][0][field] = [action]
    if with_metadata:
        definition["permissions"][0].update(
            {"condition": None, "conditionVersion": None}
        )
    azure.role_definition_response = [definition]

    with pytest.raises(PreflightError, match="allow only deployments what-if"):
        run_preflight(
            configuration,
            azure,
            phase="foundation-plan",
            repository_root=ROOT,
        )


@pytest.mark.parametrize("response", [None, [], {}, [{"roleType": "CustomRole"}]])
def test_foundation_plan_role_definition_fails_closed_when_unreadable_or_malformed(
    response: Any,
) -> None:
    """Do not continue when the custom role definition cannot be proven."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.role_definition_response = response if response is not None else [None]

    with pytest.raises(PreflightError, match="role definition"):
        run_preflight(
            configuration,
            azure,
            phase="foundation-plan",
            repository_root=ROOT,
        )


@pytest.mark.parametrize("with_metadata", [False, True])
@pytest.mark.parametrize(
    "scopes",
    [
        [],
        [f"/subscriptions/{SUBSCRIPTION_ID}"],
        [f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-other"],
        [
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-optima-hackathon",
            f"/subscriptions/{SUBSCRIPTION_ID}",
        ],
        [
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-optima-hackathon",
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-other",
        ],
        [
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-optima-hackathon",
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-optima-hackathon",
        ],
    ],
    ids=(
        "empty",
        "subscription",
        "other-group",
        "expanded",
        "extra-group",
        "duplicate",
    ),
)
def test_foundation_plan_role_requires_usable_assignable_scope(
    with_metadata: bool, scopes: list[str]
) -> None:
    """Require only the reviewed target group, not a containing or extra scope."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    definition = foundation_plan_role_definition(assignable_scopes=scopes)
    if with_metadata:
        definition["permissions"][0].update(
            {"condition": None, "conditionVersion": None}
        )
    azure.role_definition_response = [definition]

    with pytest.raises(PreflightError, match="not assignable"):
        run_preflight(
            configuration,
            azure,
            phase="foundation-plan",
            repository_root=ROOT,
        )


def test_foundation_plan_rejects_any_extra_effective_assignment() -> None:
    """Allow only subscription Reader and target-RG what-if access for planning."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.effective_assignments = [
        *plan_effective_assignments(),
        effective_assignment(
            CONTRIBUTOR_ROLE_ID,
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-optima-hackathon",
            principal_id=FOUNDATION_PLAN_PRINCIPAL_ID,
        ),
    ]

    with pytest.raises(PreflightError, match="Contributor only at the approved scope"):
        run_preflight(
            configuration,
            azure,
            phase="foundation-plan",
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    "missing",
    [
        "AZURE_CONTAINER_REGISTRY_NAME",
        "AZURE_OPENAI_RESOURCE_ID",
        "OPTIMA_FOUNDRY_BASE_URL",
        "OPTIMA_UI_AUTH_CLIENT_ID",
        UI_AUTH_CLIENT_SECRET_ENV,
        "OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS",
    ],
)
def test_enabled_phases_still_require_runtime_inputs(missing: str) -> None:
    """Keep every runtime-composition input mandatory once its phase runs."""
    environment = disabled_environment()
    environment.pop(missing)

    with pytest.raises(PreflightError):
        load_configuration(environment, phase="rollout")


@pytest.mark.parametrize("phase", ["publish", "artifacts", "rollout"])
def test_runtime_phases_load_the_complete_runtime_contract(phase: str) -> None:
    """Keep all non-foundation phases on the strict runtime contract."""
    configuration = load_configuration(disabled_environment(), phase=phase)

    assert configuration.registry_name == APPROVED_REGISTRY_NAME
    assert configuration.pricing is not None
    assert {binding.role for binding in configuration.models} == {
        "SMALL",
        "STRONG",
        "JUDGE",
    }


def test_default_configuration_phase_remains_strict_rollout() -> None:
    """Preserve rollout as the strict default for every existing caller."""
    environment = disabled_environment()

    assert load_configuration(environment) == load_configuration(
        environment,
        phase="rollout",
    )


@pytest.mark.parametrize(
    "supplied",
    [
        "AZURE_CONTAINER_REGISTRY_NAME",
        "AZURE_OPENAI_RESOURCE_ID",
        "OPTIMA_FOUNDRY_BASE_URL",
        "OPTIMA_UI_AUTH_CLIENT_ID",
        "OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS",
    ],
)
def test_foundation_plan_ignores_absent_runtime_inputs(supplied: str) -> None:
    """Never require a deferred runtime input during foundation planning."""
    environment = foundation_plan_environment()
    environment.pop(supplied, None)

    configuration = load_configuration(environment, phase="foundation-plan")

    assert configuration.pricing is None


def test_unsupported_preflight_phase_is_rejected() -> None:
    """Reject an unknown preflight phase instead of inferring a contract."""
    with pytest.raises(PreflightError, match="Unsupported preflight phase"):
        load_configuration(foundation_environment(), phase="bootstrap")


PRODUCTION_SUBSCRIPTION_SCOPE = f"/subscriptions/{SUBSCRIPTION_ID}"
PRODUCTION_RESOURCE_GROUP_SCOPE = (
    f"{PRODUCTION_SUBSCRIPTION_SCOPE}/resourceGroups/rg-optima-hackathon"
)
PRODUCTION_REGISTRY_SCOPE = (
    f"{PRODUCTION_RESOURCE_GROUP_SCOPE}/providers/Microsoft.ContainerRegistry/"
    f"registries/{APPROVED_REGISTRY_NAME}"
)


def production_effective_assignments() -> list[dict[str, str]]:
    """Return the exact approved production-foundation deployer three-role set."""
    return [
        effective_assignment(
            READER_ROLE_ID,
            PRODUCTION_SUBSCRIPTION_SCOPE,
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
        effective_assignment(
            CONTRIBUTOR_ROLE_ID,
            PRODUCTION_RESOURCE_GROUP_SCOPE,
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
        effective_assignment(
            ACR_PUSH_ROLE_ID,
            PRODUCTION_REGISTRY_SCOPE,
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
    ]


def _authenticated_production_azure(azure: FakeAzure) -> None:
    azure.access_token = synthetic_access_token(
        tenant_id=azure.configuration.tenant_id,
        client_id=azure.configuration.deployment_client_id,
        principal_id=azure.deployment_principal_id,
    )


def _production_foundation_configuration() -> DeploymentConfiguration:
    """Load the real cache-disabled production-foundation configuration."""
    return load_configuration(disabled_environment(), phase="production-foundation")


def _foundation_whatif_document(
    *,
    registry_name: str = APPROVED_REGISTRY_NAME,
    subscription_id: str = SUBSCRIPTION_ID,
    resource_group: str = PRODUCTION_RESOURCE_GROUP,
    environment: str = "hackathon",
    change_type: str = "NoChange",
) -> dict[str, Any]:
    """Build a canonical ten-resource foundation what-if.

    Production requires an already applied, converged foundation, so the default
    change type is NoChange. Passing ``change_type="Create"`` builds the
    unapplied ten-Create form that the convergence gate must reject.
    """
    suffix = registry_name[len("acroptima") :]
    cosmos = f"cosmos-optima-{suffix}"
    base = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers"
    resource_ids = [
        f"{base}/Microsoft.ManagedIdentity/userAssignedIdentities/"
        f"id-optima-api-{environment}",
        f"{base}/Microsoft.ManagedIdentity/userAssignedIdentities/"
        f"id-optima-ui-{environment}",
        f"{base}/Microsoft.ContainerRegistry/registries/{registry_name}",
        f"{base}/Microsoft.OperationalInsights/workspaces/law-optima-{environment}",
        f"{base}/Microsoft.Insights/components/appi-optima-{environment}",
        f"{base}/Microsoft.DocumentDB/databaseAccounts/{cosmos}",
        f"{base}/Microsoft.DocumentDB/databaseAccounts/{cosmos}/sqlDatabases/optima",
        f"{base}/Microsoft.DocumentDB/databaseAccounts/{cosmos}/sqlDatabases/optima/"
        "containers/runs",
        f"{base}/Microsoft.App/managedEnvironments/cae-optima-{environment}",
        f"{base}/Microsoft.Insights/actionGroups/application insights smart detection",
    ]
    return {
        "status": "Succeeded",
        "changes": [
            {"changeType": change_type, "resourceId": resource_id}
            for resource_id in resource_ids
        ],
    }


def _effective_parameter_document(
    *,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the canonical runtime parameter artifact used by production."""
    values = disabled_environment() if environment is None else dict(environment)
    values.update(
        {
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_ID": "123456789",
            "GITHUB_SHA": COMMIT_SHA,
        }
    )
    parameter_names = (
        set(production_parameters._REQUIRED_ENVIRONMENT_PARAMETERS)
        | set(production_parameters._OPTIONAL_ENVIRONMENT_PARAMETERS)
        | set(production_parameters._CACHE_ENVIRONMENT_PARAMETERS)
        | {
            "deploymentCommitSha",
            "deploymentWorkflowRunId",
            "deployContainerApps",
            "deployRuntimeAccess",
            "environmentName",
            "exposePublicUi",
            "productionEvaluatorMode",
            "redisEmbeddingDimension",
            "semanticCacheEnabled",
        }
    )
    compiled = {
        "$schema": production_parameters.DEPLOYMENT_PARAMETERS_SCHEMA,
        "contentVersion": "1.0.0.0",
        "parameters": {name: {"value": None} for name in parameter_names},
    }
    return production_parameters.build_effective_document(compiled, values)


def _parsed_effective_parameters(document: dict[str, Any]) -> dict[str, Any]:
    return classifier.parse_foundation_parameters(
        production_parameters.canonical_parameter_bytes(document),
        label="production-foundation.parameters.json",
        template_file="infra/resource-group.bicep",
        parameter_source_file="infra/environments/hackathon.runtime.bicepparam",
        resource_group=PRODUCTION_RESOURCE_GROUP,
    )


def _classified_evidence_document(
    *,
    registry_name: str = APPROVED_REGISTRY_NAME,
    commit_sha: str = COMMIT_SHA,
    subscription_id: str = SUBSCRIPTION_ID,
    resource_group: str = PRODUCTION_RESOURCE_GROUP,
    environment: str = "hackathon",
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return sanitized APPROVED evidence for a fresh, source-bound what-if."""
    whatif = document or _foundation_whatif_document(
        registry_name=registry_name,
        subscription_id=subscription_id,
        resource_group=resource_group,
        environment=environment,
    )
    parameter_document = _effective_parameter_document()
    parameter_document["parameters"]["environmentName"] = {"value": environment}
    parameter_document["parameters"]["deploymentCommitSha"] = {"value": commit_sha}
    parameters = classifier.parse_foundation_parameters(
        production_parameters.canonical_parameter_bytes(parameter_document),
        label="production-foundation.parameters.json",
        template_file="infra/resource-group.bicep",
        parameter_source_file="infra/environments/hackathon.runtime.bicepparam",
        resource_group=resource_group,
    )
    return classifier.build_evidence_from_whatif(
        whatif,
        parameters=parameters,
        source_root=ROOT,
        subscription_id=subscription_id,
        resource_group=resource_group,
        commit_sha=commit_sha,
        deployment_mode="Incremental",
        external_policy=None,
    )


def _write_evidence(evidence: dict[str, Any]) -> Path:
    """Persist evidence to an isolated temporary file for preflight consumption."""
    directory = Path(tempfile.mkdtemp(prefix="optima-preflight-evidence-"))
    path = directory / "foundation-plan-evidence.json"
    path.write_bytes(classifier.serialize_evidence(evidence))
    return path


_VALID_EVIDENCE_PATH = _write_evidence(_classified_evidence_document())
_VALID_RAW_WHATIF_PATH = _VALID_EVIDENCE_PATH.with_name("foundation-whatif.json")
_VALID_RAW_WHATIF_PATH.write_bytes(
    json.dumps(
        _foundation_whatif_document(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
)
_VALID_PARAMETERS_PATH = _VALID_EVIDENCE_PATH.with_name(
    "production-foundation.parameters.json"
)
_VALID_PARAMETERS_PATH.write_bytes(
    production_parameters.canonical_parameter_bytes(_effective_parameter_document())
)


def _sha256(path: Path) -> str:
    if not path.exists():
        return "0" * 64
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_production_foundation(
    configuration: DeploymentConfiguration,
    azure: FakeAzure,
    *,
    phase: str = "production-foundation",
    classified_evidence: Path | None = None,
    raw_whatif: Path | None = None,
    effective_parameters: Path | None = None,
    expected_commit_sha: str = COMMIT_SHA,
) -> dict[str, Any]:
    """Run a three-role phase with same-job classified foundation evidence."""
    return run_preflight(
        configuration,
        azure,
        phase=phase,
        repository_root=ROOT,
        classified_evidence=(classified_evidence or _VALID_EVIDENCE_PATH),
        classified_evidence_sha256=_sha256(classified_evidence or _VALID_EVIDENCE_PATH),
        raw_whatif=(raw_whatif or _VALID_RAW_WHATIF_PATH),
        raw_whatif_sha256=_sha256(raw_whatif or _VALID_RAW_WHATIF_PATH),
        effective_parameters=(effective_parameters or _VALID_PARAMETERS_PATH),
        effective_parameters_sha256=_sha256(
            effective_parameters or _VALID_PARAMETERS_PATH
        ),
        expected_commit_sha=expected_commit_sha,
    )


@pytest.fixture(autouse=True)
def _clear_external_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the external-observation policy variable unset for evidence binding."""
    monkeypatch.delenv("OPTIMA_FOUNDATION_EXTERNAL_OBSERVATION_POLICY", raising=False)


@pytest.fixture(autouse=True)
def _stub_repository_registry_identity(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default three-role tests to the approved repository-derived registry name.

    The four-way classified-evidence binding is exercised by the dedicated tests
    marked ``real_registry_identity``. Role-contract and inventory-binding tests
    stub the repository-derived identity to the approved name so they reach the
    intended validation layer without constructing full classified evidence.
    """
    if "real_registry_identity" in request.keywords:
        return
    monkeypatch.setattr(
        azure_preflight,
        "_resolve_repository_registry_identity",
        lambda *arguments, **keywords: APPROVED_REGISTRY_NAME,
    )


def test_ordinary_foundation_accepts_reader_and_contributor_only() -> None:
    """Ordinary foundation still passes with exactly Reader and Contributor."""
    configuration = load_configuration(disabled_environment(), phase="foundation")
    assert configuration.registry_name is None
    azure = FakeAzure(configuration, foundation_exists=True, acr_push=False)
    _authenticated_production_azure(azure)

    evidence = run_preflight(
        configuration, azure, phase="foundation", repository_root=ROOT
    )

    assert evidence["phase"] == "foundation"


def test_ordinary_foundation_rejects_added_acr_push() -> None:
    """Ordinary foundation rejects AcrPush exactly as production run 35231953191."""
    configuration = load_configuration(disabled_environment(), phase="foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.effective_assignments = production_effective_assignments()

    with pytest.raises(PreflightError, match="exactly the approved effective roles"):
        run_preflight(configuration, azure, phase="foundation", repository_root=ROOT)


def test_production_foundation_loads_runtime_identity_with_cache_disabled() -> None:
    """Cache-disabled production-foundation loads the full runtime contract.

    Blocking-review regression for run 35231953191: the real production setting
    keeps semantic cache disabled, so configuration must load the approved ACR
    name for the exact role contract without enabling runtime composition. On the
    pre-fix head this failed with "Container registry name is unavailable".
    """
    environment = disabled_environment()
    assert environment["OPTIMA_SEMANTIC_CACHE_ENABLED"] == "false"
    configuration = load_configuration(environment, phase="production-foundation")

    assert configuration.registry_name == APPROVED_REGISTRY_NAME
    assert configuration.semantic_cache_enabled is False
    assert {binding.role for binding in configuration.models} == {
        "JUDGE",
        "SMALL",
        "STRONG",
    }
    assert configuration.openai_resource_id == OPENAI_RESOURCE_ID
    assert configuration.pricing is not None
    assert configuration.embedding_dimension is None

    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    evidence = run_preflight(
        configuration, azure, phase="production-foundation", repository_root=ROOT
    )

    assert evidence["phase"] == "production-foundation"
    # no Redis or embedding queries are issued for the ACR-only identity load
    assert not any("Microsoft.Cache" in " ".join(call) for call in azure.calls)


def test_production_session_validates_identity_without_acr_or_graph() -> None:
    """The immediate post-login gate proves session identity before what-if."""
    configuration = load_configuration(
        disabled_environment(), phase="production-session"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)

    evidence = run_preflight(
        configuration, azure, phase="production-session", repository_root=ROOT
    )

    assert evidence["phase"] == "production-session"
    assert evidence["checks"] == ["account", "github_oidc_federation"]
    assert not any(
        call[:1] in {("acr",), ("provider",), ("role",)} for call in azure.calls
    )
    assert not any("graph.microsoft.com" in " ".join(call) for call in azure.calls)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("session_tenant_id", "99999999-0000-1111-2222-333333333333", "tenant"),
        ("session_client_id", "99999999-0000-1111-2222-333333333333", "client"),
        ("session_principal_id", "99999999-0000-1111-2222-333333333333", "object"),
    ],
)
def test_production_session_rejects_wrong_token_identity(
    field: str, value: str, message: str
) -> None:
    """Tenant, client, and principal drift fail before the foundation what-if."""
    configuration = load_configuration(
        disabled_environment(), phase="production-session"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    setattr(azure, field, value)

    with pytest.raises(PreflightError, match=message):
        run_preflight(
            configuration, azure, phase="production-session", repository_root=ROOT
        )


def test_production_session_rejects_wrong_subscription() -> None:
    """The Azure CLI account must remain on the protected subscription."""
    configuration = load_configuration(
        disabled_environment(), phase="production-session"
    )
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.configuration = replace(
        configuration,
        subscription_id="99999999-0000-1111-2222-333333333333",
    )

    with pytest.raises(PreflightError, match="subscription"):
        run_preflight(
            configuration, azure, phase="production-session", repository_root=ROOT
        )


def test_production_foundation_accepts_exact_three_role_contract() -> None:
    """Production-foundation accepts exactly Reader, Contributor, and AcrPush@ACR."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)

    evidence = run_preflight(
        configuration, azure, phase="production-foundation", repository_root=ROOT
    )

    assert evidence["phase"] == "production-foundation"


def test_production_foundation_requires_acr_name() -> None:
    """A missing ACR name fails configuration closed even with cache disabled."""
    environment = disabled_environment()
    environment.pop("AZURE_CONTAINER_REGISTRY_NAME")

    with pytest.raises(
        PreflightError, match="AZURE_CONTAINER_REGISTRY_NAME is missing"
    ):
        load_configuration(environment, phase="production-foundation")


def test_production_foundation_rejects_blank_acr_name() -> None:
    """A whitespace-only ACR name fails closed before trimming."""
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = "   "

    with pytest.raises(PreflightError, match="contains whitespace"):
        load_configuration(environment, phase="production-foundation")


def test_production_foundation_rejects_placeholder_acr_name() -> None:
    """A placeholder ACR name fails configuration closed."""
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = "placeholder"

    with pytest.raises(PreflightError, match="is a placeholder"):
        load_configuration(environment, phase="production-foundation")


def test_production_foundation_requires_acr_push() -> None:
    """Missing AcrPush fails the production-foundation contract closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True, acr_push=False)

    with pytest.raises(PreflightError, match="lacks AcrPush"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_acr_push_on_another_registry() -> None:
    """AcrPush on a different ACR does not satisfy the approved binding."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    other_registry = (
        f"{PRODUCTION_RESOURCE_GROUP_SCOPE}/providers/Microsoft.ContainerRegistry/"
        "registries/acrimposter000000"
    )
    base = production_effective_assignments()
    azure.effective_assignments = [
        base[0],
        base[1],
        effective_assignment(
            ACR_PUSH_ROLE_ID, other_registry, principal_id=DEPLOYMENT_PRINCIPAL_ID
        ),
    ]

    with pytest.raises(PreflightError, match="lacks AcrPush"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_acr_push_at_broader_scope() -> None:
    """AcrPush at the resource-group scope does not satisfy the ACR binding."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    base = production_effective_assignments()
    azure.effective_assignments = [
        base[0],
        base[1],
        effective_assignment(
            ACR_PUSH_ROLE_ID,
            PRODUCTION_RESOURCE_GROUP_SCOPE,
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
    ]

    with pytest.raises(PreflightError, match="lacks AcrPush"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_acr_pull_instead_of_push() -> None:
    """AcrPull at the approved ACR does not satisfy the AcrPush requirement."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    base = production_effective_assignments()
    azure.effective_assignments = [
        base[0],
        base[1],
        effective_assignment(
            ACR_PULL_ROLE_ID,
            PRODUCTION_REGISTRY_SCOPE,
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
    ]

    with pytest.raises(PreflightError, match="lacks AcrPush"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_duplicate_acr_push() -> None:
    """A duplicated AcrPush assignment fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.effective_assignments = [
        *production_effective_assignments(),
        effective_assignment(
            ACR_PUSH_ROLE_ID,
            PRODUCTION_REGISTRY_SCOPE,
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
    ]

    with pytest.raises(PreflightError, match="duplicate role assignments"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_forbidden_owner_role() -> None:
    """An added Owner assignment is rejected as a forbidden role."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(
        configuration, foundation_exists=True, forbidden_deployment_role=True
    )

    with pytest.raises(PreflightError, match="forbidden Owner"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_lingering_subscription_contributor() -> None:
    """A broader Contributor assignment fails the exact-scope contract."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(
        configuration,
        foundation_exists=True,
        lingering_subscription_contributor=True,
    )

    with pytest.raises(PreflightError, match="Contributor only"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_extra_unrelated_role() -> None:
    """Any additional unrelated role breaks the exact three-role set."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.effective_assignments = [
        *production_effective_assignments(),
        effective_assignment(
            OPENAI_USER_ROLE_ID,
            PRODUCTION_RESOURCE_GROUP_SCOPE,
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
        ),
    ]

    with pytest.raises(PreflightError, match="exactly the approved effective roles"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_group_derived_acr_push() -> None:
    """Group-derived access cannot silently satisfy the AcrPush requirement."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    base = production_effective_assignments()
    azure.effective_assignments = [
        base[0],
        base[1],
        effective_assignment(
            ACR_PUSH_ROLE_ID,
            PRODUCTION_REGISTRY_SCOPE,
            principal_id=DEPLOYMENT_PRINCIPAL_ID,
            principal_type="Group",
        ),
    ]

    with pytest.raises(PreflightError, match="group-derived role"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_wrong_deployer_principal() -> None:
    """An assignment for another principal fails closed before role acceptance."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    base = production_effective_assignments()
    azure.effective_assignments = [
        base[0],
        base[1],
        effective_assignment(
            ACR_PUSH_ROLE_ID,
            PRODUCTION_REGISTRY_SCOPE,
            principal_id="99999999-0000-1111-2222-333333333333",
        ),
    ]

    with pytest.raises(PreflightError, match="principal is malformed"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_is_runtime_composition() -> None:
    """Production-foundation validates the exact runtime-composition inputs."""
    from scripts.azure_preflight import (
        ACR_ROLE_IDENTITY_PHASES,
        RUNTIME_COMPOSITION_PHASES,
    )

    assert "production-foundation" in RUNTIME_COMPOSITION_PHASES
    assert "production-foundation" in ACR_ROLE_IDENTITY_PHASES
    configuration = _production_foundation_configuration()
    assert configuration.models
    assert configuration.openai_resource_id == OPENAI_RESOURCE_ID
    assert configuration.ui_auth_client_id == UI_AUTH_CLIENT_ID
    assert configuration.pricing is not None
    assert configuration.embedding_dimension is None


def test_deploy_production_selects_explicit_production_foundation_context() -> None:
    """The production workflow requests the closed production-foundation context.

    PyYAML is not a project dependency, so an independent structural YAML parse
    would broaden scope; this exact-set regex assertion over ``--phase`` tokens
    is retained as the structural contract for each workflow.
    """
    deploy = (ROOT / ".github" / "workflows" / "deploy-production.yml").read_text(
        encoding="utf-8"
    )
    phases = re.findall(r"--phase (\S+)", deploy)
    assert set(phases) == {
        "production-session",
        "production-foundation",
        "publish",
        "artifacts",
        "rollout",
    }
    assert "foundation" not in phases
    assert deploy.count("id-token: write") == 1
    assert deploy.index("--phase production-foundation") < deploy.index(
        "Deploy the exactly authorized runtime foundation"
    )
    assert deploy.index("--phase production-foundation") < deploy.index(
        "Push the exact verified images"
    )

    foundation = (ROOT / ".github" / "workflows" / "foundation.yml").read_text(
        encoding="utf-8"
    )
    assert set(re.findall(r"--phase (\S+)", foundation)) == {
        "foundation-plan",
        "foundation-apply",
    }
    assert "production-foundation" not in foundation


# --- Second independent review: strict ACR-name grammar --------------------


@pytest.mark.parametrize("registry_name", ["acr12", "a" * 50, "ACR12345", "0AaZz99999"])
def test_valid_acr_names_load_at_the_grammar_boundaries(registry_name: str) -> None:
    """Exactly 5-50 ASCII alphanumeric characters is accepted at configuration load."""
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = registry_name

    configuration = load_configuration(environment, phase="production-foundation")

    assert configuration.registry_name == registry_name


@pytest.mark.parametrize(
    "registry_name",
    [
        "acr1",  # four characters is below the minimum length
        "a" * 51,  # fifty-one characters is above the maximum length
        "bad_name",  # underscore is not ASCII alphanumeric
        "bad-name",  # hyphen is not ASCII alphanumeric
        "bad.name",  # period is not ASCII alphanumeric
        "bad/name",  # forward slash is not ASCII alphanumeric
        "bad\\name",  # backslash is not ASCII alphanumeric
        "bad%20name",  # percent-encoding is not ASCII alphanumeric
        "acr\u00f6ptima",  # non-ASCII Unicode is rejected
    ],
)
def test_malformed_acr_names_fail_configuration_load(registry_name: str) -> None:
    """Malformed ACR names fail load rather than reaching ARM-segment validation.

    Second-review regression: on the pre-fix head ``bad_name`` passed the generic
    ``_required`` check and only failed deep inside scope canonicalization.
    """
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = registry_name

    with pytest.raises(PreflightError, match="5-50 ASCII alphanumeric"):
        load_configuration(environment, phase="production-foundation")


@pytest.mark.parametrize("registry_name", ["bad\tname", "bad\nname"])
def test_control_character_acr_names_fail_configuration_load(
    registry_name: str,
) -> None:
    """Tab and newline ACR names fail load before reaching the preflight."""
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = registry_name

    with pytest.raises(PreflightError):
        load_configuration(environment, phase="production-foundation")


@pytest.mark.parametrize("phase", ["publish", "artifacts", "rollout"])
def test_runtime_phases_reject_malformed_acr_names(phase: str) -> None:
    """Every ACR-consuming phase enforces the shared registry-name grammar."""
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = "bad_name"

    with pytest.raises(PreflightError, match="5-50 ASCII alphanumeric"):
        load_configuration(environment, phase=phase)


# --- Second independent review: inventory-bound ACR scope ------------------


def test_fake_azure_acr_assignment_scope_is_independent_of_configuration() -> None:
    """The fixture no longer derives the AcrPush scope from the configured name.

    This proves the decoupling that the second review requires: configuration
    registry name, live inventory registry, and the granted role-assignment scope
    are three independent fixture inputs.
    """
    configuration = _production_foundation_configuration()
    assert configuration.registry_name == APPROVED_REGISTRY_NAME
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.acr_assignment_registry_name = "acrindependent01"

    granted = azure.json(
        "role",
        "assignment",
        "list",
        "--assignee-object-id",
        DEPLOYMENT_PRINCIPAL_ID,
        "--all",
    )
    acr_scopes = [
        assignment["scope"]
        for assignment in granted
        if assignment["roleDefinitionId"]
        == role_definition_resource_id(ACR_PUSH_ROLE_ID)
    ]

    assert acr_scopes == [
        f"{PRODUCTION_RESOURCE_GROUP_SCOPE}/providers/Microsoft.ContainerRegistry/"
        "registries/acrindependent01"
    ]
    assert configuration.registry_name not in acr_scopes[0]


def test_fake_azure_inventory_does_not_realign_default_acr_assignment() -> None:
    """Inventory and role scope remain independent without explicit overrides."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registry_name = ALTERNATE_REGISTRY_NAME

    granted = azure.json(
        "role",
        "assignment",
        "list",
        "--assignee-object-id",
        DEPLOYMENT_PRINCIPAL_ID,
        "--all",
    )
    acr_scopes = [
        assignment["scope"]
        for assignment in granted
        if assignment["roleDefinitionId"]
        == role_definition_resource_id(ACR_PUSH_ROLE_ID)
    ]

    assert acr_scopes == [PRODUCTION_REGISTRY_SCOPE]
    assert ALTERNATE_REGISTRY_NAME not in acr_scopes[0]


def test_production_foundation_binds_acr_scope_from_inventory_not_configuration() -> (
    None
):
    """A different valid configured registry cannot pass while inventory holds
    the approved original.

    Blocking second-review regression: on the pre-fix head the expected AcrPush
    scope was constructed from the configured name, so an imposter registry plus
    a matching role assignment passed even though inventory still contained the
    approved registry.
    """
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = "acrimposter00001"
    configuration = load_configuration(environment, phase="production-foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    # Live inventory still contains only the approved original registry.
    azure.inventory_registry_name = APPROVED_REGISTRY_NAME
    # The deployer even holds a matching AcrPush on the configured imposter.
    azure.acr_assignment_registry_name = "acrimposter00001"
    _authenticated_production_azure(azure)

    with pytest.raises(
        PreflightError, match="does not match the repository-derived registry identity"
    ):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_inventory_registry_name_mismatch() -> None:
    """A live registry whose name differs from configuration fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registry_name = "acrotherlive000001"
    azure.acr_assignment_registry_name = APPROVED_REGISTRY_NAME
    _authenticated_production_azure(azure)

    with pytest.raises(
        PreflightError, match="does not match the repository-derived registry identity"
    ):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_acr_push_scoped_off_the_live_registry() -> None:
    """AcrPush granted on an independent registry fails against the live binding."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    # Configuration and inventory both name the approved registry, but the
    # granted AcrPush assignment is scoped to an independent registry.
    azure.acr_assignment_registry_name = "acrindependent01"
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="lacks AcrPush"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_accepts_consistent_inventory_binding() -> None:
    """Configuration, inventory, and assignment all naming the approved ACR pass."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registry_name = APPROVED_REGISTRY_NAME
    azure.acr_assignment_registry_name = APPROVED_REGISTRY_NAME
    _authenticated_production_azure(azure)

    evidence = run_preflight(
        configuration, azure, phase="production-foundation", repository_root=ROOT
    )

    assert evidence["phase"] == "production-foundation"


def test_production_foundation_rejects_missing_registry_inventory() -> None:
    """An empty container-registry inventory fails the cardinality contract."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registries = []
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="exactly one Azure Container Registry"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_multiple_registry_inventory() -> None:
    """Two container registries in the group fail the cardinality contract."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registries = [
        {
            "id": PRODUCTION_REGISTRY_SCOPE,
            "name": APPROVED_REGISTRY_NAME,
            "type": "Microsoft.ContainerRegistry/registries",
            "resourceGroup": "rg-optima-hackathon",
            "provisioningState": "Succeeded",
        },
        {
            "id": (
                f"{PRODUCTION_RESOURCE_GROUP_SCOPE}/providers/"
                "Microsoft.ContainerRegistry/registries/acrsecond00000001"
            ),
            "name": "acrsecond00000001",
            "type": "Microsoft.ContainerRegistry/registries",
            "resourceGroup": "rg-optima-hackathon",
            "provisioningState": "Succeeded",
        },
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="exactly one Azure Container Registry"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_non_registry_inventory_resource() -> None:
    """A non-registry resource in the ACR inventory is rejected."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registry_type = "Microsoft.Storage/storageAccounts"
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="non-registry resource"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_unprovisioned_registry() -> None:
    """A registry that is not fully provisioned fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registry_provisioning_state = "Creating"
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="not fully provisioned"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_registry_id_in_another_group() -> None:
    """A live registry ID outside the approved group fails the identity binding.

    Even when configuration, inventory name, and assignment all point to the
    same alternative, an inventory resource ID anchored to a different resource
    group cannot match the independently constructed managed-resource identity.
    """
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registry_id = (
        f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-attacker/providers/"
        f"Microsoft.ContainerRegistry/registries/{APPROVED_REGISTRY_NAME}"
    )
    _authenticated_production_azure(azure)

    with pytest.raises(
        PreflightError, match="resource ID does not match the managed identity"
    ):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_malformed_registry_inventory() -> None:
    """A malformed inventory payload fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registries = "not-a-list"  # type: ignore[assignment]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="inventory response is malformed"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


# --- Second independent review: exact Microsoft Graph permission -----------


def test_production_foundation_accepts_exact_graph_application_read_all() -> None:
    """The default identity holds exactly Application.Read.All on Microsoft Graph."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)

    evidence = run_preflight(
        configuration, azure, phase="production-foundation", repository_root=ROOT
    )

    assert evidence["phase"] == "production-foundation"
    assert any("appRoleAssignments" in " ".join(call) for call in azure.calls)


def test_production_foundation_rejects_missing_graph_permission() -> None:
    """No Microsoft Graph application permission fails the exact contract."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = []
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="exactly the Application.Read.All"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_extra_graph_permission() -> None:
    """Application.Read.All plus another Graph permission fails the exact contract.

    Second-review regression: proving Graph access indirectly is insufficient;
    the identity must hold exactly one Microsoft Graph application permission.
    """
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(),
        graph_assignment(
            assignment_id=SECOND_GRAPH_ASSIGNMENT_ID,
            app_role_id="06da0dbc-49e2-44d2-8312-53f166ab848a",
        ),
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="exactly the Application.Read.All"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_duplicate_graph_permission() -> None:
    """A duplicated Application.Read.All grant fails the exact contract."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(),
        graph_assignment(assignment_id=SECOND_GRAPH_ASSIGNMENT_ID),
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="exactly the Application.Read.All"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


@pytest.mark.parametrize(
    "app_role_id",
    [
        "06da0dbc-49e2-44d2-8312-53f166ab848a",  # Directory.Read.All
        "1bfefb4e-e0b5-418b-a88f-73c46d2cc8e9",  # Application.ReadWrite.All
    ],
)
def test_production_foundation_rejects_wrong_graph_permission(
    app_role_id: str,
) -> None:
    """A single Graph permission that is not Application.Read.All fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [graph_assignment(app_role_id=app_role_id)]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="is not Application.Read.All"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_graph_permission_for_wrong_principal() -> None:
    """A Graph permission granted to another principal fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(principal_id="99999999-0000-1111-2222-333333333333")
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="targets another principal"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_permission_on_non_graph_service_principal() -> (
    None
):
    """Application.Read.All granted on a non-Graph resource is not accepted."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(resource_id="cccccccc-1111-2222-3333-444444444444")
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="exactly the Application.Read.All"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_disabled_graph_application_role() -> None:
    """A disabled Application.Read.All app role fails the enabled-permission check."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_service_principal_response = {
        "id": GRAPH_SERVICE_PRINCIPAL_ID,
        "appId": MICROSOFT_GRAPH_APP_ID,
        "appRoles": [
            {
                "id": GRAPH_APPLICATION_READ_ALL_ROLE_ID,
                "isEnabled": False,
                "allowedMemberTypes": ["Application"],
            }
        ],
    }
    _authenticated_production_azure(azure)

    with pytest.raises(
        PreflightError, match="not an enabled Microsoft Graph application"
    ):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_allows_unrelated_enterprise_app_permission() -> None:
    """Non-Graph enterprise-application permissions are outside this check."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(),
        graph_assignment(
            assignment_id=SECOND_GRAPH_ASSIGNMENT_ID,
            app_role_id="abcdef01-2345-6789-abcd-ef0123456789",
            resource_id="dddddddd-1111-2222-3333-444444444444",
        ),
    ]
    _authenticated_production_azure(azure)

    evidence = run_preflight(
        configuration, azure, phase="production-foundation", repository_root=ROOT
    )

    assert evidence["phase"] == "production-foundation"


def test_production_foundation_rejects_unpaginated_graph_permissions() -> None:
    """A paginated Graph app-role response fails closed rather than truncating."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments_response = {
        "@odata.count": 1,
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
        "value": [graph_assignment()],
    }
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="exceed the limit"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_malformed_graph_permissions() -> None:
    """A malformed Graph app-role response fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments_response = {"value": "not-a-list"}
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="app role assignments are malformed"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_unexpected_graph_service_principal() -> None:
    """A resolved Graph service principal with the wrong appId fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_service_principal_response = {
        "id": GRAPH_SERVICE_PRINCIPAL_ID,
        "appId": "11111111-0000-0000-c000-000000000000",
        "appRoles": [],
    }
    _authenticated_production_azure(azure)

    with pytest.raises(
        PreflightError, match="Microsoft Graph service principal is unexpected"
    ):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_foundation_plan_requires_exact_graph_permission() -> None:
    """The Graph permission contract applies to the foundation-plan identity too."""
    configuration = load_configuration(
        foundation_plan_environment(), phase="foundation-plan"
    )
    azure = FakeAzure(configuration)
    azure.graph_app_role_assignments = []
    azure.access_token = synthetic_access_token(
        tenant_id=configuration.tenant_id,
        client_id=configuration.deployment_client_id,
        principal_id=azure.deployment_principal_id,
    )

    with pytest.raises(PreflightError, match="exactly the Application.Read.All"):
        run_preflight(
            configuration, azure, phase="foundation-plan", repository_root=ROOT
        )


# --- Third independent review: four-way classified-evidence ACR binding -----


ALTERNATE_REGISTRY_NAME = "acroptimadeadbeef01234"


@pytest.mark.real_registry_identity
def test_production_foundation_binds_all_four_approved_sources() -> None:
    """The exact approved four-way binding passes end to end."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)

    evidence = _run_production_foundation(configuration, azure)

    assert evidence["phase"] == "production-foundation"


def _write_unconverged_foundation() -> tuple[Path, Path, Path]:
    """Persist matching evidence, raw what-if, and parameters for ten Creates."""
    whatif = _foundation_whatif_document(change_type="Create")
    evidence_path = _write_evidence(_classified_evidence_document(document=whatif))
    raw_path = evidence_path.with_name("foundation-whatif.json")
    raw_path.write_bytes(
        json.dumps(
            whatif, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
    )
    parameters_path = evidence_path.with_name("production-foundation.parameters.json")
    parameters_path.write_bytes(
        production_parameters.canonical_parameter_bytes(_effective_parameter_document())
    )
    return evidence_path, raw_path, parameters_path


@pytest.mark.real_registry_identity
def test_production_foundation_rejects_unconverged_ten_create_foundation() -> None:
    """Fresh evidence describing ten Creates proves an unapplied foundation."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    evidence_path, raw_path, parameters_path = _write_unconverged_foundation()

    with pytest.raises(PreflightError, match="converged foundation"):
        _run_production_foundation(
            configuration,
            azure,
            classified_evidence=evidence_path,
            raw_whatif=raw_path,
            effective_parameters=parameters_path,
        )


@pytest.mark.real_registry_identity
def test_production_foundation_accepts_converged_foundation() -> None:
    """The default converged evidence reports zero Creates and passes."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)

    evidence = _run_production_foundation(configuration, azure)

    assert evidence["phase"] == "production-foundation"


@pytest.mark.real_registry_identity
def test_self_consistent_alternate_registry_fails_against_evidence() -> None:
    """Config, inventory, and AcrPush agreeing on an alternate ACR still fails.

    The ARM-evaluated source identity from the classified what-if remains the
    approved registry, so a fully self-consistent alternative is rejected.
    """
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = ALTERNATE_REGISTRY_NAME
    configuration = load_configuration(environment, phase="production-foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registry_name = ALTERNATE_REGISTRY_NAME
    azure.acr_assignment_registry_name = ALTERNATE_REGISTRY_NAME
    _authenticated_production_azure(azure)

    with pytest.raises(
        PreflightError, match="does not match the repository-derived registry identity"
    ):
        _run_production_foundation(configuration, azure)


@pytest.mark.real_registry_identity
def test_all_four_alternate_sources_fail_against_fixed_raw_whatif() -> None:
    """Evidence, config, inventory, and role cannot replace the raw ARM result."""
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = ALTERNATE_REGISTRY_NAME
    configuration = load_configuration(environment, phase="production-foundation")
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.inventory_registry_name = ALTERNATE_REGISTRY_NAME
    azure.acr_assignment_registry_name = ALTERNATE_REGISTRY_NAME
    _authenticated_production_azure(azure)
    evidence = _write_evidence(
        _classified_evidence_document(registry_name=ALTERNATE_REGISTRY_NAME)
    )

    with pytest.raises(PreflightError, match="differs from raw what-if reconstruction"):
        _run_production_foundation(
            configuration,
            azure,
            classified_evidence=evidence,
            raw_whatif=_VALID_RAW_WHATIF_PATH,
        )


@pytest.mark.real_registry_identity
def test_alternate_registry_in_evidence_fails_against_approved_sources() -> None:
    """Alternate-registry evidence with three approved live sources fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    evidence = _write_evidence(
        _classified_evidence_document(registry_name=ALTERNATE_REGISTRY_NAME)
    )

    with pytest.raises(PreflightError, match="differs from raw what-if reconstruction"):
        _run_production_foundation(configuration, azure, classified_evidence=evidence)


@pytest.mark.real_registry_identity
def test_absent_evidence_argument_fails_closed() -> None:
    """Production-foundation without classified evidence fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="requires complete foundation evidence"):
        run_preflight(
            configuration,
            azure,
            phase="production-foundation",
            repository_root=ROOT,
            expected_commit_sha=COMMIT_SHA,
        )


@pytest.mark.real_registry_identity
def test_missing_evidence_file_fails_closed() -> None:
    """A missing classified-evidence file fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    missing = Path(tempfile.mkdtemp()) / "absent-evidence.json"

    with pytest.raises(PreflightError, match="missing, changed, or malformed"):
        _run_production_foundation(configuration, azure, classified_evidence=missing)


@pytest.mark.real_registry_identity
def test_wrong_commit_sha_evidence_fails() -> None:
    """Evidence for a different commit SHA fails the binding."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    evidence = _write_evidence(_classified_evidence_document(commit_sha="b" * 40))

    with pytest.raises(PreflightError, match="differs from raw what-if reconstruction"):
        _run_production_foundation(configuration, azure, classified_evidence=evidence)


@pytest.mark.real_registry_identity
def test_wrong_scope_evidence_fails() -> None:
    """Evidence for a different resource group fails the binding."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    evidence = _write_evidence(
        _classified_evidence_document(resource_group="rg-attacker")
    )

    with pytest.raises(PreflightError, match="differs from raw what-if reconstruction"):
        _run_production_foundation(configuration, azure, classified_evidence=evidence)


@pytest.mark.real_registry_identity
def test_tampered_parameter_fingerprint_evidence_fails() -> None:
    """Evidence whose parameter fingerprint is substituted fails the binding."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    document = _classified_evidence_document()
    document["parameters"]["fingerprint"] = "c" * 64
    evidence = _write_evidence(document)

    with pytest.raises(PreflightError, match="differs from raw what-if reconstruction"):
        _run_production_foundation(configuration, azure, classified_evidence=evidence)


@pytest.mark.real_registry_identity
def test_tampered_source_fingerprint_evidence_fails() -> None:
    """Evidence whose source fingerprint is substituted fails the binding."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    document = _classified_evidence_document()
    document["deployment_source"]["fingerprint"] = "c" * 64
    evidence = _write_evidence(document)

    with pytest.raises(PreflightError, match="differs from raw what-if reconstruction"):
        _run_production_foundation(configuration, azure, classified_evidence=evidence)


@pytest.mark.real_registry_identity
def test_changed_parameter_artifact_fails_against_captured_evidence() -> None:
    """Regenerating parameters after classification invalidates authorization."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    document = _effective_parameter_document()
    document["parameters"]["deploymentWorkflowRunId"] = {"value": "987654321-1"}
    directory = Path(tempfile.mkdtemp())
    parameters = directory / "changed.parameters.json"
    parameters.write_bytes(production_parameters.canonical_parameter_bytes(document))

    with pytest.raises(PreflightError, match="differs from raw what-if reconstruction"):
        _run_production_foundation(
            configuration,
            azure,
            effective_parameters=parameters,
        )


@pytest.mark.real_registry_identity
def test_unapproved_classification_evidence_fails() -> None:
    """Evidence that is not APPROVED fails closed as malformed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    document = _classified_evidence_document()
    document["classification"] = "REJECTED"
    evidence = _write_evidence(document)

    with pytest.raises(PreflightError, match="missing, changed, or malformed"):
        _run_production_foundation(configuration, azure, classified_evidence=evidence)


@pytest.mark.real_registry_identity
def test_wrong_type_registry_fact_evidence_fails() -> None:
    """Evidence whose container-registry fact has a wrong type fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    document = _classified_evidence_document()
    for fact in document["changes"]["resources"]:
        if fact["resource_role"] == "container_registry":
            fact["resource_type"] = "microsoft.storage/storageaccounts"
    evidence = _write_evidence(document)

    with pytest.raises(PreflightError, match="missing, changed, or malformed"):
        _run_production_foundation(configuration, azure, classified_evidence=evidence)


@pytest.mark.real_registry_identity
def test_duplicate_key_evidence_fails() -> None:
    """Evidence with duplicated JSON keys fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    directory = Path(tempfile.mkdtemp())
    evidence = directory / "duplicate-evidence.json"
    evidence.write_text(
        '{"schema_version": "x", "schema_version": "y"}\n', encoding="utf-8"
    )

    with pytest.raises(PreflightError, match="missing, changed, or malformed"):
        _run_production_foundation(configuration, azure, classified_evidence=evidence)


@pytest.mark.real_registry_identity
def test_symlink_evidence_fails() -> None:
    """A symlinked classified-evidence file fails closed."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    directory = Path(tempfile.mkdtemp())
    link = directory / "evidence-link.json"
    try:
        link.symlink_to(_VALID_EVIDENCE_PATH)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(PreflightError, match="missing, changed, or malformed"):
        _run_production_foundation(configuration, azure, classified_evidence=link)


@pytest.mark.real_registry_identity
def test_hard_linked_evidence_fails() -> None:
    """Evidence with another filesystem name fails the single-link contract."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    with tempfile.TemporaryDirectory() as directory:
        link = Path(directory) / "hard-linked-evidence.json"
        try:
            os.link(_VALID_EVIDENCE_PATH, link)
        except OSError as error:
            pytest.skip(f"hard-link creation is unavailable: {error}")

        with pytest.raises(PreflightError, match="missing, changed, or malformed"):
            _run_production_foundation(configuration, azure, classified_evidence=link)


def test_safe_reader_rejects_path_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pathname swap between lstat and open cannot substitute evidence."""
    target = tmp_path / "evidence.json"
    replacement = tmp_path / "replacement.json"
    target.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    original_open = os.open
    swapped = False

    def swapping_open(path: Any, flags: int, mode: int = 0o777) -> int:
        nonlocal swapped
        if Path(path) == target and not swapped:
            swapped = True
            target.unlink()
            replacement.replace(target)
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(OSError, match="changed before it was opened"):
        classifier.read_regular_file(target)


def test_safe_reader_rejects_directory(tmp_path: Path) -> None:
    """A directory cannot be consumed as an authorization artifact."""
    with pytest.raises(OSError, match="not a bounded single-link regular file"):
        classifier.read_regular_file(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires a POSIX host")
def test_safe_reader_rejects_fifo(tmp_path: Path) -> None:
    """A FIFO cannot block or stream substituted authorization bytes."""
    fifo = tmp_path / "evidence.fifo"
    make_fifo = cast(Callable[[Path], None], os.__dict__["mkfifo"])
    make_fifo(fifo)

    with pytest.raises(OSError, match="not a bounded single-link regular file"):
        classifier.read_regular_file(fifo)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix socket unavailable")
def test_safe_reader_rejects_socket(tmp_path: Path) -> None:
    """A filesystem socket cannot supply authorization bytes."""
    path = tmp_path / "evidence.socket"
    unix_family = cast(int, socket.__dict__["AF_UNIX"])
    server = socket.socket(unix_family, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        with pytest.raises(OSError, match="not a bounded single-link regular file"):
            classifier.read_regular_file(path)
    finally:
        server.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX device path required")
def test_safe_reader_rejects_device() -> None:
    """A character device cannot supply authorization bytes."""
    with pytest.raises(OSError, match="not a bounded single-link regular file"):
        classifier.read_regular_file(Path("/dev/null"))


@pytest.mark.real_registry_identity
def test_publish_phase_also_binds_classified_evidence() -> None:
    """The publish phase reuses the same-job classified evidence for AcrPush."""
    configuration = load_configuration(disabled_environment(), phase="publish")
    azure = FakeAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = ALTERNATE_REGISTRY_NAME
    alternate_configuration = load_configuration(environment, phase="publish")
    alternate_azure = FakeAzure(alternate_configuration, foundation_exists=True)
    alternate_azure.inventory_registry_name = ALTERNATE_REGISTRY_NAME
    alternate_azure.acr_assignment_registry_name = ALTERNATE_REGISTRY_NAME
    _authenticated_production_azure(alternate_azure)

    passing = _run_production_foundation(configuration, azure, phase="publish")
    assert passing["phase"] == "publish"
    with pytest.raises(
        PreflightError, match="does not match the repository-derived registry identity"
    ):
        _run_production_foundation(
            alternate_configuration, alternate_azure, phase="publish"
        )


# --- Third independent review: Graph validate-before-filter ----------------


def test_production_foundation_rejects_malformed_unrelated_graph_assignment() -> None:
    """A malformed non-Graph assignment fails closed before it is filtered out.

    Third-review regression: on the pre-fix head an unrelated assignment missing
    principalId and appRoleId was skipped without structural validation.
    """
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(),
        {"resourceId": "dddddddd-1111-2222-3333-444444444444"},
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="app role assignment is malformed"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_graph_assignment_missing_id() -> None:
    """A Graph app-role assignment without an id fails closed (finding 9)."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    assignment = graph_assignment()
    del assignment["id"]
    azure.graph_app_role_assignments = [assignment]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="app role assignment is malformed"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_graph_assignment_missing_odata_type() -> None:
    """A Graph app-role assignment without @odata.type fails closed (finding 9)."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    assignment = graph_assignment()
    del assignment["@odata.type"]
    azure.graph_app_role_assignments = [assignment]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="app role assignment is malformed"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_rejects_malformed_assignment_before_graph_entry() -> (
    None
):
    """A malformed unrelated assignment before the Graph entry fails closed.

    Third-review regression: the pre-fix head skipped a non-Graph assignment on
    the resourceId check before validating its appRoleId.
    """
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    malformed = graph_assignment(
        assignment_id=SECOND_GRAPH_ASSIGNMENT_ID,
        resource_id="dddddddd-1111-2222-3333-444444444444",
    )
    malformed["appRoleId"] = "not-a-canonical-guid"
    azure.graph_app_role_assignments = [
        malformed,
        graph_assignment(),
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="app role ID is not a canonical GUID"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_production_foundation_accepts_valid_unrelated_before_graph_entry() -> None:
    """A structurally valid unrelated assignment before the Graph entry passes."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(
            assignment_id=SECOND_GRAPH_ASSIGNMENT_ID,
            app_role_id="abcdef01-2345-6789-abcd-ef0123456789",
            resource_id="dddddddd-1111-2222-3333-444444444444",
        ),
        graph_assignment(),
    ]
    _authenticated_production_azure(azure)

    evidence = run_preflight(
        configuration, azure, phase="production-foundation", repository_root=ROOT
    )

    assert evidence["phase"] == "production-foundation"


def test_unrelated_graph_assignment_for_wrong_principal_fails() -> None:
    """An unrelated assignment must still belong to the queried principal."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(
            assignment_id=SECOND_GRAPH_ASSIGNMENT_ID,
            app_role_id="abcdef01-2345-6789-abcd-ef0123456789",
            principal_id="99999999-0000-1111-2222-333333333333",
            resource_id="dddddddd-1111-2222-3333-444444444444",
        ),
        graph_assignment(),
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="targets another principal"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


@pytest.mark.parametrize("missing_field", ["id", "@odata.type"])
def test_graph_assignment_requires_id_and_type(missing_field: str) -> None:
    """Every Graph assignment requires identity and canonical type metadata."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    assignment = graph_assignment()
    del assignment[missing_field]
    azure.graph_app_role_assignments = [assignment]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="app role assignment is malformed"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_graph_assignment_rejects_wrong_odata_type() -> None:
    """A non-app-role OData entity cannot satisfy the assignment contract."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(odata_type="#microsoft.graph.oauth2PermissionGrant")
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="app role assignment is malformed"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_graph_assignment_rejects_duplicate_assignment_id() -> None:
    """Distinct role rows cannot reuse one Graph assignment identity."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    azure.graph_app_role_assignments = [
        graph_assignment(),
        graph_assignment(
            app_role_id="abcdef01-2345-6789-abcd-ef0123456789",
            resource_id="dddddddd-1111-2222-3333-444444444444",
        ),
    ]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="duplicate IDs"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_graph_assignment_rejects_null_id() -> None:
    """A null assignment identity fails before resource filtering."""
    configuration = _production_foundation_configuration()
    azure = FakeAzure(configuration, foundation_exists=True)
    assignment: dict[str, Any] = graph_assignment()
    assignment["id"] = None
    azure.graph_app_role_assignments = [assignment]
    _authenticated_production_azure(azure)

    with pytest.raises(PreflightError, match="assignment ID is malformed"):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


def test_graph_assignment_authorization_failure_is_sanitized() -> None:
    """Graph authorization errors expose only the typed query boundary message."""
    configuration = _production_foundation_configuration()

    class UnauthorizedGraphAzure(FakeAzure):
        def json(self, *arguments: str, allow_missing: bool = False) -> Any:
            if any("appRoleAssignments" in argument for argument in arguments):
                raise AzureQueryError(AzureQueryFailureKind.UNAUTHORIZED, "rest")
            return super().json(*arguments, allow_missing=allow_missing)

    azure = UnauthorizedGraphAzure(configuration, foundation_exists=True)
    _authenticated_production_azure(azure)

    with pytest.raises(
        AzureQueryError, match=r"Azure rest query failed \(UNAUTHORIZED\)"
    ):
        run_preflight(
            configuration, azure, phase="production-foundation", repository_root=ROOT
        )


# --- Third independent review: raw ACR-name validation ---------------------


@pytest.mark.parametrize(
    "registry_name",
    [" acr12 ", " acr12", "acr12 ", "acr\t12", "acr\n12", "acr\u00a012"],
)
def test_raw_registry_name_rejects_whitespace(registry_name: str) -> None:
    """Surrounding or internal whitespace fails before any trimming.

    Third-review regression: on the pre-fix head ``_required`` stripped the raw
    value, so a padded registry name loaded successfully.
    """
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = registry_name

    with pytest.raises(PreflightError, match="contains whitespace"):
        load_configuration(environment, phase="production-foundation")


@pytest.mark.parametrize(
    "registry_name",
    ["example", "Example", "placeholder", "PLACEHOLDER", "changeme", "ChangeMe"],
)
def test_registry_name_rejects_exact_placeholders(registry_name: str) -> None:
    """Exact, case-insensitive placeholder names fail closed."""
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = registry_name

    with pytest.raises(PreflightError, match="is a placeholder"):
        load_configuration(environment, phase="production-foundation")


@pytest.mark.parametrize("registry_name", ["replace_me", "todo"])
def test_registry_name_rejects_grammar_invalid_placeholders(
    registry_name: str,
) -> None:
    """replace_me and todo are already rejected by the ACR grammar."""
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = registry_name

    with pytest.raises(PreflightError, match="5-50 ASCII alphanumeric"):
        load_configuration(environment, phase="production-foundation")


@pytest.mark.parametrize(
    "registry_name", ["acrexamplefoo", "placeholderacr01", "changemeregistry"]
)
def test_registry_names_with_placeholder_substrings_load(
    registry_name: str,
) -> None:
    """A legitimate name containing a placeholder word as a substring is accepted.

    Third-review regression: on the pre-fix head ``_required`` rejected any name
    containing ``example`` or ``placeholder`` as a substring.
    """
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = registry_name

    configuration = load_configuration(environment, phase="production-foundation")

    assert configuration.registry_name == registry_name


@pytest.mark.parametrize("registry_name", ["acr12", "a" * 50])
def test_raw_registry_name_length_boundaries_load(registry_name: str) -> None:
    """The 5 and 50 character boundaries load unchanged."""
    environment = disabled_environment()
    environment["AZURE_CONTAINER_REGISTRY_NAME"] = registry_name

    configuration = load_configuration(environment, phase="production-foundation")

    assert configuration.registry_name == registry_name
