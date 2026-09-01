"""Fail-closed read-only preflight for the OPTIMA Azure deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXPECTED_LOCATION = "eastus2"
EXPECTED_ENVIRONMENT = "hackathon"
EXPECTED_REPOSITORY = "sekharrcs/optima"
MAX_FIXED_MONTHLY_COST_INR = Decimal("5000")
MAX_COST_REVIEW_AGE_DAYS = 31
ACR_PUSH_ROLE_ID = "8311e382-0749-4cb8-b61a-304f252e45ec"
ACR_PULL_ROLE_ID = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
CONTRIBUTOR_ROLE_ID = "b24988ac-6180-42a0-ab88-20f7382dd24c"
READER_ROLE_ID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
OPENAI_USER_ROLE_ID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
FORBIDDEN_DEPLOYMENT_ROLE_IDS = {
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
    "18d7d88d-d35e-462f-b5bf-48877a3e4ade",
    "f58310d9-a9f6-439a-9e8d-f62e7b41a168",
}
PLACEHOLDER_MARKERS = ("replace-", "example", "placeholder")
REQUIRED_PROVIDERS = (
    "Microsoft.Authorization",
    "Microsoft.App",
    "Microsoft.Cache",
    "Microsoft.CognitiveServices",
    "Microsoft.ContainerRegistry",
    "Microsoft.DocumentDB",
    "Microsoft.Insights",
    "Microsoft.ManagedIdentity",
    "Microsoft.OperationalInsights",
    "Microsoft.Resources",
)
REQUIRED_FOUNDATION_RESOURCE_TYPES = {
    "microsoft.app/managedenvironments": 1,
    "microsoft.cache/redisenterprise": 1,
    "microsoft.containerregistry/registries": 1,
    "microsoft.documentdb/databaseaccounts": 1,
    "microsoft.insights/components": 1,
    "microsoft.managedidentity/userassignedidentities": 2,
}


class PreflightError(RuntimeError):
    """A deployment prerequisite is missing or cannot be proven."""


class AzureQuery(Protocol):
    """Read-only Azure query boundary used by preflight and tests."""

    def json(self, *arguments: str, allow_missing: bool = False) -> Any:
        """Run one Azure CLI query and decode its JSON response."""


@dataclass(frozen=True)
class ModelBinding:
    """One logical OPTIMA role bound to an exact Azure deployment model."""

    role: str
    deployment: str
    model: str
    version: str


@dataclass(frozen=True)
class PricingConfiguration:
    """Reviewed catalog provenance and exact per-million-token rates."""

    catalog_version: str
    binding_sha256: str
    source_url: str
    currency: str
    small_model: str
    small_model_version: str
    small_input: Decimal
    small_output: Decimal
    small_cached_input: Decimal | None
    strong_model: str
    strong_model_version: str
    strong_input: Decimal
    strong_output: Decimal
    strong_cached_input: Decimal | None
    judge_model: str
    judge_model_version: str
    judge_input: Decimal
    judge_output: Decimal
    judge_cached_input: Decimal | None
    embedding_model: str
    embedding_model_version: str
    embedding_input: Decimal


@dataclass(frozen=True)
class DeploymentConfiguration:
    """Non-secret production bindings plus proof that the UI secret exists."""

    tenant_id: str
    subscription_id: str
    deployment_client_id: str
    deployment_identity_resource_id: str
    resource_group: str
    location: str
    registry_name: str
    openai_resource_id: str
    foundry_base_url: str
    ui_auth_client_id: str
    ui_auth_tenant_id: str
    ui_auth_redirect_uri: str
    ui_auth_secret_present: bool
    embedding_dimension: int
    models: tuple[ModelBinding, ...]
    pricing: PricingConfiguration
    expected_fixed_monthly_cost_inr: Decimal
    cost_reviewed_on: date
    github_repository: str
    github_environment: str
    oidc_request_available: bool


class AzureCli:
    """Execute Azure CLI commands without echoing identifiers or credentials."""

    def json(self, *arguments: str, allow_missing: bool = False) -> Any:
        """Run one Azure CLI command and return decoded JSON."""
        command = ["az", *arguments, "--only-show-errors", "--output", "json"]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except FileNotFoundError as error:
            raise PreflightError("Azure CLI is required for live preflight") from error
        except subprocess.TimeoutExpired as error:
            raise PreflightError(
                f"Azure CLI {arguments[0]} query exceeded 90 seconds"
            ) from error
        if completed.returncode != 0:
            if allow_missing and completed.returncode in {3, 4}:
                return None
            raise PreflightError(
                f"Azure CLI {arguments[0]} query failed; inspect the runner's "
                "redacted Azure diagnostics"
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise PreflightError(
                f"Azure CLI {arguments[0]} returned malformed JSON"
            ) from error


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise PreflightError(f"Required deployment setting {name} is missing")
    if any(marker in value.casefold() for marker in PLACEHOLDER_MARKERS):
        raise PreflightError(f"Deployment setting {name} still contains a placeholder")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PreflightError(f"Deployment setting {name} contains control characters")
    return value


def _decimal(
    environment: Mapping[str, str],
    name: str,
    *,
    required: bool = True,
) -> Decimal | None:
    raw_value = environment.get(name, "").strip()
    if not raw_value and not required:
        return None
    value = _required(environment, name)
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise PreflightError(f"Deployment setting {name} must be a decimal") from error
    if not parsed.is_finite() or parsed < 0 or (required and parsed == 0):
        qualifier = "a positive" if required else "a non-negative"
        raise PreflightError(f"Deployment setting {name} must be {qualifier} decimal")
    return parsed


def _model_binding(environment: Mapping[str, str], role: str) -> ModelBinding:
    prefix = "OPTIMA_FOUNDRY" if role in {"SMALL", "STRONG"} else "OPTIMA"
    deployment_name = (
        f"{prefix}_{role}_DEPLOYMENT"
        if role != "EMBEDDING"
        else "OPTIMA_REDIS_EMBEDDING_DEPLOYMENT"
    )
    model_name = (
        f"{prefix}_{role}_MODEL"
        if role != "EMBEDDING"
        else "OPTIMA_REDIS_EMBEDDING_MODEL"
    )
    version_name = (
        f"{prefix}_{role}_MODEL_VERSION"
        if role != "EMBEDDING"
        else "OPTIMA_REDIS_EMBEDDING_MODEL_VERSION"
    )
    return ModelBinding(
        role=role,
        deployment=_required(environment, deployment_name),
        model=_required(environment, model_name),
        version=_required(environment, version_name),
    )


def load_configuration(environment: Mapping[str, str]) -> DeploymentConfiguration:
    """Load and validate deployment configuration without retaining secrets."""
    try:
        embedding_dimension = int(
            _required(environment, "OPTIMA_REDIS_EMBEDDING_DIMENSION")
        )
    except ValueError as error:
        raise PreflightError(
            "OPTIMA_REDIS_EMBEDDING_DIMENSION must be an integer"
        ) from error
    if not 1 <= embedding_dimension <= 32768:
        raise PreflightError(
            "OPTIMA_REDIS_EMBEDDING_DIMENSION must be between 1 and 32768"
        )
    try:
        reviewed_on = date.fromisoformat(
            _required(environment, "OPTIMA_COST_REVIEWED_ON")
        )
    except ValueError as error:
        raise PreflightError("OPTIMA_COST_REVIEWED_ON must use YYYY-MM-DD") from error
    pricing = PricingConfiguration(
        catalog_version=_required(environment, "OPTIMA_PRICING_CATALOG_VERSION"),
        binding_sha256=_required(environment, "OPTIMA_PRICING_BINDING_SHA256"),
        source_url=_required(environment, "OPTIMA_PRICING_SOURCE_URL"),
        currency=_required(environment, "OPTIMA_PRICING_CURRENCY"),
        small_model=_required(environment, "OPTIMA_PRICING_SMALL_MODEL"),
        small_model_version=_required(
            environment, "OPTIMA_PRICING_SMALL_MODEL_VERSION"
        ),
        small_input=cast(
            Decimal,
            _decimal(
                environment,
                "OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS",
            ),
        ),
        small_output=cast(
            Decimal,
            _decimal(
                environment,
                "OPTIMA_PRICING_SMALL_OUTPUT_RATE_PER_MILLION_TOKENS",
            ),
        ),
        small_cached_input=_decimal(
            environment,
            "OPTIMA_PRICING_SMALL_CACHED_INPUT_RATE_PER_MILLION_TOKENS",
            required=False,
        ),
        strong_model=_required(environment, "OPTIMA_PRICING_STRONG_MODEL"),
        strong_model_version=_required(
            environment, "OPTIMA_PRICING_STRONG_MODEL_VERSION"
        ),
        strong_input=cast(
            Decimal,
            _decimal(
                environment,
                "OPTIMA_PRICING_STRONG_INPUT_RATE_PER_MILLION_TOKENS",
            ),
        ),
        strong_output=cast(
            Decimal,
            _decimal(
                environment,
                "OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS",
            ),
        ),
        strong_cached_input=_decimal(
            environment,
            "OPTIMA_PRICING_STRONG_CACHED_INPUT_RATE_PER_MILLION_TOKENS",
            required=False,
        ),
        judge_model=_required(environment, "OPTIMA_PRICING_JUDGE_MODEL"),
        judge_model_version=_required(
            environment, "OPTIMA_PRICING_JUDGE_MODEL_VERSION"
        ),
        judge_input=cast(
            Decimal,
            _decimal(
                environment,
                "OPTIMA_PRICING_JUDGE_INPUT_RATE_PER_MILLION_TOKENS",
            ),
        ),
        judge_output=cast(
            Decimal,
            _decimal(
                environment,
                "OPTIMA_PRICING_JUDGE_OUTPUT_RATE_PER_MILLION_TOKENS",
            ),
        ),
        judge_cached_input=_decimal(
            environment,
            "OPTIMA_PRICING_JUDGE_CACHED_INPUT_RATE_PER_MILLION_TOKENS",
            required=False,
        ),
        embedding_model=_required(environment, "OPTIMA_PRICING_EMBEDDING_MODEL"),
        embedding_model_version=_required(
            environment, "OPTIMA_PRICING_EMBEDDING_MODEL_VERSION"
        ),
        embedding_input=cast(
            Decimal,
            _decimal(
                environment,
                "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS",
            ),
        ),
    )
    fixed_cost = cast(
        Decimal,
        _decimal(environment, "OPTIMA_EXPECTED_FIXED_MONTHLY_COST_INR"),
    )
    configuration = DeploymentConfiguration(
        tenant_id=_required(environment, "AZURE_TENANT_ID"),
        subscription_id=_required(environment, "AZURE_SUBSCRIPTION_ID"),
        deployment_client_id=_required(environment, "AZURE_CLIENT_ID"),
        deployment_identity_resource_id=_required(
            environment, "AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID"
        ),
        resource_group=_required(environment, "AZURE_RESOURCE_GROUP"),
        location=_required(environment, "AZURE_LOCATION"),
        registry_name=_required(environment, "AZURE_CONTAINER_REGISTRY_NAME"),
        openai_resource_id=_required(environment, "AZURE_OPENAI_RESOURCE_ID"),
        foundry_base_url=_required(environment, "OPTIMA_FOUNDRY_BASE_URL"),
        ui_auth_client_id=_required(environment, "OPTIMA_UI_AUTH_CLIENT_ID"),
        ui_auth_tenant_id=_required(environment, "OPTIMA_UI_AUTH_TENANT_ID"),
        ui_auth_redirect_uri=environment.get("OPTIMA_UI_AUTH_REDIRECT_URI", "").strip(),
        ui_auth_secret_present=bool(
            _required(environment, "OPTIMA_UI_AUTH_CLIENT_SECRET")
        ),
        embedding_dimension=embedding_dimension,
        models=tuple(
            _model_binding(environment, role)
            for role in ("SMALL", "STRONG", "JUDGE", "EMBEDDING")
        ),
        pricing=pricing,
        expected_fixed_monthly_cost_inr=fixed_cost,
        cost_reviewed_on=reviewed_on,
        github_repository=_required(environment, "GITHUB_REPOSITORY"),
        github_environment=_required(environment, "OPTIMA_GITHUB_ENVIRONMENT"),
        oidc_request_available=bool(
            environment.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
            and environment.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
        ),
    )
    validate_configuration(configuration)
    return configuration


def validate_configuration(
    configuration: DeploymentConfiguration,
    *,
    today: date | None = None,
) -> None:
    """Validate non-Azure deployment invariants and reviewed selections."""
    if configuration.location != EXPECTED_LOCATION:
        raise PreflightError(
            f"Azure location must remain {EXPECTED_LOCATION}; no fallback is allowed"
        )
    if configuration.resource_group != "rg-optima-hackathon":
        raise PreflightError("Azure resource group must remain rg-optima-hackathon")
    if configuration.github_repository != EXPECTED_REPOSITORY:
        raise PreflightError(f"OIDC repository must be {EXPECTED_REPOSITORY}")
    if configuration.github_environment != EXPECTED_ENVIRONMENT:
        raise PreflightError(f"OIDC environment must be {EXPECTED_ENVIRONMENT}")
    if not configuration.oidc_request_available:
        raise PreflightError("GitHub OIDC request variables are unavailable")
    if configuration.ui_auth_tenant_id != configuration.tenant_id:
        raise PreflightError(
            "UI authentication tenant must match the deployment tenant"
        )
    if len({binding.deployment for binding in configuration.models}) != len(
        configuration.models
    ):
        raise PreflightError(
            "SMALL, STRONG, JUDGE, and embedding deployments must be distinct"
        )
    pricing_bindings = {
        "SMALL": (
            configuration.pricing.small_model,
            configuration.pricing.small_model_version,
        ),
        "STRONG": (
            configuration.pricing.strong_model,
            configuration.pricing.strong_model_version,
        ),
        "JUDGE": (
            configuration.pricing.judge_model,
            configuration.pricing.judge_model_version,
        ),
        "EMBEDDING": (
            configuration.pricing.embedding_model,
            configuration.pricing.embedding_model_version,
        ),
    }
    for binding in configuration.models:
        if pricing_bindings[binding.role] != (binding.model, binding.version):
            raise PreflightError(
                f"{binding.role} pricing model/version does not match its live "
                "deployment binding"
            )
    if configuration.expected_fixed_monthly_cost_inr > MAX_FIXED_MONTHLY_COST_INR:
        raise PreflightError(
            "Reviewed fixed monthly infrastructure estimate exceeds the INR 5,000 "
            "infrastructure allocation"
        )
    current_date = today or date.today()
    age_days = (current_date - configuration.cost_reviewed_on).days
    if age_days < 0 or age_days > MAX_COST_REVIEW_AGE_DAYS:
        raise PreflightError(
            f"Infrastructure cost review must be no more than "
            f"{MAX_COST_REVIEW_AGE_DAYS} days old"
        )
    if not re.fullmatch(r"[A-Z]{3}", configuration.pricing.currency):
        raise PreflightError("Pricing currency must be a three-letter uppercase code")
    pricing_source = urlparse(configuration.pricing.source_url)
    if (
        pricing_source.scheme != "https"
        or not pricing_source.hostname
        or pricing_source.username
        or pricing_source.password
        or pricing_source.query
        or pricing_source.fragment
    ):
        raise PreflightError("OPTIMA_PRICING_SOURCE_URL must be a public HTTPS URL")
    expected_pricing_digest = pricing_binding_sha256(configuration.pricing)
    if re.fullmatch(r"[0-9a-f]{64}", configuration.pricing.binding_sha256) is None:
        raise PreflightError(
            "OPTIMA_PRICING_BINDING_SHA256 must be 64 lowercase hexadecimal characters"
        )
    if configuration.pricing.binding_sha256 != expected_pricing_digest:
        raise PreflightError(
            "OPTIMA_PRICING_BINDING_SHA256 does not match the reviewed pricing "
            f"binding; expected {expected_pricing_digest}"
        )
    parsed_url = urlparse(configuration.foundry_base_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.path.rstrip("/") != "/openai/v1"
        or parsed_url.params
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise PreflightError(
            "OPTIMA_FOUNDRY_BASE_URL must be an HTTPS /openai/v1 API root"
        )
    if configuration.ui_auth_redirect_uri:
        redirect = urlparse(configuration.ui_auth_redirect_uri)
        if (
            redirect.scheme != "https"
            or not redirect.hostname
            or redirect.path != "/.auth/login/aad/callback"
            or redirect.params
            or redirect.query
            or redirect.fragment
        ):
            raise PreflightError(
                "OPTIMA_UI_AUTH_REDIRECT_URI must be the exact HTTPS Container "
                "Apps authentication callback"
            )


def pricing_binding_sha256(pricing: PricingConfiguration) -> str:
    """Hash every reviewed pricing provenance, identity, and rate field."""
    document = {
        "catalog_version": pricing.catalog_version,
        "currency": pricing.currency,
        "embedding": {
            "input": str(pricing.embedding_input),
            "model": pricing.embedding_model,
            "model_version": pricing.embedding_model_version,
        },
        "judge": {
            "cached_input": (
                str(pricing.judge_cached_input)
                if pricing.judge_cached_input is not None
                else None
            ),
            "input": str(pricing.judge_input),
            "model": pricing.judge_model,
            "model_version": pricing.judge_model_version,
            "output": str(pricing.judge_output),
        },
        "small": {
            "cached_input": (
                str(pricing.small_cached_input)
                if pricing.small_cached_input is not None
                else None
            ),
            "input": str(pricing.small_input),
            "model": pricing.small_model,
            "model_version": pricing.small_model_version,
            "output": str(pricing.small_output),
        },
        "source_url": pricing.source_url,
        "strong": {
            "cached_input": (
                str(pricing.strong_cached_input)
                if pricing.strong_cached_input is not None
                else None
            ),
            "input": str(pricing.strong_input),
            "model": pricing.strong_model,
            "model_version": pricing.strong_model_version,
            "output": str(pricing.strong_output),
        },
    }
    serialized = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _redact_identifier(value: str) -> str:
    if len(value) < 9:
        return "redacted"
    return f"{value[:4]}...{value[-4:]}"


def _normalized_location(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _check_account(configuration: DeploymentConfiguration, azure: AzureQuery) -> None:
    account = azure.json("account", "show")
    if not isinstance(account, dict):
        raise PreflightError("Azure account response is malformed")
    if account.get("id") != configuration.subscription_id:
        raise PreflightError(
            "Azure CLI subscription does not match AZURE_SUBSCRIPTION_ID"
        )
    if account.get("tenantId") != configuration.tenant_id:
        raise PreflightError("Azure CLI tenant does not match AZURE_TENANT_ID")
    if account.get("state") != "Enabled":
        raise PreflightError("Azure subscription is not enabled")


def _check_providers(azure: AzureQuery) -> None:
    for namespace in REQUIRED_PROVIDERS:
        provider = azure.json("provider", "show", "--namespace", namespace)
        if (
            not isinstance(provider, dict)
            or provider.get("registrationState") != "Registered"
        ):
            raise PreflightError(
                f"Required resource provider {namespace} is not registered"
            )


def _identity_parts(resource_id: str) -> tuple[str, str]:
    match = re.fullmatch(
        r"/subscriptions/[^/]+/resourceGroups/([^/]+)/providers/"
        r"Microsoft\.ManagedIdentity/userAssignedIdentities/([^/]+)",
        resource_id,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise PreflightError(
            "AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID is not a user-assigned "
            "managed identity resource ID"
        )
    return match.group(1), match.group(2)


def _check_oidc_federation(
    configuration: DeploymentConfiguration, azure: AzureQuery
) -> None:
    resource_group, identity_name = _identity_parts(
        configuration.deployment_identity_resource_id
    )
    identity = azure.json(
        "identity",
        "show",
        "--resource-group",
        resource_group,
        "--name",
        identity_name,
    )
    if (
        not isinstance(identity, dict)
        or identity.get("clientId") != configuration.deployment_client_id
        or not identity.get("principalId")
    ):
        raise PreflightError("OIDC deployment identity does not match AZURE_CLIENT_ID")
    credentials = azure.json(
        "identity",
        "federated-credential",
        "list",
        "--resource-group",
        resource_group,
        "--identity-name",
        identity_name,
    )
    expected_subject = (
        f"repo:{configuration.github_repository}:environment:"
        f"{configuration.github_environment}"
    )
    if not isinstance(credentials, list) or not any(
        isinstance(credential, dict)
        and credential.get("issuer") == "https://token.actions.githubusercontent.com"
        and credential.get("subject") == expected_subject
        and "api://AzureADTokenExchange" in credential.get("audiences", [])
        for credential in credentials
    ):
        raise PreflightError(
            "GitHub environment federated credential is missing or mismatched"
        )
    assignments = azure.json(
        "role",
        "assignment",
        "list",
        "--assignee-object-id",
        str(identity["principalId"]),
        "--all",
    )
    group = azure.json(
        "group",
        "show",
        "--name",
        configuration.resource_group,
        allow_missing=True,
    )
    subscription_scope = f"/subscriptions/{configuration.subscription_id}".casefold()
    resource_group_scope = (
        f"/subscriptions/{configuration.subscription_id}/resourceGroups/"
        f"{configuration.resource_group}"
    ).casefold()
    if not isinstance(assignments, list):
        raise PreflightError("OIDC deployment role assignment response is malformed")
    contributor_scopes = {
        str(assignment.get("scope", "")).casefold()
        for assignment in assignments
        if isinstance(assignment, dict)
        and str(assignment.get("roleDefinitionId", ""))
        .casefold()
        .endswith(CONTRIBUTOR_ROLE_ID)
    }
    required_contributor_scope = (
        subscription_scope if group is None else resource_group_scope
    )
    if contributor_scopes != {required_contributor_scope}:
        raise PreflightError(
            "OIDC deployment identity must have Contributor only at the approved scope"
        )
    if group is not None and not any(
        isinstance(assignment, dict)
        and str(assignment.get("roleDefinitionId", ""))
        .casefold()
        .endswith(READER_ROLE_ID)
        and str(assignment.get("scope", "")).casefold() == subscription_scope
        for assignment in assignments
    ):
        raise PreflightError(
            "OIDC deployment identity requires subscription Reader after bootstrap"
        )
    registry_scope = (
        f"{resource_group_scope}/providers/Microsoft.ContainerRegistry/registries/"
        f"{configuration.registry_name}"
    ).casefold()
    if any(
        isinstance(assignment, dict)
        and str(assignment.get("roleDefinitionId", ""))
        .casefold()
        .endswith(ACR_PUSH_ROLE_ID)
        and str(assignment.get("scope", "")).casefold() != registry_scope
        for assignment in assignments
    ):
        raise PreflightError("AcrPush must not be inherited or broadly scoped")
    if any(
        isinstance(assignment, dict)
        and any(
            str(assignment.get("roleDefinitionId", "")).casefold().endswith(role_id)
            for role_id in FORBIDDEN_DEPLOYMENT_ROLE_IDS
        )
        for assignment in assignments
    ):
        raise PreflightError(
            "OIDC deployment identity has a forbidden Owner or RBAC administration role"
        )


def _check_ui_authentication(
    configuration: DeploymentConfiguration, azure: AzureQuery
) -> None:
    if not configuration.ui_auth_redirect_uri:
        raise PreflightError(
            "OPTIMA_UI_AUTH_REDIRECT_URI is required after foundation provisioning"
        )
    application = azure.json(
        "ad", "app", "show", "--id", configuration.ui_auth_client_id
    )
    if not isinstance(application, dict):
        raise PreflightError("UI Entra application response is malformed")
    if application.get("signInAudience") != "AzureADMyOrg":
        raise PreflightError("UI Entra application must be single-tenant")
    web = application.get("web", {})
    redirect_uris = web.get("redirectUris", []) if isinstance(web, dict) else []
    if configuration.ui_auth_redirect_uri not in redirect_uris:
        raise PreflightError(
            "UI Entra application is missing the exact Container Apps callback URI"
        )
    service_principal = azure.json(
        "ad", "sp", "show", "--id", configuration.ui_auth_client_id
    )
    if not isinstance(service_principal, dict) or (
        service_principal.get("appRoleAssignmentRequired") is not True
    ):
        raise PreflightError(
            "UI Entra application must require explicit user assignment"
        )


def _check_redis_availability(
    configuration: DeploymentConfiguration, azure: AzureQuery
) -> None:
    subscription = configuration.subscription_id
    sku_url = (
        "https://management.azure.com/subscriptions/"
        f"{subscription}/providers/Microsoft.Cache/skus?api-version=2024-11-01"
    )
    sku_document = azure.json("rest", "--method", "get", "--url", sku_url)
    sku_items = sku_document.get("value") if isinstance(sku_document, dict) else None
    if not isinstance(sku_items, list):
        raise PreflightError("Microsoft.Cache SKU response is malformed")
    expected_location = _normalized_location(configuration.location)
    matches = [
        item
        for item in sku_items
        if isinstance(item, dict)
        and item.get("name") == "Balanced_B0"
        and str(item.get("resourceType", "")).casefold()
        in {"redisenterprise", "redisenterprise/databases"}
        and expected_location
        in {
            _normalized_location(str(location))
            for location in item.get("locations", [])
        }
    ]
    if len(matches) != 1:
        raise PreflightError(
            "Managed Redis Balanced_B0 availability in eastus2 was not proven"
        )
    restrictions = matches[0].get("restrictions", [])
    if restrictions:
        raise PreflightError(
            "Managed Redis Balanced_B0 has an active eastus2 restriction"
        )
    usage_url = (
        "https://management.azure.com/subscriptions/"
        f"{subscription}/providers/Microsoft.Cache/locations/"
        f"{configuration.location}/usages?api-version=2024-11-01"
    )
    usage_document = azure.json("rest", "--method", "get", "--url", usage_url)
    usages = usage_document.get("value") if isinstance(usage_document, dict) else None
    if not isinstance(usages, list):
        raise PreflightError("Microsoft.Cache quota response is malformed")
    quota_matches = []
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        name = usage.get("name", {})
        names = name.values() if isinstance(name, dict) else (name,)
        if any("balancedb0" in _normalized_location(str(value)) for value in names):
            quota_matches.append(usage)
    if len(quota_matches) != 1:
        raise PreflightError(
            "Managed Redis Balanced_B0 eastus2 quota could not be identified"
        )
    usage = quota_matches[0]
    current = usage.get("currentValue")
    limit = usage.get("limit")
    if not isinstance(current, int) or not isinstance(limit, int) or current >= limit:
        raise PreflightError("Managed Redis Balanced_B0 eastus2 quota is exhausted")


def _openai_account_parts(resource_id: str) -> tuple[str, str]:
    match = re.fullmatch(
        r"/subscriptions/[^/]+/resourceGroups/([^/]+)/providers/"
        r"Microsoft\.CognitiveServices/accounts/([^/]+)",
        resource_id,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise PreflightError("AZURE_OPENAI_RESOURCE_ID is not an account resource ID")
    return match.group(1), match.group(2)


def _check_model_deployments(
    configuration: DeploymentConfiguration, azure: AzureQuery
) -> dict[str, dict[str, str]]:
    resource_group, account_name = _openai_account_parts(
        configuration.openai_resource_id
    )
    account = azure.json(
        "cognitiveservices",
        "account",
        "show",
        "--name",
        account_name,
        "--resource-group",
        resource_group,
    )
    if not isinstance(account, dict):
        raise PreflightError("Azure OpenAI account response is malformed")
    endpoint = account.get("properties", {}).get("endpoint")
    expected_host = urlparse(configuration.foundry_base_url).hostname
    actual_host = urlparse(str(endpoint)).hostname
    if actual_host != expected_host:
        raise PreflightError(
            "Foundry base URL does not identify the selected Azure OpenAI account"
        )
    evidence: dict[str, dict[str, str]] = {}
    for binding in configuration.models:
        deployment = azure.json(
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "--name",
            account_name,
            "--resource-group",
            resource_group,
            "--deployment-name",
            binding.deployment,
        )
        if not isinstance(deployment, dict):
            raise PreflightError(f"{binding.role} deployment response is malformed")
        properties = deployment.get("properties", {})
        model = properties.get("model", {})
        if properties.get("provisioningState") != "Succeeded":
            raise PreflightError(f"{binding.role} deployment is not usable")
        if (
            model.get("name") != binding.model
            or model.get("version") != binding.version
        ):
            raise PreflightError(
                f"{binding.role} deployment model/version does not match "
                "reviewed binding"
            )
        sku = deployment.get("sku", {})
        capacity = sku.get("capacity")
        if not sku.get("name") or not isinstance(capacity, int) or capacity <= 0:
            raise PreflightError(f"{binding.role} deployment has no usable capacity")
        evidence[binding.role] = {
            "deployment": binding.deployment,
            "model": binding.model,
            "version": binding.version,
        }
    return evidence


def _check_iac_representation(repository_root: Path) -> None:
    required_tokens = {
        "infra/resource-group.bicep": (
            "modules/runtime-access.bicep",
            "modules/container-apps.bicep",
            "deployRuntimeAccess",
            "deployContainerApps",
        ),
        "infra/modules/container-apps.bicep": (
            "Microsoft.App/managedEnvironments@",
            "Microsoft.App/jobs@",
            "OPTIMA_PRODUCTION_COST_MEASUREMENT_REQUIRED",
            "OPTIMA_API_BASE_URL",
        ),
        "infra/modules/runtime-access.bicep": (
            ACR_PULL_ROLE_ID,
            "00000000-0000-0000-0000-000000000002",
            "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments",
        ),
    }
    for relative_path, tokens in required_tokens.items():
        path = repository_root / relative_path
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            raise PreflightError(
                f"Required IaC file {relative_path} is unavailable"
            ) from error
        missing = [token for token in tokens if token not in content]
        if missing:
            raise PreflightError(
                f"Required deployment contract is missing from {relative_path}"
            )


def _check_resource_group(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    *,
    require_foundation: bool,
) -> list[dict[str, Any]]:
    group = azure.json(
        "group",
        "show",
        "--name",
        configuration.resource_group,
        allow_missing=True,
    )
    if group is None:
        if require_foundation:
            raise PreflightError("OPTIMA resource group has not been provisioned")
        return []
    if not isinstance(group, dict):
        raise PreflightError("Azure resource group response is malformed")
    if _normalized_location(str(group.get("location", ""))) != _normalized_location(
        configuration.location
    ):
        raise PreflightError("OPTIMA resource group is not in eastus2")
    if not require_foundation:
        return []
    resources = azure.json(
        "resource", "list", "--resource-group", configuration.resource_group
    )
    if not isinstance(resources, list):
        raise PreflightError("Azure resource inventory response is malformed")
    typed_resources = [resource for resource in resources if isinstance(resource, dict)]
    counts: dict[str, int] = {}
    for resource in typed_resources:
        resource_type = str(resource.get("type", "")).casefold()
        counts[resource_type] = counts.get(resource_type, 0) + 1
    for resource_type, minimum in REQUIRED_FOUNDATION_RESOURCE_TYPES.items():
        if counts.get(resource_type, 0) < minimum:
            raise PreflightError(
                f"Required foundation resource type {resource_type} is missing"
            )
    return typed_resources


def _check_acr_push(configuration: DeploymentConfiguration, azure: AzureQuery) -> None:
    identity_resource_group, identity_name = _identity_parts(
        configuration.deployment_identity_resource_id
    )
    identity = azure.json(
        "identity",
        "show",
        "--resource-group",
        identity_resource_group,
        "--name",
        identity_name,
    )
    if not isinstance(identity, dict) or not identity.get("principalId"):
        raise PreflightError("OIDC deployment identity principal ID is unavailable")
    registry = azure.json(
        "acr",
        "show",
        "--name",
        configuration.registry_name,
        "--resource-group",
        configuration.resource_group,
    )
    if not isinstance(registry, dict):
        raise PreflightError("Azure Container Registry response is malformed")
    if registry.get("adminUserEnabled") is not False:
        raise PreflightError(
            "Azure Container Registry admin credentials must be disabled"
        )
    registry_id = registry.get("id")
    if not isinstance(registry_id, str) or not registry_id:
        raise PreflightError("Azure Container Registry resource ID is unavailable")
    assignments = azure.json(
        "role",
        "assignment",
        "list",
        "--assignee-object-id",
        str(identity["principalId"]),
        "--scope",
        registry_id,
        "--all",
    )
    if not isinstance(assignments, list) or not any(
        isinstance(assignment, dict)
        and str(assignment.get("roleDefinitionId", ""))
        .casefold()
        .endswith(ACR_PUSH_ROLE_ID)
        and str(assignment.get("scope", "")).casefold() == registry_id.casefold()
        for assignment in assignments
    ):
        raise PreflightError(
            "OIDC deployment identity lacks AcrPush on the OPTIMA registry"
        )


def _check_foundry_runtime_access(
    configuration: DeploymentConfiguration, azure: AzureQuery
) -> None:
    identity = azure.json(
        "identity",
        "show",
        "--resource-group",
        configuration.resource_group,
        "--name",
        "id-optima-api-hackathon",
    )
    if not isinstance(identity, dict) or not identity.get("principalId"):
        raise PreflightError("OPTIMA API managed identity is unavailable")
    assignments = azure.json(
        "role",
        "assignment",
        "list",
        "--assignee-object-id",
        str(identity["principalId"]),
        "--scope",
        configuration.openai_resource_id,
        "--all",
    )
    if not isinstance(assignments, list) or not any(
        isinstance(assignment, dict)
        and str(assignment.get("roleDefinitionId", ""))
        .casefold()
        .endswith(OPENAI_USER_ROLE_ID)
        and str(assignment.get("scope", "")).casefold()
        == configuration.openai_resource_id.casefold()
        for assignment in assignments
    ):
        raise PreflightError(
            "OPTIMA API identity lacks Cognitive Services OpenAI User on the "
            "selected account"
        )


def _one_resource(
    resources: list[dict[str, Any]], resource_type: str
) -> dict[str, Any]:
    matches = [
        resource
        for resource in resources
        if str(resource.get("type", "")).casefold() == resource_type.casefold()
    ]
    if len(matches) != 1:
        raise PreflightError(
            f"Expected exactly one foundation resource of type {resource_type}"
        )
    return matches[0]


def _check_runtime_access(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    resources: list[dict[str, Any]],
) -> None:
    registry = _one_resource(resources, "Microsoft.ContainerRegistry/registries")
    cosmos = _one_resource(resources, "Microsoft.DocumentDB/databaseAccounts")
    redis = _one_resource(resources, "Microsoft.Cache/redisEnterprise")
    for resource, label in (
        (registry, "registry"),
        (cosmos, "Cosmos"),
        (redis, "Redis"),
    ):
        if not isinstance(resource.get("id"), str) or not resource["id"]:
            raise PreflightError(f"{label} resource ID is unavailable")
    principals: dict[str, str] = {}
    for component in ("api", "ui"):
        identity = azure.json(
            "identity",
            "show",
            "--resource-group",
            configuration.resource_group,
            "--name",
            f"id-optima-{component}-hackathon",
        )
        if not isinstance(identity, dict) or not identity.get("principalId"):
            raise PreflightError(
                f"OPTIMA {component.upper()} managed identity is unavailable"
            )
        principals[component] = str(identity["principalId"])
    registry_assignments = azure.json(
        "role",
        "assignment",
        "list",
        "--scope",
        str(registry["id"]),
        "--all",
    )
    if not isinstance(registry_assignments, list):
        raise PreflightError("ACR role assignment response is malformed")
    for component, principal_id in principals.items():
        if not any(
            isinstance(assignment, dict)
            and assignment.get("principalId") == principal_id
            and str(assignment.get("roleDefinitionId", ""))
            .casefold()
            .endswith(ACR_PULL_ROLE_ID)
            for assignment in registry_assignments
        ):
            raise PreflightError(
                f"OPTIMA {component.upper()} identity lacks AcrPull on the registry"
            )
    cosmos_assignments = azure.json(
        "rest",
        "--method",
        "get",
        "--url",
        f"{cosmos['id']}/sqlRoleAssignments?api-version=2024-11-15",
    )
    cosmos_values = (
        cosmos_assignments.get("value")
        if isinstance(cosmos_assignments, dict)
        else None
    )
    expected_cosmos_scope = f"{cosmos['id']}/dbs/optima/colls/runs".casefold()
    if not isinstance(cosmos_values, list) or not any(
        isinstance(assignment, dict)
        and assignment.get("properties", {}).get("principalId") == principals["api"]
        and str(assignment.get("properties", {}).get("roleDefinitionId", ""))
        .casefold()
        .endswith("/sqlroledefinitions/00000000-0000-0000-0000-000000000002")
        and str(assignment.get("properties", {}).get("scope", "")).casefold()
        == expected_cosmos_scope
        for assignment in cosmos_values
    ):
        raise PreflightError(
            "OPTIMA API identity lacks container-scoped Cosmos data contribution"
        )
    redis_assignments = azure.json(
        "rest",
        "--method",
        "get",
        "--url",
        (
            f"{redis['id']}/databases/default/accessPolicyAssignments"
            "?api-version=2025-07-01"
        ),
    )
    redis_values = (
        redis_assignments.get("value") if isinstance(redis_assignments, dict) else None
    )
    if not isinstance(redis_values, list) or not any(
        isinstance(assignment, dict)
        and assignment.get("properties", {}).get("accessPolicyName") == "default"
        and assignment.get("properties", {}).get("user", {}).get("objectId")
        == principals["api"]
        for assignment in redis_values
    ):
        raise PreflightError(
            "OPTIMA API identity lacks the reviewed Redis default access policy"
        )


def _validated_digest(value: str, component: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise PreflightError(f"{component} image digest is not an immutable sha256")
    if value == "sha256:" + ("0" * 64):
        raise PreflightError(f"{component} image digest is still a placeholder")
    return value


def _check_artifact(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    *,
    repository: str,
    digest: str,
) -> None:
    metadata = azure.json(
        "acr",
        "manifest",
        "show-metadata",
        "--registry",
        configuration.registry_name,
        "--name",
        f"{repository}@{digest}",
    )
    if not isinstance(metadata, dict) or metadata.get("digest") != digest:
        raise PreflightError(
            f"Registry did not return the expected {repository} manifest digest"
        )


def run_preflight(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    *,
    phase: str,
    repository_root: Path,
    api_digest: str | None = None,
    ui_digest: str | None = None,
) -> dict[str, Any]:
    """Run a read-only preflight phase and return secret-free evidence."""
    if phase not in {"foundation", "publish", "artifacts", "rollout"}:
        raise PreflightError(f"Unsupported preflight phase {phase}")
    _check_iac_representation(repository_root)
    _check_account(configuration, azure)
    _check_providers(azure)
    _check_oidc_federation(configuration, azure)
    _check_redis_availability(configuration, azure)
    model_evidence = _check_model_deployments(configuration, azure)
    require_foundation = phase in {"publish", "artifacts", "rollout"}
    resources = _check_resource_group(
        configuration,
        azure,
        require_foundation=require_foundation,
    )
    if require_foundation:
        _check_ui_authentication(configuration, azure)
        _check_acr_push(configuration, azure)
        _check_foundry_runtime_access(configuration, azure)
    if phase == "rollout":
        _check_runtime_access(configuration, azure, resources)
    artifact_evidence: dict[str, str] = {}
    if phase in {"artifacts", "rollout"}:
        validated_api_digest = _validated_digest(api_digest or "", "API")
        validated_ui_digest = _validated_digest(ui_digest or "", "UI")
        if validated_api_digest == validated_ui_digest:
            raise PreflightError("API and UI image digests must be distinct")
        _check_artifact(
            configuration,
            azure,
            repository="optima-api",
            digest=validated_api_digest,
        )
        _check_artifact(
            configuration,
            azure,
            repository="optima-ui",
            digest=validated_ui_digest,
        )
        artifact_evidence = {
            "api": validated_api_digest,
            "ui": validated_ui_digest,
        }
    return {
        "artifacts": artifact_evidence,
        "checks": [
            "account",
            "providers",
            "github_oidc_federation",
            "redis_balanced_b0_availability_and_quota",
            "model_deployments",
            "iac_representation",
            *(
                (
                    "foundation_resources",
                    "ui_entra_authentication",
                    "acr_push",
                    "foundry_runtime_access",
                )
                if require_foundation
                else ()
            ),
            *(("runtime_access", "immutable_artifacts") if phase == "rollout" else ()),
            *(("immutable_artifacts",) if phase == "artifacts" else ()),
        ],
        "cost": {
            "binding_sha256": configuration.pricing.binding_sha256,
            "catalog_version": configuration.pricing.catalog_version,
            "currency": configuration.pricing.currency,
            "fixed_monthly_inr": str(configuration.expected_fixed_monthly_cost_inr),
            "reviewed_on": configuration.cost_reviewed_on.isoformat(),
            "source_url": configuration.pricing.source_url,
        },
        "environment": EXPECTED_ENVIRONMENT,
        "location": configuration.location,
        "models": model_evidence,
        "phase": phase,
        "resource_group": configuration.resource_group,
        "subscription_id": _redact_identifier(configuration.subscription_id),
        "tenant_id": _redact_identifier(configuration.tenant_id),
    }


def create_parser() -> argparse.ArgumentParser:
    """Create the preflight command-line parser."""
    parser = argparse.ArgumentParser(
        description="Run read-only fail-closed OPTIMA Azure deployment preflight."
    )
    parser.add_argument(
        "--phase",
        choices=("foundation", "publish", "artifacts", "rollout"),
        required=True,
    )
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--api-digest")
    parser.add_argument("--ui-digest")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run preflight and write only redacted, non-secret evidence."""
    arguments = create_parser().parse_args(argv)
    try:
        configuration = load_configuration(os.environ)
        evidence = run_preflight(
            configuration,
            AzureCli(),
            phase=arguments.phase,
            repository_root=arguments.repository_root.resolve(),
            api_digest=arguments.api_digest,
            ui_digest=arguments.ui_digest,
        )
        serialized = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        if arguments.output is not None:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(serialized, encoding="utf-8")
        print(serialized, end="")
    except PreflightError as error:
        print(f"PREFLIGHT FAILED: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
