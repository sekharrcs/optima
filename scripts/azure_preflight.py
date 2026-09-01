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
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, urljoin, urlparse

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXPECTED_LOCATION = "eastus2"
EXPECTED_ENVIRONMENT = "hackathon"
EXPECTED_REPOSITORY = "sekharrcs/optima"
MAX_FIXED_MONTHLY_COST_INR = Decimal("5000")
MAX_COST_REVIEW_AGE_DAYS = 31
REDIS_API_VERSION = "2025-07-01"
REDIS_RESOURCE_TYPE = "redisEnterprise"
REDIS_SKU_NAME = "Balanced_B0"
REDIS_SKU_TIER = "Balanced"
REDIS_MAX_RESPONSE_PAGES = 32
REDIS_ARM_HOST = "management.azure.com"
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


class AzureQueryFailureKind(StrEnum):
    """Sanitized failure categories retained at the Azure query boundary."""

    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    TRANSIENT = "TRANSIENT"
    MALFORMED = "MALFORMED"
    OTHER = "OTHER"


class AzureQueryError(PreflightError):
    """A sanitized Azure query failure with no response body or identifiers."""

    def __init__(self, kind: AzureQueryFailureKind, operation: str) -> None:
        self.kind = kind
        self.operation = operation
        super().__init__(f"Azure {operation} query failed ({kind.value})")


class RedisPreflightErrorCode(StrEnum):
    """Stable fail-closed outcome codes for Managed Redis preflight."""

    PROVIDER_METADATA_MALFORMED = "REDIS_PROVIDER_METADATA_MALFORMED"
    PROVIDER_NOT_REGISTERED = "REDIS_PROVIDER_NOT_REGISTERED"
    RESOURCE_TYPE_NOT_ADVERTISED = "REDIS_RESOURCE_TYPE_NOT_ADVERTISED"
    REGION_NOT_ADVERTISED = "REDIS_REGION_NOT_ADVERTISED"
    API_VERSION_NOT_ADVERTISED = "REDIS_API_VERSION_NOT_ADVERTISED"
    SKU_QUERY_NOT_FOUND = "REDIS_SKU_QUERY_NOT_FOUND"
    SKU_QUERY_UNAUTHORIZED = "REDIS_SKU_QUERY_UNAUTHORIZED"
    SKU_QUERY_TRANSIENT = "REDIS_SKU_QUERY_TRANSIENT"
    SKU_QUERY_FAILED = "REDIS_SKU_QUERY_FAILED"
    SKU_RESPONSE_MALFORMED = "REDIS_SKU_RESPONSE_MALFORMED"
    SKU_PAGINATION_INVALID = "REDIS_SKU_PAGINATION_INVALID"
    REQUESTED_SKU_ABSENT = "REDIS_REQUESTED_SKU_ABSENT"
    REQUESTED_SKU_REGION_ABSENT = "REDIS_REQUESTED_SKU_REGION_ABSENT"
    RESTRICTION_MALFORMED = "REDIS_RESTRICTION_MALFORMED"
    RESTRICTION_UNKNOWN = "REDIS_RESTRICTION_UNKNOWN"
    TARGET_REGION_RESTRICTED = "REDIS_TARGET_REGION_RESTRICTED"
    SUBSCRIPTION_RESTRICTED = "REDIS_SUBSCRIPTION_RESTRICTED"
    QUOTA_RESTRICTED = "REDIS_QUOTA_RESTRICTED"
    QUOTA_API_VERSION_NOT_ADVERTISED = "REDIS_QUOTA_API_VERSION_NOT_ADVERTISED"
    QUOTA_QUERY_UNAUTHORIZED = "REDIS_QUOTA_QUERY_UNAUTHORIZED"
    QUOTA_QUERY_TRANSIENT = "REDIS_QUOTA_QUERY_TRANSIENT"
    QUOTA_QUERY_FAILED = "REDIS_QUOTA_QUERY_FAILED"
    QUOTA_RESPONSE_MALFORMED = "REDIS_QUOTA_RESPONSE_MALFORMED"
    QUOTA_PAGINATION_INVALID = "REDIS_QUOTA_PAGINATION_INVALID"
    QUOTA_EXHAUSTED = "REDIS_QUOTA_EXHAUSTED"


class RedisPreflightError(PreflightError):
    """A hard-blocking Managed Redis outcome with a stable error code."""

    def __init__(self, code: RedisPreflightErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


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
            raise AzureQueryError(
                AzureQueryFailureKind.TRANSIENT, arguments[0]
            ) from error
        if completed.returncode != 0:
            if allow_missing and completed.returncode in {3, 4}:
                return None
            raise AzureQueryError(
                _classify_azure_query_failure(completed.stderr), arguments[0]
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AzureQueryError(
                AzureQueryFailureKind.MALFORMED, arguments[0]
            ) from error


def _classify_azure_query_failure(stderr: str) -> AzureQueryFailureKind:
    """Classify an Azure CLI failure without retaining diagnostic content."""
    normalized = stderr.casefold()
    status_match = re.search(
        r"(?:http\s+)?status(?:\s+code)?[^0-9]{0,12}(401|403|404|408|429|5\d\d)\b",
        normalized,
    )
    status = status_match.group(1) if status_match is not None else None
    if status in {"401", "403"} or any(
        code in normalized
        for code in ("authorizationfailed", "authenticationfailed", "unauthorized")
    ):
        return AzureQueryFailureKind.UNAUTHORIZED
    if status == "404" or any(
        code in normalized
        for code in ("resource not found", "resourcenotfound", "not found", "notfound")
    ):
        return AzureQueryFailureKind.NOT_FOUND
    if status in {"408", "429"} or (status is not None and status.startswith("5")):
        return AzureQueryFailureKind.TRANSIENT
    if any(
        code in normalized
        for code in (
            "gatewaytimeout",
            "internalservererror",
            "requesttimeout",
            "serviceunavailable",
            "toomanyrequests",
        )
    ):
        return AzureQueryFailureKind.TRANSIENT
    return AzureQueryFailureKind.OTHER


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


def _check_providers(azure: AzureQuery) -> dict[str, Any]:
    cache_provider: dict[str, Any] | None = None
    for namespace in REQUIRED_PROVIDERS:
        provider = azure.json("provider", "show", "--namespace", namespace)
        if not isinstance(provider, dict):
            if namespace == "Microsoft.Cache":
                raise RedisPreflightError(
                    RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
                    "Microsoft.Cache provider metadata is malformed",
                )
            raise PreflightError(
                f"Required resource provider {namespace} response is malformed"
            )
        registration_state = provider.get("registrationState")
        if not isinstance(registration_state, str) or not registration_state.strip():
            if namespace == "Microsoft.Cache":
                raise RedisPreflightError(
                    RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
                    "Microsoft.Cache registration state is malformed",
                )
            raise PreflightError(
                f"Required resource provider {namespace} response is malformed"
            )
        if registration_state.strip().casefold() != "registered":
            if namespace == "Microsoft.Cache":
                raise RedisPreflightError(
                    RedisPreflightErrorCode.PROVIDER_NOT_REGISTERED,
                    "Microsoft.Cache is not registered",
                )
            raise PreflightError(
                f"Required resource provider {namespace} is not registered"
            )
        if namespace == "Microsoft.Cache":
            cache_provider = provider
    if cache_provider is None:
        raise RedisPreflightError(
            RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
            "Microsoft.Cache provider metadata is unavailable",
        )
    return cache_provider


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


def _normalized_exact_token(value: object) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _string_list(
    value: object,
    *,
    code: RedisPreflightErrorCode,
    field: str,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise RedisPreflightError(code, f"Microsoft.Cache {field} is malformed")
    return [item.strip() for item in value]


def _provider_resource_types(provider: Mapping[str, Any]) -> list[dict[str, Any]]:
    resource_types = provider.get("resourceTypes")
    if not isinstance(resource_types, list) or any(
        not isinstance(resource_type, dict) for resource_type in resource_types
    ):
        raise RedisPreflightError(
            RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
            "Microsoft.Cache resource type metadata is malformed",
        )
    return cast(list[dict[str, Any]], resource_types)


def _provider_zone_evidence(
    resource_type: Mapping[str, Any], expected_location: str
) -> list[str]:
    zone_mappings = resource_type.get("zoneMappings")
    if zone_mappings is None:
        return []
    if not isinstance(zone_mappings, list) or any(
        not isinstance(mapping, dict) for mapping in zone_mappings
    ):
        raise RedisPreflightError(
            RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
            "Microsoft.Cache provider zone mappings are malformed",
        )
    zones: set[str] = set()
    for mapping in zone_mappings:
        location = mapping.get("location")
        if not isinstance(location, str) or not location.strip():
            raise RedisPreflightError(
                RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
                "Microsoft.Cache provider zone mapping location is malformed",
            )
        mapping_zones = _string_list(
            mapping.get("zones"),
            code=RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
            field="provider zone mapping zones",
        )
        if _normalized_location(location) == expected_location:
            zones.update(mapping_zones)
    return sorted(zones)


def _validate_redis_page_url(
    candidate: str,
    *,
    subscription: str,
    expected_path: str,
    pagination_code: RedisPreflightErrorCode,
) -> str:
    try:
        absolute = urljoin(f"https://{REDIS_ARM_HOST}", candidate)
        parsed = urlparse(absolute)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise RedisPreflightError(
            pagination_code,
            "Microsoft.Cache pagination continuation is malformed",
        ) from error
    expected_prefix = f"/subscriptions/{subscription}/providers/Microsoft.Cache/"
    if (
        parsed.scheme.casefold() != "https"
        or hostname is None
        or hostname.casefold() != REDIS_ARM_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.casefold().startswith(expected_prefix.casefold())
        or parsed.path.rstrip("/").casefold() != expected_path.casefold()
    ):
        raise RedisPreflightError(
            pagination_code,
            "Microsoft.Cache pagination continuation is outside the approved route",
        )
    query = parse_qs(parsed.query, keep_blank_values=True)
    api_versions = [
        value
        for key, values in query.items()
        if key.casefold() == "api-version"
        for value in values
    ]
    if api_versions != [REDIS_API_VERSION]:
        raise RedisPreflightError(
            pagination_code,
            "Microsoft.Cache pagination changed the approved API version",
        )
    return absolute


def _redis_query_error_code(
    kind: AzureQueryFailureKind,
    *,
    quota: bool,
) -> RedisPreflightErrorCode:
    if kind == AzureQueryFailureKind.UNAUTHORIZED:
        return (
            RedisPreflightErrorCode.QUOTA_QUERY_UNAUTHORIZED
            if quota
            else RedisPreflightErrorCode.SKU_QUERY_UNAUTHORIZED
        )
    if kind == AzureQueryFailureKind.TRANSIENT:
        return (
            RedisPreflightErrorCode.QUOTA_QUERY_TRANSIENT
            if quota
            else RedisPreflightErrorCode.SKU_QUERY_TRANSIENT
        )
    if kind == AzureQueryFailureKind.MALFORMED:
        return (
            RedisPreflightErrorCode.QUOTA_RESPONSE_MALFORMED
            if quota
            else RedisPreflightErrorCode.SKU_RESPONSE_MALFORMED
        )
    if kind == AzureQueryFailureKind.NOT_FOUND and not quota:
        return RedisPreflightErrorCode.SKU_QUERY_NOT_FOUND
    return (
        RedisPreflightErrorCode.QUOTA_QUERY_FAILED
        if quota
        else RedisPreflightErrorCode.SKU_QUERY_FAILED
    )


def _read_redis_pages(
    azure: AzureQuery,
    *,
    initial_url: str,
    subscription: str,
    expected_path: str,
    quota: bool,
    allow_not_found: bool = False,
) -> tuple[list[dict[str, Any]], int] | None:
    malformed_code = (
        RedisPreflightErrorCode.QUOTA_RESPONSE_MALFORMED
        if quota
        else RedisPreflightErrorCode.SKU_RESPONSE_MALFORMED
    )
    pagination_code = (
        RedisPreflightErrorCode.QUOTA_PAGINATION_INVALID
        if quota
        else RedisPreflightErrorCode.SKU_PAGINATION_INVALID
    )
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    url = initial_url
    page_count = 0
    while True:
        if page_count >= REDIS_MAX_RESPONSE_PAGES:
            raise RedisPreflightError(
                pagination_code,
                "Microsoft.Cache response exceeded the bounded page limit",
            )
        url = _validate_redis_page_url(
            url,
            subscription=subscription,
            expected_path=expected_path,
            pagination_code=pagination_code,
        )
        if url in seen_urls:
            raise RedisPreflightError(
                pagination_code,
                "Microsoft.Cache response contains a pagination cycle",
            )
        seen_urls.add(url)
        page_count += 1
        try:
            document = azure.json("rest", "--method", "get", "--url", url)
        except AzureQueryError as error:
            if (
                allow_not_found
                and page_count == 1
                and error.kind == AzureQueryFailureKind.NOT_FOUND
            ):
                return None
            raise RedisPreflightError(
                _redis_query_error_code(error.kind, quota=quota),
                "Microsoft.Cache query did not return usable evidence",
            ) from error
        values = document.get("value") if isinstance(document, dict) else None
        if not isinstance(values, list) or any(
            not isinstance(value, dict) for value in values
        ):
            raise RedisPreflightError(
                malformed_code, "Microsoft.Cache response value is malformed"
            )
        items.extend(cast(list[dict[str, Any]], values))
        next_link = document.get("nextLink")
        if next_link is None:
            return items, page_count
        if not isinstance(next_link, str) or not next_link.strip():
            raise RedisPreflightError(
                pagination_code,
                "Microsoft.Cache response nextLink is malformed",
            )
        url = next_link.strip()


def _sku_locations_and_zones(
    item: Mapping[str, Any], expected_location: str
) -> tuple[set[str], set[str]]:
    if "locations" in item and item.get("locations") is None:
        raise RedisPreflightError(
            RedisPreflightErrorCode.SKU_RESPONSE_MALFORMED,
            "Microsoft.Cache SKU locations is malformed",
        )
    locations = {
        _normalized_location(location)
        for location in _string_list(
            item.get("locations"),
            code=RedisPreflightErrorCode.SKU_RESPONSE_MALFORMED,
            field="SKU locations",
        )
    }
    location_info = item.get("locationInfo")
    if "locationInfo" not in item:
        return locations, set()
    if not isinstance(location_info, list) or any(
        not isinstance(detail, dict) for detail in location_info
    ):
        raise RedisPreflightError(
            RedisPreflightErrorCode.SKU_RESPONSE_MALFORMED,
            "Microsoft.Cache SKU locationInfo is malformed",
        )
    zones: set[str] = set()
    for detail in location_info:
        location = detail.get("location")
        if not isinstance(location, str) or not location.strip():
            raise RedisPreflightError(
                RedisPreflightErrorCode.SKU_RESPONSE_MALFORMED,
                "Microsoft.Cache SKU locationInfo location is malformed",
            )
        normalized_location = _normalized_location(location)
        locations.add(normalized_location)
        detail_zones = _string_list(
            detail.get("zones"),
            code=RedisPreflightErrorCode.SKU_RESPONSE_MALFORMED,
            field="SKU locationInfo zones",
        )
        if normalized_location == expected_location:
            zones.update(detail_zones)
    return locations, zones


def _restriction_scope(
    restriction: Mapping[str, Any], expected_location: str
) -> tuple[bool, bool, str]:
    restriction_type = _normalized_exact_token(restriction.get("type"))
    reason = _normalized_exact_token(restriction.get("reasonCode"))
    if not restriction_type or not reason:
        raise RedisPreflightError(
            RedisPreflightErrorCode.RESTRICTION_MALFORMED,
            "Managed Redis restriction type or reason is malformed",
        )
    values = _string_list(
        restriction.get("values"),
        code=RedisPreflightErrorCode.RESTRICTION_MALFORMED,
        field="restriction values",
    )
    restriction_info = restriction.get("restrictionInfo")
    if restriction_info is None:
        restriction_info = {}
    if not isinstance(restriction_info, dict):
        raise RedisPreflightError(
            RedisPreflightErrorCode.RESTRICTION_MALFORMED,
            "Managed Redis restrictionInfo is malformed",
        )
    locations = _string_list(
        restriction_info.get("locations"),
        code=RedisPreflightErrorCode.RESTRICTION_MALFORMED,
        field="restriction locations",
    )
    zones = _string_list(
        restriction_info.get("zones"),
        code=RedisPreflightErrorCode.RESTRICTION_MALFORMED,
        field="restriction zones",
    )
    if restriction_type == "location":
        locations.extend(values)
    elif restriction_type == "zone":
        zones.extend(values)
    else:
        locations.extend(values)
    normalized_locations = {_normalized_location(location) for location in locations}
    global_scope = not normalized_locations and not zones
    target_region_scope = expected_location in normalized_locations and not zones
    applicable = global_scope or target_region_scope
    return applicable, target_region_scope, reason


def _check_sku_restrictions(
    items: Sequence[Mapping[str, Any]], expected_location: str
) -> int:
    ignored = 0
    for item in items:
        restrictions = item.get("restrictions")
        if "restrictions" not in item:
            restrictions = []
        if not isinstance(restrictions, list) or any(
            not isinstance(restriction, dict) for restriction in restrictions
        ):
            raise RedisPreflightError(
                RedisPreflightErrorCode.RESTRICTION_MALFORMED,
                "Managed Redis restrictions are malformed",
            )
        for restriction in restrictions:
            applicable, target_region_scope, reason = _restriction_scope(
                restriction, expected_location
            )
            if not applicable:
                ignored += 1
                continue
            if reason == "notavailableforsubscription":
                raise RedisPreflightError(
                    RedisPreflightErrorCode.SUBSCRIPTION_RESTRICTED,
                    "Managed Redis is not available for this subscription",
                )
            if reason == "quotaid":
                raise RedisPreflightError(
                    RedisPreflightErrorCode.QUOTA_RESTRICTED,
                    "Managed Redis has an applicable quota restriction",
                )
            if target_region_scope:
                raise RedisPreflightError(
                    RedisPreflightErrorCode.TARGET_REGION_RESTRICTED,
                    "Managed Redis has an explicit target-region restriction",
                )
            raise RedisPreflightError(
                RedisPreflightErrorCode.RESTRICTION_UNKNOWN,
                "Managed Redis has an applicable unrecognized restriction",
            )
    return ignored


def _quota_evidence(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    quota_resource_type: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if quota_resource_type is None:
        return {
            "status": "NOT_EXPOSED",
            "source": "PROVIDER_METADATA",
        }
    api_versions = _string_list(
        quota_resource_type.get("apiVersions"),
        code=RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
        field="quota resource type API versions",
    )
    if REDIS_API_VERSION.casefold() not in {
        api_version.casefold() for api_version in api_versions
    }:
        raise RedisPreflightError(
            RedisPreflightErrorCode.QUOTA_API_VERSION_NOT_ADVERTISED,
            "The advertised quota route does not support the approved API version",
        )
    subscription = configuration.subscription_id
    expected_path = (
        f"/subscriptions/{subscription}/providers/Microsoft.Cache/locations/"
        f"{configuration.location}/usages"
    )
    usage_url = (
        f"https://{REDIS_ARM_HOST}{expected_path}?api-version={REDIS_API_VERSION}"
    )
    page_result = _read_redis_pages(
        azure,
        initial_url=usage_url,
        subscription=subscription,
        expected_path=expected_path,
        quota=True,
        allow_not_found=True,
    )
    if page_result is None:
        return {
            "status": "NOT_EXPOSED",
            "source": "ADVERTISED_ROUTE_NOT_FOUND",
        }
    usages, page_count = page_result
    quota_matches: list[dict[str, Any]] = []
    for usage in usages:
        name = usage.get("name")
        if not isinstance(name, dict) or not isinstance(name.get("value"), str):
            raise RedisPreflightError(
                RedisPreflightErrorCode.QUOTA_RESPONSE_MALFORMED,
                "Microsoft.Cache quota meter identity is malformed",
            )
        if _normalized_exact_token(name["value"]) == REDIS_SKU_NAME.casefold():
            quota_matches.append(usage)
    if len(quota_matches) != 1:
        raise RedisPreflightError(
            RedisPreflightErrorCode.QUOTA_RESPONSE_MALFORMED,
            "Microsoft.Cache quota response lacks one exact Balanced_B0 meter",
        )
    current = quota_matches[0].get("currentValue")
    limit = quota_matches[0].get("limit")
    if (
        not isinstance(current, int)
        or isinstance(current, bool)
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or current < 0
        or limit < 0
    ):
        raise RedisPreflightError(
            RedisPreflightErrorCode.QUOTA_RESPONSE_MALFORMED,
            "Microsoft.Cache quota values must be non-negative integers",
        )
    if current >= limit:
        raise RedisPreflightError(
            RedisPreflightErrorCode.QUOTA_EXHAUSTED,
            "Managed Redis Balanced_B0 quota is exhausted",
        )
    return {
        "current_value": current,
        "limit": limit,
        "page_count": page_count,
        "status": "AVAILABLE",
        "source": "MICROSOFT_CACHE_LOCATIONS_USAGES",
    }


def _check_redis_availability(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    provider: Mapping[str, Any],
) -> dict[str, Any]:
    resource_types = _provider_resource_types(provider)
    redis_resource_types = [
        resource_type
        for resource_type in resource_types
        if _normalized_exact_token(resource_type.get("resourceType"))
        == REDIS_RESOURCE_TYPE.casefold()
    ]
    if not redis_resource_types:
        raise RedisPreflightError(
            RedisPreflightErrorCode.RESOURCE_TYPE_NOT_ADVERTISED,
            "Microsoft.Cache does not advertise the redisEnterprise type",
        )
    if len(redis_resource_types) > 1:
        raise RedisPreflightError(
            RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
            "Microsoft.Cache advertises duplicate redisEnterprise resource types",
        )
    redis_resource_type = redis_resource_types[0]
    expected_location = _normalized_location(configuration.location)
    if not isinstance(redis_resource_type.get("locations"), list):
        raise RedisPreflightError(
            RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
            "Microsoft.Cache redisEnterprise locations are malformed",
        )
    provider_locations = _string_list(
        redis_resource_type.get("locations"),
        code=RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
        field="redisEnterprise locations",
    )
    if expected_location not in {
        _normalized_location(location) for location in provider_locations
    }:
        raise RedisPreflightError(
            RedisPreflightErrorCode.REGION_NOT_ADVERTISED,
            "Microsoft.Cache does not advertise redisEnterprise in the target region",
        )
    if not isinstance(redis_resource_type.get("apiVersions"), list):
        raise RedisPreflightError(
            RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
            "Microsoft.Cache redisEnterprise API versions are malformed",
        )
    redis_api_versions = _string_list(
        redis_resource_type.get("apiVersions"),
        code=RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
        field="redisEnterprise API versions",
    )
    if REDIS_API_VERSION.casefold() not in {
        api_version.casefold() for api_version in redis_api_versions
    }:
        raise RedisPreflightError(
            RedisPreflightErrorCode.API_VERSION_NOT_ADVERTISED,
            "Microsoft.Cache does not advertise the approved stable API version",
        )
    provider_zones = _provider_zone_evidence(redis_resource_type, expected_location)
    subscription = configuration.subscription_id
    sku_path = f"/subscriptions/{subscription}/providers/Microsoft.Cache/skus"
    sku_url = f"https://{REDIS_ARM_HOST}{sku_path}?api-version={REDIS_API_VERSION}"
    page_result = _read_redis_pages(
        azure,
        initial_url=sku_url,
        subscription=subscription,
        expected_path=sku_path,
        quota=False,
    )
    assert page_result is not None
    sku_items, sku_page_count = page_result
    exact_items = [
        item
        for item in sku_items
        if _normalized_exact_token(item.get("resourceType"))
        == REDIS_RESOURCE_TYPE.casefold()
        and _normalized_exact_token(item.get("name")) == REDIS_SKU_NAME.casefold()
        and _normalized_exact_token(item.get("tier")) == REDIS_SKU_TIER.casefold()
    ]
    if not exact_items:
        raise RedisPreflightError(
            RedisPreflightErrorCode.REQUESTED_SKU_ABSENT,
            "The exact redisEnterprise Balanced_B0 Balanced SKU is absent",
        )
    advertised_locations: set[str] = set()
    target_zones: set[str] = set()
    target_items: list[dict[str, Any]] = []
    for item in exact_items:
        locations, zones = _sku_locations_and_zones(item, expected_location)
        advertised_locations.update(locations)
        if expected_location in locations:
            target_items.append(item)
            target_zones.update(zones)
    if not target_items:
        raise RedisPreflightError(
            RedisPreflightErrorCode.REQUESTED_SKU_REGION_ABSENT,
            "The exact Balanced_B0 SKU is not advertised in the target region",
        )
    ignored_restrictions = _check_sku_restrictions(exact_items, expected_location)
    quota_resource_types = [
        resource_type
        for resource_type in resource_types
        if _normalized_exact_token(resource_type.get("resourceType"))
        == "locations/usages"
    ]
    if len(quota_resource_types) > 1:
        raise RedisPreflightError(
            RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
            "Microsoft.Cache advertises duplicate quota resource types",
        )
    quota = _quota_evidence(
        configuration,
        azure,
        quota_resource_types[0] if quota_resource_types else None,
    )
    return {
        "allocation": {
            "status": "NOT_PROVABLE_BEFORE_CREATION",
            "statement": (
                "Provider and SKU metadata do not reserve or guarantee current "
                "physical capacity."
            ),
        },
        "api_version": REDIS_API_VERSION,
        "provider": {
            "namespace": "Microsoft.Cache",
            "registration": "REGISTERED",
            "regional_support": "ADVERTISED",
            "resource_type": REDIS_RESOURCE_TYPE,
            "target_region": configuration.location,
            "target_region_zones": provider_zones,
        },
        "quota": quota,
        "restrictions": {
            "applicable": 0,
            "ignored_unrelated": ignored_restrictions,
            "status": "NONE_APPLICABLE",
        },
        "sku": {
            "advertised_locations": sorted(advertised_locations),
            "catalog_item_count": len(sku_items),
            "catalog_page_count": sku_page_count,
            "matched_entry_count": len(exact_items),
            "name": REDIS_SKU_NAME,
            "resource_type": REDIS_RESOURCE_TYPE,
            "status": "ADVERTISED",
            "target_region_zones": sorted(target_zones),
            "tier": REDIS_SKU_TIER,
        },
    }


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
    cache_provider = _check_providers(azure)
    _check_oidc_federation(configuration, azure)
    redis_evidence = _check_redis_availability(configuration, azure, cache_provider)
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
            "redis_provider_registration",
            "redis_regional_resource_type",
            "redis_exact_sku_advertisement",
            "redis_applicable_restrictions",
            "redis_quota_exposure",
            "redis_allocation_not_provable",
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
        "redis": redis_evidence,
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
