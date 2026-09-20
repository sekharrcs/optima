"""Fail-closed read-only preflight for the OPTIMA Azure deployment."""

from __future__ import annotations

import argparse
import base64
import binascii
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
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import parse_qs, urljoin, urlparse

if TYPE_CHECKING or __package__:
    from scripts import oidc_federation, whatif_classification
else:
    import oidc_federation
    import whatif_classification

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
PREFLIGHT_PHASES = (
    "foundation-plan",
    "foundation-apply",
    "foundation",
    "production-session",
    "production-foundation",
    "publish",
    "artifacts",
    "rollout",
)
RUNTIME_COMPOSITION_PHASES = frozenset(
    {"production-foundation", "publish", "artifacts", "rollout"}
)
# Phases whose exact deployer role contract binds AcrPush to the approved ACR;
# these must load the registry identity even without runtime composition.
ACR_ROLE_IDENTITY_PHASES = frozenset(
    {"production-foundation", "publish", "artifacts", "rollout"}
)
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
MICROSOFT_GRAPH_HOST = "graph.microsoft.com"
MAX_TRANSITIVE_GROUPS = 999
MAX_GRAPH_APP_ROLE_ASSIGNMENTS = 999
# Microsoft Graph first-party application (appId) and the exact application
# permission the deployment identities require to read directory evidence.
MICROSOFT_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_APPLICATION_READ_ALL_ROLE_ID = "9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30"
# Casefolded ARM resource type of an Azure Container Registry, used to bind the
# approved registry from live inventory instead of trusting configuration alone.
ACR_REGISTRY_RESOURCE_TYPE = "microsoft.containerregistry/registries"
# An Azure Container Registry name is exactly 5-50 ASCII alphanumeric characters.
ACR_NAME_PATTERN = re.compile(r"[A-Za-z0-9]{5,50}")
# Exact, case-insensitive placeholder registry names that satisfy the ACR
# grammar but must never bind a live registry.
ACR_PLACEHOLDER_NAMES = frozenset(
    {"example", "placeholder", "changeme", "replace_me", "todo"}
)
# The closed foundation deployment profile whose ARM-evaluated what-if the
# fail-closed classifier approves. The production-foundation registry identity
# is derived from that classified evidence, never from configuration alone.
FOUNDATION_TEMPLATE_FILE = "infra/resource-group.bicep"
FOUNDATION_PARAMETER_FILE = "infra/environments/hackathon.runtime.bicepparam"
CONTAINER_REGISTRY_RESOURCE_ROLE = "container_registry"
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
    "microsoft.containerregistry/registries": 1,
    "microsoft.documentdb/databaseaccounts": 1,
    "microsoft.insights/components": 1,
    "microsoft.managedidentity/userassignedidentities": 2,
}
REDIS_FOUNDATION_RESOURCE_TYPE = "microsoft.cache/redisenterprise"
PREFLIGHT_CACHE_ONLY_SETTINGS = frozenset(
    {
        "OPTIMA_CACHE_SIMILARITY_THRESHOLD",
        "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS",
        "OPTIMA_PRICING_EMBEDDING_MODEL",
        "OPTIMA_PRICING_EMBEDDING_MODEL_VERSION",
        "OPTIMA_REDIS_ACCESS_KEY",
        "OPTIMA_REDIS_AUTH_MODE",
        "OPTIMA_REDIS_EMBEDDING_DEPLOYMENT",
        "OPTIMA_REDIS_EMBEDDING_DIMENSION",
        "OPTIMA_REDIS_EMBEDDING_MODEL",
        "OPTIMA_REDIS_EMBEDDING_MODEL_VERSION",
        "OPTIMA_REDIS_HOST",
        "OPTIMA_REDIS_INDEX_NAME",
        "OPTIMA_REDIS_MANAGED_IDENTITY_CLIENT_ID",
        "OPTIMA_REDIS_MAX_CONNECTIONS",
        "OPTIMA_REDIS_OBJECT_ID",
        "OPTIMA_REDIS_TIMEOUT_SECONDS",
        "OPTIMA_REDIS_TOKEN_ACQUISITION_TIMEOUT_SECONDS",
        "OPTIMA_REDIS_TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS",
        "OPTIMA_REDIS_TOKEN_REAUTH_TIMEOUT_SECONDS",
        "OPTIMA_REDIS_TOKEN_RENEWAL_ATTEMPTS",
        "OPTIMA_REDIS_TOKEN_RETRY_BACKOFF_CAP_SECONDS",
        "OPTIMA_REDIS_TOKEN_RETRY_BACKOFF_SECONDS",
    }
)


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
    embedding_model: str | None
    embedding_model_version: str | None
    embedding_input: Decimal | None


@dataclass(frozen=True)
class DeploymentConfiguration:
    """Non-secret production bindings plus proof that the UI secret exists.

    Runtime-composition bindings are absent when the effective phase does not
    deploy or consume them. Disabled-cache foundation planning omits them, while
    cache-enabled foundation planning validates them before Redis provisioning.
    """

    tenant_id: str
    subscription_id: str
    deployment_client_id: str
    deployment_identity_resource_id: str
    counterpart_client_id: str | None
    counterpart_identity_resource_id: str | None
    foundation_plan_role_definition_id: str | None
    resource_group: str
    location: str
    registry_name: str | None
    openai_resource_id: str | None
    foundry_base_url: str | None
    ui_auth_client_id: str | None
    ui_auth_tenant_id: str | None
    ui_auth_redirect_uri: str
    ui_auth_secret_present: bool
    semantic_cache_enabled: bool
    embedding_dimension: int | None
    models: tuple[ModelBinding, ...]
    pricing: PricingConfiguration | None
    expected_fixed_monthly_cost_inr: Decimal
    cost_reviewed_on: date
    github_repository: str
    github_environment: str
    oidc_request_available: bool


@dataclass(frozen=True)
class EffectiveRoleAssignment:
    """One canonical effective Azure RBAC assignment."""

    role_definition_id: str
    scope: str


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


def _required_boolean(environment: Mapping[str, str], name: str) -> bool:
    """Parse one required canonical Boolean without inferring a default."""
    value = _required(environment, name).casefold()
    if value == "true":
        return True
    if value == "false":
        return False
    raise PreflightError(f"Deployment setting {name} must be exactly true or false")


def _required_guid(environment: Mapping[str, str], name: str) -> str:
    """Load one stable canonical GUID."""
    value = _required(environment, name).casefold()
    if (
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value,
        )
        is None
    ):
        raise PreflightError(f"Deployment setting {name} must be a canonical GUID")
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


def _load_pricing(
    environment: Mapping[str, str], *, semantic_cache_enabled: bool
) -> PricingConfiguration:
    """Load the reviewed pricing catalog for a runtime-composition phase."""
    return PricingConfiguration(
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
        embedding_model=(
            _required(environment, "OPTIMA_PRICING_EMBEDDING_MODEL")
            if semantic_cache_enabled
            else None
        ),
        embedding_model_version=(
            _required(environment, "OPTIMA_PRICING_EMBEDDING_MODEL_VERSION")
            if semantic_cache_enabled
            else None
        ),
        embedding_input=(
            cast(
                Decimal,
                _decimal(
                    environment,
                    "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS",
                ),
            )
            if semantic_cache_enabled
            else None
        ),
    )


def _validated_registry_name(value: str) -> str:
    """Return ``value`` when it is a syntactically valid ACR name.

    Azure Container Registry names are exactly 5-50 ASCII alphanumeric
    characters. Malformed names such as ``bad_name`` must fail configuration
    load rather than reaching generic ARM-segment validation deeper in the
    preflight. The original value is returned unchanged so the canonical
    inventory binding, not this validator, performs case-insensitive matching.
    """
    if ACR_NAME_PATTERN.fullmatch(value) is None:
        raise PreflightError(
            "AZURE_CONTAINER_REGISTRY_NAME must be 5-50 ASCII alphanumeric characters"
        )
    return value


def _required_raw_registry_name(environment: Mapping[str, str], name: str) -> str:
    """Validate the raw registry name before any trimming or normalization.

    The generic ``_required`` helper strips surrounding whitespace, which would
    silently accept a padded value. The Azure Container Registry name must be
    exactly 5-50 ASCII alphanumeric characters with no surrounding or internal
    whitespace, and it must not be a recognized placeholder. A narrowly scoped
    loader keeps the strict raw contract off the global configuration helper.
    """
    raw = environment.get(name)
    if raw is None or raw == "":
        raise PreflightError(f"Required deployment setting {name} is missing")
    if raw != raw.strip() or any(character.isspace() for character in raw):
        raise PreflightError(f"Deployment setting {name} contains whitespace")
    if ACR_NAME_PATTERN.fullmatch(raw) is None:
        raise PreflightError(f"{name} must be 5-50 ASCII alphanumeric characters")
    if raw.casefold() in ACR_PLACEHOLDER_NAMES:
        raise PreflightError(f"Deployment setting {name} is a placeholder")
    return raw


def load_configuration(
    environment: Mapping[str, str], *, phase: str = "rollout"
) -> DeploymentConfiguration:
    """Load configuration for one effective phase without retaining secrets.

    ``phase`` selects which runtime-composition inputs are required. A
    disabled-cache foundation needs only foundation inputs. Cache-enabled
    foundation validates the complete runtime contract before Redis can be
    provisioned. The default keeps the strict rollout contract used by existing
    callers unchanged.
    """
    if phase not in PREFLIGHT_PHASES:
        raise PreflightError(f"Unsupported preflight phase {phase}")
    semantic_cache_enabled = _required_boolean(
        environment, "OPTIMA_SEMANTIC_CACHE_ENABLED"
    )
    foundation_plan = phase == "foundation-plan"
    separated_foundation_apply = phase == "foundation-apply"
    session_only = phase == "production-session"
    if foundation_plan and semantic_cache_enabled:
        raise PreflightError("foundation-plan does not support enabled semantic cache")
    runtime_composition = not session_only and (
        phase in RUNTIME_COMPOSITION_PHASES or semantic_cache_enabled
    )
    # ACR identity loads for its exact role contract without runtime composition.
    requires_registry_identity = phase in ACR_ROLE_IDENTITY_PHASES
    supplied_cache_settings = sorted(
        name
        for name in PREFLIGHT_CACHE_ONLY_SETTINGS
        if environment.get(name, "").strip()
    )
    if not session_only and not semantic_cache_enabled and supplied_cache_settings:
        raise PreflightError(
            "Disabled semantic cache cannot configure cache-only deployment "
            f"settings: {', '.join(supplied_cache_settings)}"
        )
    embedding_dimension: int | None = None
    if semantic_cache_enabled and not session_only:
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
    pricing = (
        _load_pricing(environment, semantic_cache_enabled=semantic_cache_enabled)
        if runtime_composition
        else None
    )
    fixed_cost = cast(
        Decimal,
        _decimal(environment, "OPTIMA_EXPECTED_FIXED_MONTHLY_COST_INR"),
    )
    configuration = DeploymentConfiguration(
        tenant_id=_required(environment, "AZURE_TENANT_ID"),
        subscription_id=_required(environment, "AZURE_SUBSCRIPTION_ID"),
        deployment_client_id=_required(
            environment,
            (
                "AZURE_FOUNDATION_PLAN_CLIENT_ID"
                if foundation_plan
                else "AZURE_CLIENT_ID"
            ),
        ),
        deployment_identity_resource_id=_required(
            environment,
            (
                "AZURE_FOUNDATION_PLAN_IDENTITY_RESOURCE_ID"
                if foundation_plan
                else "AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID"
            ),
        ),
        counterpart_client_id=(
            _required(
                environment,
                (
                    "AZURE_CLIENT_ID"
                    if foundation_plan
                    else "AZURE_FOUNDATION_PLAN_CLIENT_ID"
                ),
            )
            if foundation_plan or separated_foundation_apply
            else None
        ),
        counterpart_identity_resource_id=(
            _required(
                environment,
                (
                    "AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID"
                    if foundation_plan
                    else "AZURE_FOUNDATION_PLAN_IDENTITY_RESOURCE_ID"
                ),
            )
            if foundation_plan or separated_foundation_apply
            else None
        ),
        foundation_plan_role_definition_id=(
            _required_guid(environment, "AZURE_FOUNDATION_PLAN_ROLE_DEFINITION_ID")
            if foundation_plan
            else None
        ),
        resource_group=_required(environment, "AZURE_RESOURCE_GROUP"),
        location=_required(environment, "AZURE_LOCATION"),
        registry_name=(
            _required_raw_registry_name(environment, "AZURE_CONTAINER_REGISTRY_NAME")
            if runtime_composition or requires_registry_identity
            else None
        ),
        openai_resource_id=(
            _required(environment, "AZURE_OPENAI_RESOURCE_ID")
            if runtime_composition
            else None
        ),
        foundry_base_url=(
            _required(environment, "OPTIMA_FOUNDRY_BASE_URL")
            if runtime_composition
            else None
        ),
        ui_auth_client_id=(
            _required(environment, "OPTIMA_UI_AUTH_CLIENT_ID")
            if runtime_composition
            else None
        ),
        ui_auth_tenant_id=(
            _required(environment, "OPTIMA_UI_AUTH_TENANT_ID")
            if runtime_composition
            else None
        ),
        ui_auth_redirect_uri=environment.get("OPTIMA_UI_AUTH_REDIRECT_URI", "").strip(),
        ui_auth_secret_present=(
            bool(_required(environment, "OPTIMA_UI_AUTH_CLIENT_SECRET"))
            if runtime_composition
            else False
        ),
        semantic_cache_enabled=semantic_cache_enabled,
        embedding_dimension=embedding_dimension,
        models=(
            tuple(
                _model_binding(environment, role)
                for role in (
                    ("SMALL", "STRONG", "JUDGE", "EMBEDDING")
                    if semantic_cache_enabled
                    else ("SMALL", "STRONG", "JUDGE")
                )
            )
            if runtime_composition
            else ()
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


def _validate_pricing_bindings(
    configuration: DeploymentConfiguration, pricing: PricingConfiguration
) -> None:
    """Validate reviewed pricing identity, provenance, and model agreement."""
    pricing_bindings = {
        "SMALL": (pricing.small_model, pricing.small_model_version),
        "STRONG": (pricing.strong_model, pricing.strong_model_version),
        "JUDGE": (pricing.judge_model, pricing.judge_model_version),
    }
    if configuration.semantic_cache_enabled:
        if (
            configuration.embedding_dimension is None
            or pricing.embedding_model is None
            or pricing.embedding_model_version is None
            or pricing.embedding_input is None
        ):
            raise PreflightError(
                "Enabled semantic cache requires complete embedding configuration "
                "and pricing"
            )
        pricing_bindings["EMBEDDING"] = (
            pricing.embedding_model,
            pricing.embedding_model_version,
        )
    for binding in configuration.models:
        if pricing_bindings[binding.role] != (binding.model, binding.version):
            raise PreflightError(
                f"{binding.role} pricing model/version does not match its live "
                "deployment binding"
            )
    if not re.fullmatch(r"[A-Z]{3}", pricing.currency):
        raise PreflightError("Pricing currency must be a three-letter uppercase code")
    pricing_source = urlparse(pricing.source_url)
    if (
        pricing_source.scheme != "https"
        or not pricing_source.hostname
        or pricing_source.username
        or pricing_source.password
        or pricing_source.query
        or pricing_source.fragment
    ):
        raise PreflightError("OPTIMA_PRICING_SOURCE_URL must be a public HTTPS URL")
    expected_pricing_digest = pricing_binding_sha256(pricing)
    if re.fullmatch(r"[0-9a-f]{64}", pricing.binding_sha256) is None:
        raise PreflightError(
            "OPTIMA_PRICING_BINDING_SHA256 must be 64 lowercase hexadecimal characters"
        )
    if pricing.binding_sha256 != expected_pricing_digest:
        raise PreflightError(
            "OPTIMA_PRICING_BINDING_SHA256 does not match the reviewed pricing "
            f"binding; expected {expected_pricing_digest}"
        )


def validate_configuration(
    configuration: DeploymentConfiguration,
    *,
    today: date | None = None,
) -> None:
    """Validate non-Azure deployment invariants and reviewed selections.

    Runtime-composition invariants apply only to the inputs the effective phase
    supplied. Disabled-cache foundation planning leaves absent runtime bindings
    unchecked rather than fabricating them.
    """
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
    if (
        configuration.counterpart_client_id is not None
        or configuration.counterpart_identity_resource_id is not None
    ):
        if (
            configuration.counterpart_client_id is None
            or configuration.counterpart_identity_resource_id is None
        ):
            raise PreflightError("Foundation identity separation is incomplete")
        if _canonical_guid(
            configuration.deployment_client_id,
            label="selected foundation client ID",
        ) == _canonical_guid(
            configuration.counterpart_client_id,
            label="counterpart foundation client ID",
        ):
            raise PreflightError(
                "Foundation plan and apply client IDs must be distinct"
            )
        _, _, selected_identity_id = _identity_parts(
            configuration.deployment_identity_resource_id,
            subscription_id=configuration.subscription_id,
        )
        _, _, counterpart_identity_id = _identity_parts(
            configuration.counterpart_identity_resource_id,
            subscription_id=configuration.subscription_id,
        )
        if selected_identity_id == counterpart_identity_id:
            raise PreflightError(
                "Foundation plan and apply identity resource IDs must be distinct"
            )
    if (
        configuration.ui_auth_tenant_id is not None
        and configuration.ui_auth_tenant_id != configuration.tenant_id
    ):
        raise PreflightError(
            "UI authentication tenant must match the deployment tenant"
        )
    if len({binding.deployment for binding in configuration.models}) != len(
        configuration.models
    ):
        raise PreflightError("Active model-role deployments must be distinct")
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
    if configuration.pricing is not None:
        _validate_pricing_bindings(configuration, configuration.pricing)
    if configuration.foundry_base_url is not None:
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
    if (
        pricing.embedding_input is not None
        and pricing.embedding_model is not None
        and pricing.embedding_model_version is not None
    ):
        document["embedding"] = {
            "input": str(pricing.embedding_input),
            "model": pricing.embedding_model,
            "model_version": pricing.embedding_model_version,
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


def _canonical_guid(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise PreflightError(f"{label} is not a canonical GUID")
    normalized = value.casefold()
    if (
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            normalized,
        )
        is None
    ):
        raise PreflightError(f"{label} is not a canonical GUID")
    return normalized


def _canonical_graph_assignment_id(value: object) -> str:
    """Return one canonical bounded Microsoft Graph assignment identifier."""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{20,128}", value) is None
    ):
        raise PreflightError("Microsoft Graph app role assignment ID is malformed")
    try:
        decoded = base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except binascii.Error as error:
        raise PreflightError(
            "Microsoft Graph app role assignment ID is malformed"
        ) from error
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if not 16 <= len(decoded) <= 64 or canonical != value:
        raise PreflightError("Microsoft Graph app role assignment ID is malformed")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _decode_access_token_claims(access_token: object) -> dict[str, Any]:
    if not isinstance(access_token, str):
        raise PreflightError("Azure ARM access token response is malformed")
    segments = access_token.split(".")
    if len(segments) != 3 or any(
        not segment or re.fullmatch(r"[A-Za-z0-9_-]+", segment) is None
        for segment in segments
    ):
        raise PreflightError("Azure ARM access token claims are malformed")
    encoded_payload = segments[1] + ("=" * (-len(segments[1]) % 4))
    try:
        payload_bytes = base64.b64decode(
            encoded_payload,
            altchars=b"-_",
            validate=True,
        )
        claims = json.loads(
            payload_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise PreflightError("Azure ARM access token claims are malformed") from error
    if not isinstance(claims, dict):
        raise PreflightError("Azure ARM access token claims are malformed")
    return claims


def _check_account(configuration: DeploymentConfiguration, azure: AzureQuery) -> str:
    account = azure.json("account", "show")
    if not isinstance(account, dict):
        raise PreflightError("Azure account response is malformed")
    subscription_id = _canonical_guid(
        configuration.subscription_id,
        label="AZURE_SUBSCRIPTION_ID",
    )
    tenant_id = _canonical_guid(
        configuration.tenant_id,
        label="AZURE_TENANT_ID",
    )
    client_id = _canonical_guid(
        configuration.deployment_client_id,
        label="configured deployment client ID",
    )
    if _canonical_guid(account.get("id"), label="Azure CLI subscription ID") != (
        subscription_id
    ):
        raise PreflightError(
            "Azure CLI subscription does not match AZURE_SUBSCRIPTION_ID"
        )
    if _canonical_guid(account.get("tenantId"), label="Azure CLI tenant ID") != (
        tenant_id
    ):
        raise PreflightError("Azure CLI tenant does not match AZURE_TENANT_ID")
    if account.get("state") != "Enabled":
        raise PreflightError("Azure subscription is not enabled")
    user = account.get("user")
    if (
        not isinstance(user, dict)
        or not isinstance(user.get("type"), str)
        or user["type"].casefold() != "serviceprincipal"
        or _canonical_guid(user.get("name"), label="Azure CLI client identity")
        != client_id
    ):
        raise PreflightError(
            "Azure CLI session is not the configured service-principal client"
        )
    token_document = azure.json(
        "account",
        "get-access-token",
        "--resource-type",
        "arm",
        "--subscription",
        configuration.subscription_id,
    )
    if not isinstance(token_document, dict) or token_document.get("tokenType") != (
        "Bearer"
    ):
        raise PreflightError("Azure ARM access token response is malformed")
    claims = _decode_access_token_claims(token_document.get("accessToken"))
    claim_tenant = _canonical_guid(claims.get("tid"), label="ARM token tenant claim")
    claim_principal = _canonical_guid(
        claims.get("oid"),
        label="ARM token object claim",
    )
    client_claims = [
        _canonical_guid(claims[name], label=f"ARM token {name} claim")
        for name in ("appid", "azp")
        if name in claims
    ]
    if claim_tenant != tenant_id:
        raise PreflightError("Azure ARM token tenant does not match AZURE_TENANT_ID")
    if not client_claims or any(claim != client_id for claim in client_claims):
        raise PreflightError(
            "Azure ARM token client does not match the configured deployment client"
        )
    return claim_principal


def _check_providers(
    azure: AzureQuery, *, semantic_cache_enabled: bool
) -> dict[str, Any] | None:
    cache_provider: dict[str, Any] | None = None
    for namespace in REQUIRED_PROVIDERS:
        if namespace == "Microsoft.Cache" and not semantic_cache_enabled:
            continue
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
    if semantic_cache_enabled and cache_provider is None:
        raise RedisPreflightError(
            RedisPreflightErrorCode.PROVIDER_METADATA_MALFORMED,
            "Microsoft.Cache provider metadata is unavailable",
        )
    return cache_provider


def _canonical_arm_segment(value: str, *, label: str) -> str:
    if re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._()-]{0,255}", value
    ) is None or value.endswith("."):
        raise PreflightError(f"{label} contains a malformed ARM path segment")
    return value.casefold()


def _identity_parts(resource_id: str, *, subscription_id: str) -> tuple[str, str, str]:
    match = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
        r"Microsoft\.ManagedIdentity/userAssignedIdentities/([^/]+)",
        resource_id,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise PreflightError(
            "AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID is not a user-assigned "
            "managed identity resource ID"
        )
    identity_subscription = _canonical_guid(
        match.group(1),
        label="deployment identity subscription",
    )
    configured_subscription = _canonical_guid(
        subscription_id,
        label="AZURE_SUBSCRIPTION_ID",
    )
    if identity_subscription != configured_subscription:
        raise PreflightError(
            "Deployment identity subscription does not match AZURE_SUBSCRIPTION_ID"
        )
    resource_group = _canonical_arm_segment(
        match.group(2),
        label="deployment identity resource group",
    )
    identity_name = _canonical_arm_segment(
        match.group(3),
        label="deployment identity name",
    )
    canonical_id = (
        f"/subscriptions/{identity_subscription}/resourcegroups/{resource_group}/"
        "providers/microsoft.managedidentity/userassignedidentities/"
        f"{identity_name}"
    )
    return match.group(2), match.group(3), canonical_id


def _canonical_arm_scope(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.endswith("/")
        or "%" in value
        or "?" in value
        or "#" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PreflightError("OIDC deployment role assignment scope is malformed")
    segments = value.split("/")
    if (
        len(segments) >= 4
        and segments[1].casefold() == "providers"
        and segments[2].casefold() == "microsoft.management"
    ):
        if (
            len(segments) != 5
            or segments[1].casefold() != "providers"
            or segments[3].casefold() != "managementgroups"
        ):
            raise PreflightError("OIDC deployment role assignment scope is malformed")
        management_group = _canonical_arm_segment(
            segments[4],
            label="management group scope",
        )
        return f"/providers/microsoft.management/managementgroups/{management_group}"
    if len(segments) < 3 or segments[1].casefold() != "subscriptions":
        raise PreflightError("OIDC deployment role assignment scope is malformed")
    subscription_id = _canonical_guid(
        segments[2],
        label="role assignment scope subscription",
    )
    canonical = f"/subscriptions/{subscription_id}"
    if len(segments) == 3:
        return canonical
    if len(segments) < 5 or segments[3].casefold() != "resourcegroups":
        raise PreflightError("OIDC deployment role assignment scope is malformed")
    resource_group = _canonical_arm_segment(
        segments[4],
        label="role assignment resource group",
    )
    canonical += f"/resourcegroups/{resource_group}"
    if len(segments) == 5:
        return canonical
    if (
        len(segments) < 9
        or segments[5].casefold() != "providers"
        or (len(segments) - 7) % 2 != 0
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9.]+", segments[6]) is None
    ):
        raise PreflightError("OIDC deployment role assignment scope is malformed")
    canonical += f"/providers/{segments[6].casefold()}"
    for index in range(7, len(segments), 2):
        resource_type = _canonical_arm_segment(
            segments[index],
            label="role assignment resource type",
        )
        resource_name = _canonical_arm_segment(
            segments[index + 1],
            label="role assignment resource name",
        )
        canonical += f"/{resource_type}/{resource_name}"
    return canonical


def _role_definition_guid(value: object, *, subscription_id: str) -> str:
    if not isinstance(value, str):
        raise PreflightError("OIDC deployment role definition ID is malformed")
    patterns = (
        (
            r"/subscriptions/([^/]+)/providers/Microsoft\.Authorization/"
            r"roleDefinitions/([^/]+)",
            True,
        ),
        (
            r"/providers/Microsoft\.Authorization/roleDefinitions/([^/]+)",
            False,
        ),
        (
            r"/providers/Microsoft\.Management/managementGroups/([^/]+)/providers/"
            r"Microsoft\.Authorization/roleDefinitions/([^/]+)",
            False,
        ),
    )
    for pattern, has_subscription in patterns:
        match = re.fullmatch(pattern, value, flags=re.IGNORECASE)
        if match is None:
            continue
        role_id = match.group(2 if has_subscription else match.lastindex or 1)
        if has_subscription and _canonical_guid(
            match.group(1),
            label="role definition subscription",
        ) != _canonical_guid(subscription_id, label="AZURE_SUBSCRIPTION_ID"):
            raise PreflightError(
                "OIDC deployment role definition belongs to another subscription"
            )
        return _canonical_guid(role_id, label="OIDC deployment role definition ID")
    raise PreflightError("OIDC deployment role definition ID is malformed")


def _effective_role_assignments(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    *,
    principal_id: str,
) -> tuple[EffectiveRoleAssignment, ...]:
    subscription_scope = f"/subscriptions/{configuration.subscription_id}"
    query_arguments = (
        (
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            principal_id,
            "--all",
            "--fill-principal-name",
            "false",
        ),
        (
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            principal_id,
            "--scope",
            subscription_scope,
            "--include-inherited",
            "--fill-principal-name",
            "false",
        ),
    )
    parsed: list[EffectiveRoleAssignment] = []
    seen: set[EffectiveRoleAssignment] = set()
    for arguments in query_arguments:
        assignments = azure.json(*arguments)
        if not isinstance(assignments, list):
            raise PreflightError(
                "OIDC deployment role assignment response is malformed"
            )
        query_seen: set[EffectiveRoleAssignment] = set()
        for assignment in assignments:
            if not isinstance(assignment, dict):
                raise PreflightError(
                    "OIDC deployment role assignment response is malformed"
                )
            principal_type = assignment.get("principalType")
            if isinstance(principal_type, str) and principal_type.casefold() == "group":
                raise PreflightError(
                    "OIDC deployment identity has a group-derived role"
                )
            if (
                not isinstance(principal_type, str)
                or principal_type.casefold() != "serviceprincipal"
                or _canonical_guid(
                    assignment.get("principalId"),
                    label="role assignment principal ID",
                )
                != principal_id
            ):
                raise PreflightError(
                    "OIDC deployment role assignment principal is malformed"
                )
            parsed_assignment = EffectiveRoleAssignment(
                role_definition_id=_role_definition_guid(
                    assignment.get("roleDefinitionId"),
                    subscription_id=configuration.subscription_id,
                ),
                scope=_canonical_arm_scope(assignment.get("scope")),
            )
            if parsed_assignment in query_seen:
                raise PreflightError(
                    "OIDC deployment identity has duplicate role assignments"
                )
            query_seen.add(parsed_assignment)
            if parsed_assignment not in seen:
                seen.add(parsed_assignment)
                parsed.append(parsed_assignment)
    return tuple(parsed)


def _transitive_group_ids(azure: AzureQuery, *, principal_id: str) -> tuple[str, ...]:
    """Return bounded transitive Microsoft Entra group membership evidence."""
    document = azure.json(
        "rest",
        "--method",
        "get",
        "--url",
        (
            f"https://{MICROSOFT_GRAPH_HOST}/v1.0/servicePrincipals/"
            f"{principal_id}/transitiveMemberOf/microsoft.graph.group"
            "?$select=id&$top=999&$count=true"
        ),
        "--headers",
        "ConsistencyLevel=eventual",
        "--resource",
        "https://graph.microsoft.com/",
    )
    if not isinstance(document, dict) or set(document) - {
        "@odata.count",
        "@odata.context",
        "@odata.nextLink",
        "value",
    }:
        raise PreflightError("Microsoft Graph transitive membership is malformed")
    if document.get("@odata.nextLink") not in (None, ""):
        raise PreflightError("Microsoft Graph transitive membership exceeds the limit")
    values = document.get("value")
    count = document.get("@odata.count")
    if (
        not isinstance(values, list)
        or len(values) > MAX_TRANSITIVE_GROUPS
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count != len(values)
    ):
        raise PreflightError("Microsoft Graph transitive membership is malformed")
    group_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or set(value) - {"@odata.type", "id"}:
            raise PreflightError("Microsoft Graph transitive membership is malformed")
        if value.get("@odata.type") not in (None, "#microsoft.graph.group"):
            raise PreflightError("Microsoft Graph transitive membership is malformed")
        group_id = _canonical_guid(
            value.get("id"),
            label="Microsoft Graph transitive group ID",
        )
        if group_id in seen:
            raise PreflightError(
                "Microsoft Graph transitive membership contains duplicate groups"
            )
        seen.add(group_id)
        group_ids.append(group_id)
    return tuple(sorted(group_ids))


def _reject_transitive_group_roles(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    *,
    principal_id: str,
) -> None:
    """Reject any Azure role inherited through a transitive Entra group."""
    subscription_scope = f"/subscriptions/{configuration.subscription_id}"
    for group_id in _transitive_group_ids(azure, principal_id=principal_id):
        for arguments in (
            (
                "role",
                "assignment",
                "list",
                "--assignee-object-id",
                group_id,
                "--all",
                "--fill-principal-name",
                "false",
            ),
            (
                "role",
                "assignment",
                "list",
                "--assignee-object-id",
                group_id,
                "--scope",
                subscription_scope,
                "--include-inherited",
                "--fill-principal-name",
                "false",
            ),
        ):
            assignments = azure.json(*arguments)
            if not isinstance(assignments, list):
                raise PreflightError(
                    "Transitive group role assignment response is malformed"
                )
            if assignments:
                raise PreflightError(
                    "OIDC deployment identity has a group-derived Azure role"
                )


def _closed_permission_list(permission: Mapping[str, Any], field: str) -> list[str]:
    value = permission.get(field)
    if not isinstance(value, list) or any(
        not isinstance(action, str) or not action.strip() for action in value
    ):
        raise PreflightError(
            "Foundation plan role definition permissions are malformed"
        )
    normalized = [action.strip().casefold() for action in value]
    if len(normalized) != len(set(normalized)):
        raise PreflightError(
            "Foundation plan role definition permissions are malformed"
        )
    return normalized


def _check_foundation_plan_role_definition(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    *,
    subscription_scope: str,
    resource_group_scope: str,
) -> None:
    role_id = configuration.foundation_plan_role_definition_id
    if role_id is None:
        raise PreflightError("Foundation plan role definition ID is unavailable")
    definitions = azure.json(
        "role",
        "definition",
        "list",
        "--name",
        role_id,
        "--custom-role-only",
        "true",
    )
    if (
        not isinstance(definitions, list)
        or len(definitions) != 1
        or not isinstance(definitions[0], dict)
    ):
        raise PreflightError(
            "Foundation plan role definition is unreadable or malformed"
        )
    definition = definitions[0]
    if (
        definition.get("roleType") != "CustomRole"
        or _role_definition_guid(
            definition.get("id"),
            subscription_id=configuration.subscription_id,
        )
        != role_id
    ):
        raise PreflightError("Foundation plan role definition identity is malformed")
    permissions = definition.get("permissions")
    if (
        not isinstance(permissions, list)
        or len(permissions) != 1
        or not isinstance(permissions[0], dict)
    ):
        raise PreflightError(
            "Foundation plan role definition permissions are malformed"
        )
    permission = permissions[0]
    required_fields = {"actions", "notActions", "dataActions", "notDataActions"}
    optional_null_fields = {"condition", "conditionVersion"}
    permission_fields = set(permission)
    if (
        not required_fields
        <= permission_fields
        <= required_fields | optional_null_fields
        or any(permission.get(field) is not None for field in optional_null_fields)
    ):
        raise PreflightError(
            "Foundation plan role definition permissions are malformed"
        )
    actions = _closed_permission_list(permission, "actions")
    if actions != ["microsoft.resources/deployments/whatif/action"] or any(
        _closed_permission_list(permission, field)
        for field in ("notActions", "dataActions", "notDataActions")
    ):
        raise PreflightError(
            "Foundation plan role must allow only deployments what-if action"
        )
    assignable_scopes = definition.get("assignableScopes")
    if not isinstance(assignable_scopes, list) or any(
        not isinstance(scope, str) for scope in assignable_scopes
    ):
        raise PreflightError("Foundation plan role assignable scopes are malformed")
    canonical_scopes = [_canonical_arm_scope(scope) for scope in assignable_scopes]
    if canonical_scopes != [resource_group_scope]:
        raise PreflightError(
            "Foundation plan role is not assignable only to the target resource group"
        )


def _resolve_graph_service_principal(azure: AzureQuery) -> tuple[str, frozenset[str]]:
    """Return the Microsoft Graph service-principal ID and enabled app roles.

    The resource identity for every Graph application permission must be the
    Microsoft Graph first-party service principal. The returned app-role set is
    limited to enabled application-membership roles so a granted permission can
    be proven to be a real, enabled application permission.
    """
    document = azure.json(
        "rest",
        "--method",
        "get",
        "--url",
        (
            f"https://{MICROSOFT_GRAPH_HOST}/v1.0/servicePrincipals(appId="
            f"'{MICROSOFT_GRAPH_APP_ID}')?$select=id,appId,appRoles"
        ),
        "--resource",
        "https://graph.microsoft.com/",
    )
    if not isinstance(document, dict) or set(document) - {
        "@odata.context",
        "id",
        "appId",
        "appRoles",
    }:
        raise PreflightError("Microsoft Graph service principal is malformed")
    if document.get("appId") != MICROSOFT_GRAPH_APP_ID:
        raise PreflightError("Resolved Microsoft Graph service principal is unexpected")
    graph_principal_id = _canonical_guid(
        document.get("id"),
        label="Microsoft Graph service principal ID",
    )
    app_roles = document.get("appRoles")
    if not isinstance(app_roles, list):
        raise PreflightError("Microsoft Graph application roles are malformed")
    enabled_application_roles: set[str] = set()
    for app_role in app_roles:
        if not isinstance(app_role, dict):
            raise PreflightError("Microsoft Graph application roles are malformed")
        member_types = app_role.get("allowedMemberTypes")
        if not isinstance(member_types, list) or any(
            not isinstance(member_type, str) for member_type in member_types
        ):
            raise PreflightError("Microsoft Graph application roles are malformed")
        if app_role.get("isEnabled") is True and "Application" in member_types:
            enabled_application_roles.add(
                _canonical_guid(
                    app_role.get("id"),
                    label="Microsoft Graph application role ID",
                )
            )
    return graph_principal_id, frozenset(enabled_application_roles)


def _check_graph_application_permissions(
    azure: AzureQuery, *, principal_id: str
) -> None:
    """Verify the identity holds exactly Application.Read.All on Microsoft Graph.

    Proving Graph access indirectly is insufficient: the executing deployment
    identity must carry exactly one Microsoft Graph application permission and
    it must be the approved Application.Read.All app role, granted on the real
    Graph service principal. Additional Graph application permissions such as
    Directory.Read.All or Application.ReadWrite.All are rejected. Non-Graph
    enterprise-application assignments are outside this check.
    """
    graph_principal_id, enabled_application_roles = _resolve_graph_service_principal(
        azure
    )
    document = azure.json(
        "rest",
        "--method",
        "get",
        "--url",
        (
            f"https://{MICROSOFT_GRAPH_HOST}/v1.0/servicePrincipals/"
            f"{principal_id}/appRoleAssignments"
            "?$select=id,appRoleId,principalId,resourceId&$top=999&$count=true"
        ),
        "--headers",
        "ConsistencyLevel=eventual",
        "Accept=application/json;odata.metadata=full",
        "--resource",
        "https://graph.microsoft.com/",
    )
    if not isinstance(document, dict) or set(document) - {
        "@odata.count",
        "@odata.context",
        "@odata.nextLink",
        "value",
    }:
        raise PreflightError("Microsoft Graph app role assignments are malformed")
    if document.get("@odata.nextLink") not in (None, ""):
        raise PreflightError("Microsoft Graph app role assignments exceed the limit")
    values = document.get("value")
    count = document.get("@odata.count")
    if (
        not isinstance(values, list)
        or len(values) > MAX_GRAPH_APP_ROLE_ASSIGNMENTS
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count != len(values)
    ):
        raise PreflightError("Microsoft Graph app role assignments are malformed")
    graph_role_ids: list[str] = []
    assignment_ids: set[str] = set()
    for value in values:
        required_fields = {
            "@odata.type",
            "id",
            "appRoleId",
            "principalId",
            "resourceId",
        }
        allowed_fields = required_fields | {
            "@odata.editLink",
            "@odata.id",
            "appRoleId@odata.type",
            "principalId@odata.type",
            "resourceId@odata.type",
        }
        if (
            not isinstance(value, dict)
            or not required_fields <= set(value) <= allowed_fields
            or value.get("@odata.type") != "#microsoft.graph.appRoleAssignment"
        ):
            raise PreflightError("Microsoft Graph app role assignment is malformed")
        # Validate every structural field before deciding whether the assignment
        # targets Microsoft Graph. A malformed unrelated assignment must fail
        # closed rather than be silently filtered out of the equality check.
        resource_id = _canonical_guid(
            value.get("resourceId"),
            label="Microsoft Graph app role assignment resource",
        )
        assigned_principal_id = _canonical_guid(
            value.get("principalId"),
            label="Microsoft Graph app role assignment principal",
        )
        assigned_role_id = _canonical_guid(
            value.get("appRoleId"),
            label="Microsoft Graph app role ID",
        )
        assignment_id = _canonical_graph_assignment_id(value.get("id"))
        if assignment_id in assignment_ids:
            raise PreflightError(
                "Microsoft Graph app role assignments contain duplicate IDs"
            )
        assignment_ids.add(assignment_id)
        for annotation in (
            "appRoleId@odata.type",
            "principalId@odata.type",
            "resourceId@odata.type",
        ):
            if annotation in value and value[annotation] != "#Guid":
                raise PreflightError("Microsoft Graph app role assignment is malformed")
        for link in ("@odata.id", "@odata.editLink"):
            if link in value and (not isinstance(value[link], str) or not value[link]):
                raise PreflightError("Microsoft Graph app role assignment is malformed")
        if assigned_principal_id != principal_id:
            raise PreflightError(
                "Microsoft Graph app role assignment targets another principal"
            )
        if resource_id != graph_principal_id:
            # A structurally valid enterprise-application permission on another
            # resource is outside the Microsoft Graph application-permission
            # contract and is ignored only after full validation.
            continue
        graph_role_ids.append(assigned_role_id)
    if len(graph_role_ids) != 1:
        raise PreflightError(
            "OIDC deployment identity must have exactly the Application.Read.All "
            "Microsoft Graph permission"
        )
    if graph_role_ids[0] != GRAPH_APPLICATION_READ_ALL_ROLE_ID:
        raise PreflightError(
            "OIDC deployment identity Microsoft Graph permission is not "
            "Application.Read.All"
        )
    if GRAPH_APPLICATION_READ_ALL_ROLE_ID not in enabled_application_roles:
        raise PreflightError(
            "Application.Read.All is not an enabled Microsoft Graph application "
            "permission"
        )


def _validate_effective_production_parameters(
    configuration: DeploymentConfiguration,
    parameters: Mapping[str, Any],
    *,
    expected_commit_sha: str,
) -> None:
    """Bind the effective ARM parameters to the protected runtime configuration."""
    models = {binding.role: binding for binding in configuration.models}
    pricing = configuration.pricing
    if (
        configuration.ui_auth_client_id is None
        or configuration.ui_auth_tenant_id is None
        or configuration.foundry_base_url is None
        or pricing is None
        or set(models) < {"SMALL", "STRONG", "JUDGE"}
    ):
        raise PreflightError(
            "Production-foundation runtime configuration is incomplete"
        )
    expected: dict[str, Any] = {
        "deploymentCommitSha": expected_commit_sha,
        "deployContainerApps": False,
        "deployRuntimeAccess": False,
        "environmentName": EXPECTED_ENVIRONMENT,
        "exposePublicUi": False,
        "foundryBaseUrl": configuration.foundry_base_url,
        "foundrySmallDeployment": models["SMALL"].deployment,
        "foundrySmallModel": models["SMALL"].model,
        "foundrySmallModelVersion": models["SMALL"].version,
        "foundryStrongDeployment": models["STRONG"].deployment,
        "foundryStrongModel": models["STRONG"].model,
        "foundryStrongModelVersion": models["STRONG"].version,
        "judgeDeployment": models["JUDGE"].deployment,
        "judgeModel": models["JUDGE"].model,
        "judgeModelVersion": models["JUDGE"].version,
        "location": configuration.location,
        "pricingCatalogVersion": pricing.catalog_version,
        "pricingCurrency": pricing.currency,
        "pricingJudgeCachedInputRatePerMillionTokens": (
            str(pricing.judge_cached_input)
            if pricing.judge_cached_input is not None
            else None
        ),
        "pricingJudgeInputRatePerMillionTokens": str(pricing.judge_input),
        "pricingJudgeOutputRatePerMillionTokens": str(pricing.judge_output),
        "pricingSmallCachedInputRatePerMillionTokens": (
            str(pricing.small_cached_input)
            if pricing.small_cached_input is not None
            else None
        ),
        "pricingSmallInputRatePerMillionTokens": str(pricing.small_input),
        "pricingSmallOutputRatePerMillionTokens": str(pricing.small_output),
        "pricingStrongCachedInputRatePerMillionTokens": (
            str(pricing.strong_cached_input)
            if pricing.strong_cached_input is not None
            else None
        ),
        "pricingStrongInputRatePerMillionTokens": str(pricing.strong_input),
        "pricingStrongOutputRatePerMillionTokens": str(pricing.strong_output),
        "productionEvaluatorMode": "LLM_JUDGE",
        "semanticCacheEnabled": configuration.semantic_cache_enabled,
        "uiAuthClientId": configuration.ui_auth_client_id,
        "uiAuthTenantId": configuration.ui_auth_tenant_id,
    }
    if configuration.semantic_cache_enabled:
        embedding = models.get("EMBEDDING")
        if (
            embedding is None
            or configuration.embedding_dimension is None
            or pricing.embedding_input is None
        ):
            raise PreflightError(
                "Enabled semantic cache runtime parameters are incomplete"
            )
        expected.update(
            {
                "pricingEmbeddingInputRatePerMillionTokens": str(
                    pricing.embedding_input
                ),
                "redisEmbeddingDeployment": embedding.deployment,
                "redisEmbeddingDimension": configuration.embedding_dimension,
                "redisEmbeddingModel": embedding.model,
            }
        )
    if any(parameters.get(name) != value for name, value in expected.items()):
        raise PreflightError(
            "Effective runtime parameters do not match protected configuration"
        )
    workflow_run_id = parameters.get("deploymentWorkflowRunId")
    if (
        not isinstance(workflow_run_id, str)
        or re.fullmatch(r"[1-9][0-9]{0,19}-[1-9][0-9]{0,19}", workflow_run_id) is None
    ):
        raise PreflightError("Effective runtime workflow identity is malformed")


def _require_converged_foundation(
    evidence: Mapping[str, Any], *, semantic_cache_enabled: bool
) -> None:
    """Fail closed unless classified evidence proves an applied, converged foundation.

    Production requires an already applied foundation. An ARM deployment that
    merely reports ``Succeeded`` is never sufficient on its own: the fresh,
    source-bound what-if reconstruction must show zero effective Creates and the
    exact managed resource count as NoChange, with no unapproved residual
    change. Evidence describing resource creation -- an unapplied foundation --
    is rejected here so a bare Succeeded deployment name cannot authorize
    production mutation. Delete, replacement, unapproved Modify, unexpected
    resource types, the exact provider-echo normalizations, the LAW NoEffect
    exception, and the policy-bound external observation count are all already
    enforced by the fail-closed classifier that produced this evidence.
    """
    expected_nochange = (
        whatif_classification._CACHE_MANAGED_FOUNDATION_RESOURCE_COUNT
        if semantic_cache_enabled
        else whatif_classification._BASE_MANAGED_FOUNDATION_RESOURCE_COUNT
    )
    counts = evidence["changes"]["counts"]
    if counts.get("Create", 0) != 0:
        raise PreflightError(
            "Production requires a converged foundation; classified evidence still "
            "reports resource creation"
        )
    if counts.get("NoChange", 0) != expected_nochange:
        raise PreflightError(
            "Classified foundation evidence does not report the exact converged "
            "managed resource count"
        )
    residual = evidence["normalizations"]["residual_unapproved_change_count"]
    if not isinstance(residual, int) or isinstance(residual, bool) or residual != 0:
        raise PreflightError(
            "Classified foundation evidence reports unapproved residual changes"
        )


def _resolve_repository_registry_identity(
    configuration: DeploymentConfiguration,
    *,
    classified_evidence: Path | None,
    classified_evidence_sha256: str | None,
    raw_whatif: Path | None,
    raw_whatif_sha256: str | None,
    effective_parameters: Path | None,
    effective_parameters_sha256: str | None,
    expected_commit_sha: str | None,
    repository_root: Path,
) -> str:
    """Recompute evidence and return its ARM-evaluated registry identity.

    The repository/Bicep-derived registry identity is the single
    ``container_registry`` fact emitted by the fail-closed classifier over a
    fresh, source-bound foundation what-if. ARM evaluates the exact reviewed
    ``uniqueString(subscription, environment)`` expression, so this identity is
    independent of the mutable configured name, live inventory, and the AcrPush
    scope. Every binding field -- schema, classification, commit SHA,
    deployment mode, target scope, source fingerprint, parameter fingerprint,
    convergence-policy fingerprint, external-policy fingerprint, residual
    change count, and resource cardinality -- is re-derived here and compared
    before the ACR fact is trusted. A plain command-line registry name is never
    accepted as proof.
    """
    if (
        classified_evidence is None
        or classified_evidence_sha256 is None
        or raw_whatif is None
        or raw_whatif_sha256 is None
        or effective_parameters is None
        or effective_parameters_sha256 is None
        or expected_commit_sha is None
    ):
        raise PreflightError(
            "Production-foundation preflight requires complete foundation evidence"
        )
    try:
        evidence_bytes = whatif_classification.read_regular_file(
            classified_evidence,
            maximum_bytes=whatif_classification.MAX_EFFECTIVE_PARAMETER_FILE_BYTES,
            expected_sha256=classified_evidence_sha256,
        )
        evidence_document = whatif_classification._validate_evidence(
            whatif_classification.parse_strict_json(
                evidence_bytes,
                code=whatif_classification.WhatIfClassificationCode.PROMOTION_MISMATCH,
                label=classified_evidence.name,
            )
        )
        raw_bytes = whatif_classification.read_regular_file(
            raw_whatif,
            expected_sha256=raw_whatif_sha256,
        )
        raw_document = whatif_classification.parse_strict_json(
            raw_bytes,
            code=whatif_classification.WhatIfClassificationCode.MALFORMED_DOCUMENT,
            label=raw_whatif.name,
        )
        parameter_bytes = whatif_classification.read_regular_file(
            effective_parameters,
            maximum_bytes=whatif_classification.MAX_EFFECTIVE_PARAMETER_FILE_BYTES,
            expected_sha256=effective_parameters_sha256,
        )
        parameters = whatif_classification.parse_foundation_parameters(
            parameter_bytes,
            label=effective_parameters.name,
            template_file=FOUNDATION_TEMPLATE_FILE,
            parameter_source_file=FOUNDATION_PARAMETER_FILE,
            resource_group=configuration.resource_group,
        )
        _validate_effective_production_parameters(
            configuration,
            parameters,
            expected_commit_sha=expected_commit_sha,
        )
        external_policy = whatif_classification._external_policy_from_environment(
            whatif_classification.EXTERNAL_POLICY_ENV
        )
        recomputed = whatif_classification.build_evidence_from_whatif(
            raw_document,
            parameters=parameters,
            source_root=repository_root,
            subscription_id=configuration.subscription_id,
            resource_group=configuration.resource_group,
            commit_sha=expected_commit_sha,
            deployment_mode="Incremental",
            external_policy=external_policy,
        )
    except (
        OSError,
        ValueError,
        whatif_classification.WhatIfClassificationError,
    ) as error:
        raise PreflightError(
            "Foundation authorization artifacts are missing, changed, or malformed"
        ) from error
    canonical_recomputed = whatif_classification.serialize_evidence(recomputed)
    if evidence_document != recomputed or evidence_bytes != canonical_recomputed:
        raise PreflightError(
            "Classified foundation evidence differs from raw what-if reconstruction"
        )
    _require_converged_foundation(
        recomputed, semantic_cache_enabled=configuration.semantic_cache_enabled
    )
    registry_facts = [
        fact
        for fact in recomputed["changes"]["resources"]
        if fact["resource_role"] == CONTAINER_REGISTRY_RESOURCE_ROLE
    ]
    if len(registry_facts) != 1:
        raise PreflightError(
            "Classified foundation evidence must contain exactly one container "
            "registry fact"
        )
    registry_fact = registry_facts[0]
    if registry_fact["resource_type"] != ACR_REGISTRY_RESOURCE_TYPE:
        raise PreflightError(
            "Classified foundation registry fact has an unexpected resource type"
        )
    registry_name = registry_fact["resource_name"]
    if (
        not isinstance(registry_name, str)
        or ACR_NAME_PATTERN.fullmatch(registry_name) is None
    ):
        raise PreflightError("Classified foundation registry name is malformed")
    return registry_name


def _resolve_approved_registry_scope(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    *,
    resource_group_scope: str,
    expected_registry_name: str,
) -> str:
    """Return the canonical AcrPush scope agreed by all four trusted sources.

    ``expected_registry_name`` is the repository/Bicep-derived identity from the
    fail-closed classifier over a fresh ARM-evaluated foundation what-if. The
    configured name, the single live Container Registry in the approved resource
    group, and the returned AcrPush scope must all agree canonically with that
    identity. Because the trusted name is never derived from configuration, live
    inventory, or the role assignment, a self-consistent alternate registry
    across those three sources still fails closed.
    """
    if configuration.registry_name is None:
        raise PreflightError("Container registry name is unavailable")
    validated_name = _validated_registry_name(configuration.registry_name)
    if validated_name.casefold() != expected_registry_name.casefold():
        raise PreflightError(
            "Configured container registry does not match the repository-derived "
            "registry identity"
        )
    registries = azure.json(
        "acr",
        "list",
        "--resource-group",
        configuration.resource_group,
    )
    if not isinstance(registries, list):
        raise PreflightError("Azure Container Registry inventory response is malformed")
    typed_registries = [
        registry for registry in registries if isinstance(registry, dict)
    ]
    if len(typed_registries) != len(registries):
        raise PreflightError("Azure Container Registry inventory response is malformed")
    container_registries = [
        registry
        for registry in typed_registries
        if str(registry.get("type", "")).casefold() == ACR_REGISTRY_RESOURCE_TYPE
    ]
    if len(container_registries) != len(typed_registries):
        raise PreflightError(
            "Azure Container Registry inventory contains a non-registry resource"
        )
    if len(container_registries) != 1:
        raise PreflightError(
            "OPTIMA resource group must contain exactly one Azure Container Registry"
        )
    registry = container_registries[0]
    provisioning_state = registry.get("provisioningState")
    if (
        not isinstance(provisioning_state, str)
        or provisioning_state.casefold() != "succeeded"
    ):
        raise PreflightError(
            "Approved Azure Container Registry is not fully provisioned"
        )
    live_name = registry.get("name")
    if not isinstance(live_name, str) or ACR_NAME_PATTERN.fullmatch(live_name) is None:
        raise PreflightError("Approved Azure Container Registry name is malformed")
    if live_name.casefold() != expected_registry_name.casefold():
        raise PreflightError(
            "Approved live container registry does not match the repository-derived "
            "registry identity"
        )
    expected_scope = _canonical_arm_scope(
        f"{resource_group_scope}/providers/Microsoft.ContainerRegistry/"
        f"registries/{expected_registry_name}"
    )
    if not expected_scope.startswith(f"{resource_group_scope}/"):
        raise PreflightError("Approved Azure Container Registry scope is malformed")
    live_scope = _canonical_arm_scope(registry.get("id"))
    if live_scope != expected_scope:
        raise PreflightError(
            "Approved Azure Container Registry resource ID does not match the "
            "managed identity"
        )
    return live_scope


def _check_deployment_role_allowlist(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    *,
    phase: str,
    principal_id: str,
    repository_root: Path,
    classified_evidence: Path | None,
    classified_evidence_sha256: str | None,
    raw_whatif: Path | None,
    raw_whatif_sha256: str | None,
    effective_parameters: Path | None,
    effective_parameters_sha256: str | None,
    expected_commit_sha: str | None,
) -> None:
    _check_graph_application_permissions(azure, principal_id=principal_id)
    _reject_transitive_group_roles(
        configuration,
        azure,
        principal_id=principal_id,
    )
    assignments = set(
        _effective_role_assignments(
            configuration,
            azure,
            principal_id=principal_id,
        )
    )
    subscription_scope = _canonical_arm_scope(
        f"/subscriptions/{configuration.subscription_id}"
    )
    resource_group_scope = _canonical_arm_scope(
        f"/subscriptions/{configuration.subscription_id}/resourceGroups/"
        f"{configuration.resource_group}"
    )
    if phase == "foundation-plan":
        plan_role_id = configuration.foundation_plan_role_definition_id
        if plan_role_id is None:
            raise PreflightError("Foundation plan role definition ID is unavailable")
        expected = {
            EffectiveRoleAssignment(READER_ROLE_ID, subscription_scope),
            EffectiveRoleAssignment(plan_role_id, resource_group_scope),
        }
    elif phase in {"foundation", "foundation-apply"}:
        group = azure.json(
            "group",
            "show",
            "--name",
            configuration.resource_group,
            allow_missing=True,
        )
        if group is None:
            if phase == "foundation-apply":
                raise PreflightError(
                    "Separated foundation apply requires the target resource group"
                )
            expected = {
                EffectiveRoleAssignment(CONTRIBUTOR_ROLE_ID, subscription_scope)
            }
        else:
            expected = {
                EffectiveRoleAssignment(READER_ROLE_ID, subscription_scope),
                EffectiveRoleAssignment(CONTRIBUTOR_ROLE_ID, resource_group_scope),
            }
    elif phase in {"production-foundation", "publish", "artifacts", "rollout"}:
        # production-foundation and the runtime-composition phases require the
        # exact three-role deployer contract. AcrPush binds to the registry
        # identity that the fail-closed classifier derived from a fresh,
        # ARM-evaluated foundation what-if, cross-agreed with configuration,
        # live inventory, and the AcrPush scope -- never the configured name,
        # inventory, or role assignment alone.
        expected_registry_name = _resolve_repository_registry_identity(
            configuration,
            classified_evidence=classified_evidence,
            classified_evidence_sha256=classified_evidence_sha256,
            raw_whatif=raw_whatif,
            raw_whatif_sha256=raw_whatif_sha256,
            effective_parameters=effective_parameters,
            effective_parameters_sha256=effective_parameters_sha256,
            expected_commit_sha=expected_commit_sha,
            repository_root=repository_root,
        )
        registry_scope = _resolve_approved_registry_scope(
            configuration,
            azure,
            resource_group_scope=resource_group_scope,
            expected_registry_name=expected_registry_name,
        )
        expected = {
            EffectiveRoleAssignment(READER_ROLE_ID, subscription_scope),
            EffectiveRoleAssignment(CONTRIBUTOR_ROLE_ID, resource_group_scope),
            EffectiveRoleAssignment(ACR_PUSH_ROLE_ID, registry_scope),
        }
        if EffectiveRoleAssignment(ACR_PUSH_ROLE_ID, registry_scope) not in assignments:
            raise PreflightError(
                "OIDC deployment identity lacks AcrPush on the OPTIMA registry"
            )
    else:
        raise PreflightError(f"Unsupported preflight role phase {phase}")
    if assignments != expected:
        if any(
            assignment.role_definition_id in FORBIDDEN_DEPLOYMENT_ROLE_IDS
            for assignment in assignments
        ):
            raise PreflightError(
                "OIDC deployment identity has a forbidden Owner or RBAC "
                "administration role"
            )
        if any(
            assignment.role_definition_id == CONTRIBUTOR_ROLE_ID
            and assignment not in expected
            for assignment in assignments
        ):
            raise PreflightError(
                "OIDC deployment identity must have Contributor only at the "
                "approved scope"
            )
        raise PreflightError(
            "OIDC deployment identity must have exactly the approved effective roles"
        )
    if phase == "foundation-plan":
        _check_foundation_plan_role_definition(
            configuration,
            azure,
            subscription_scope=subscription_scope,
            resource_group_scope=resource_group_scope,
        )


def _check_oidc_federation(
    configuration: DeploymentConfiguration,
    azure: AzureQuery,
    *,
    phase: str,
    session_principal_id: str,
    repository_root: Path,
    classified_evidence: Path | None,
    classified_evidence_sha256: str | None,
    raw_whatif: Path | None,
    raw_whatif_sha256: str | None,
    effective_parameters: Path | None,
    effective_parameters_sha256: str | None,
    expected_commit_sha: str | None,
    verify_roles: bool = True,
) -> None:
    resource_group, identity_name, configured_identity_id = _identity_parts(
        configuration.deployment_identity_resource_id,
        subscription_id=configuration.subscription_id,
    )
    identity = azure.json(
        "identity",
        "show",
        "--resource-group",
        resource_group,
        "--name",
        identity_name,
    )
    if not isinstance(identity, dict):
        raise PreflightError("OIDC deployment identity response is malformed")
    _, _, returned_identity_id = _identity_parts(
        identity.get("id", ""),
        subscription_id=configuration.subscription_id,
    )
    identity_client_id = _canonical_guid(
        identity.get("clientId"),
        label="OIDC deployment identity client ID",
    )
    identity_principal_id = _canonical_guid(
        identity.get("principalId"),
        label="OIDC deployment identity principal ID",
    )
    if returned_identity_id != configured_identity_id:
        raise PreflightError(
            "Returned OIDC deployment identity ID does not match configured identity"
        )
    if identity_client_id != _canonical_guid(
        configuration.deployment_client_id,
        label="configured deployment client ID",
    ):
        raise PreflightError("OIDC deployment identity does not match client ID")
    if identity_principal_id != session_principal_id:
        raise PreflightError(
            "Azure ARM token object does not match deployment identity principal"
        )
    credentials = azure.json(
        "identity",
        "federated-credential",
        "list",
        "--resource-group",
        resource_group,
        "--identity-name",
        identity_name,
    )
    try:
        oidc_federation.validate_federated_credentials(
            credentials,
            identity_resource_id=configuration.deployment_identity_resource_id,
        )
    except oidc_federation.FederationError as error:
        raise PreflightError(
            "GitHub environment federated credential is missing or mismatched"
        ) from error
    if not verify_roles:
        return
    _check_deployment_role_allowlist(
        configuration,
        azure,
        phase=phase,
        principal_id=identity_principal_id,
        repository_root=repository_root,
        classified_evidence=classified_evidence,
        classified_evidence_sha256=classified_evidence_sha256,
        raw_whatif=raw_whatif,
        raw_whatif_sha256=raw_whatif_sha256,
        effective_parameters=effective_parameters,
        effective_parameters_sha256=effective_parameters_sha256,
        expected_commit_sha=expected_commit_sha,
    )


def _check_ui_authentication(
    configuration: DeploymentConfiguration, azure: AzureQuery
) -> None:
    if not configuration.ui_auth_redirect_uri:
        raise PreflightError(
            "OPTIMA_UI_AUTH_REDIRECT_URI is required after foundation provisioning"
        )
    if configuration.ui_auth_client_id is None:
        raise PreflightError(
            "OPTIMA_UI_AUTH_CLIENT_ID is required to verify UI authentication"
        )
    ui_auth_client_id = configuration.ui_auth_client_id
    application = azure.json("ad", "app", "show", "--id", ui_auth_client_id)
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
    service_principal = azure.json("ad", "sp", "show", "--id", ui_auth_client_id)
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
    if (
        configuration.openai_resource_id is None
        or configuration.foundry_base_url is None
    ):
        raise PreflightError(
            "Azure OpenAI runtime bindings are required to verify model deployments"
        )
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
            "semanticCacheEnabled",
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
    if not require_foundation and configuration.semantic_cache_enabled:
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
    if (
        not configuration.semantic_cache_enabled
        and counts.get(REDIS_FOUNDATION_RESOURCE_TYPE, 0) != 0
    ):
        raise PreflightError(
            "Disabled semantic cache requires Azure Managed Redis to be absent"
        )
    required_resource_types = dict(REQUIRED_FOUNDATION_RESOURCE_TYPES)
    if configuration.semantic_cache_enabled:
        required_resource_types[REDIS_FOUNDATION_RESOURCE_TYPE] = 1
    if not require_foundation:
        return typed_resources
    for resource_type, minimum in required_resource_types.items():
        if counts.get(resource_type, 0) < minimum:
            raise PreflightError(
                f"Required foundation resource type {resource_type} is missing"
            )
    return typed_resources


def _check_acr_push(configuration: DeploymentConfiguration, azure: AzureQuery) -> None:
    if configuration.registry_name is None:
        raise PreflightError(
            "Container registry name is required to verify AcrPush publication access"
        )
    identity_resource_group, identity_name, _ = _identity_parts(
        configuration.deployment_identity_resource_id,
        subscription_id=configuration.subscription_id,
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
    if not isinstance(assignments, list):
        raise PreflightError("ACR role assignment response is malformed")
    registry_scope = _canonical_arm_scope(registry_id)
    has_acr_push = False
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise PreflightError("ACR role assignment response is malformed")
        has_acr_push = has_acr_push or (
            _role_definition_guid(
                assignment.get("roleDefinitionId"),
                subscription_id=configuration.subscription_id,
            )
            == ACR_PUSH_ROLE_ID
            and _canonical_arm_scope(assignment.get("scope")) == registry_scope
        )
    if not has_acr_push:
        raise PreflightError(
            "OIDC deployment identity lacks AcrPush on the OPTIMA registry"
        )


def _check_foundry_runtime_access(
    configuration: DeploymentConfiguration, azure: AzureQuery
) -> None:
    if configuration.openai_resource_id is None:
        raise PreflightError(
            "Azure OpenAI resource ID is required to verify runtime access"
        )
    openai_resource_id = configuration.openai_resource_id
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
        openai_resource_id,
        "--all",
    )
    if not isinstance(assignments, list):
        raise PreflightError("Foundry role assignment response is malformed")
    openai_scope = _canonical_arm_scope(openai_resource_id)
    has_openai_user = False
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise PreflightError("Foundry role assignment response is malformed")
        has_openai_user = has_openai_user or (
            _role_definition_guid(
                assignment.get("roleDefinitionId"),
                subscription_id=configuration.subscription_id,
            )
            == OPENAI_USER_ROLE_ID
            and _canonical_arm_scope(assignment.get("scope")) == openai_scope
        )
    if not has_openai_user:
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
    identified_resources = [(registry, "registry"), (cosmos, "Cosmos")]
    redis: dict[str, Any] | None = None
    if configuration.semantic_cache_enabled:
        redis = _one_resource(resources, "Microsoft.Cache/redisEnterprise")
        identified_resources.append((redis, "Redis"))
    for resource, label in identified_resources:
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
        has_acr_pull = False
        for assignment in registry_assignments:
            if not isinstance(assignment, dict):
                raise PreflightError("ACR role assignment response is malformed")
            has_acr_pull = has_acr_pull or (
                assignment.get("principalId") == principal_id
                and _role_definition_guid(
                    assignment.get("roleDefinitionId"),
                    subscription_id=configuration.subscription_id,
                )
                == ACR_PULL_ROLE_ID
            )
        if not has_acr_pull:
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
    expected_cosmos_role = (
        f"{cosmos['id']}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
    ).casefold()
    if not isinstance(cosmos_values, list) or not any(
        isinstance(assignment, dict)
        and assignment.get("properties", {}).get("principalId") == principals["api"]
        and str(assignment.get("properties", {}).get("roleDefinitionId", "")).casefold()
        == expected_cosmos_role
        and str(assignment.get("properties", {}).get("scope", "")).casefold()
        == expected_cosmos_scope
        for assignment in cosmos_values
    ):
        raise PreflightError(
            "OPTIMA API identity lacks container-scoped Cosmos data contribution"
        )
    if redis is not None:
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
            redis_assignments.get("value")
            if isinstance(redis_assignments, dict)
            else None
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
    if configuration.registry_name is None:
        raise PreflightError(
            "Container registry name is required to verify published artifacts"
        )
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
    classified_evidence: Path | None = None,
    classified_evidence_sha256: str | None = None,
    raw_whatif: Path | None = None,
    raw_whatif_sha256: str | None = None,
    effective_parameters: Path | None = None,
    effective_parameters_sha256: str | None = None,
    expected_commit_sha: str | None = None,
) -> dict[str, Any]:
    """Run a read-only preflight phase and return secret-free evidence."""
    if phase not in PREFLIGHT_PHASES:
        raise PreflightError(f"Unsupported preflight phase {phase}")
    _check_iac_representation(repository_root)
    session_principal_id = _check_account(configuration, azure)
    if phase == "production-session":
        _check_oidc_federation(
            configuration,
            azure,
            phase=phase,
            session_principal_id=session_principal_id,
            repository_root=repository_root,
            classified_evidence=None,
            classified_evidence_sha256=None,
            raw_whatif=None,
            raw_whatif_sha256=None,
            effective_parameters=None,
            effective_parameters_sha256=None,
            expected_commit_sha=None,
            verify_roles=False,
        )
        return {
            "checks": ["account", "github_oidc_federation"],
            "environment": EXPECTED_ENVIRONMENT,
            "location": configuration.location,
            "phase": phase,
            "resource_group": configuration.resource_group,
            "subscription_id": _redact_identifier(configuration.subscription_id),
            "tenant_id": _redact_identifier(configuration.tenant_id),
        }
    cache_provider = _check_providers(
        azure,
        semantic_cache_enabled=configuration.semantic_cache_enabled,
    )
    _check_oidc_federation(
        configuration,
        azure,
        phase=phase,
        session_principal_id=session_principal_id,
        repository_root=repository_root,
        classified_evidence=classified_evidence,
        classified_evidence_sha256=classified_evidence_sha256,
        raw_whatif=raw_whatif,
        raw_whatif_sha256=raw_whatif_sha256,
        effective_parameters=effective_parameters,
        effective_parameters_sha256=effective_parameters_sha256,
        expected_commit_sha=expected_commit_sha,
    )
    redis_evidence: dict[str, Any] | None = None
    if configuration.semantic_cache_enabled:
        if cache_provider is None:
            raise AssertionError("enabled semantic cache requires provider evidence")
        redis_evidence = _check_redis_availability(configuration, azure, cache_provider)
    model_evidence: dict[str, dict[str, str]] = {}
    if configuration.models:
        model_evidence = _check_model_deployments(configuration, azure)
    require_foundation = phase in {
        "production-foundation",
        "publish",
        "artifacts",
        "rollout",
    }
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
    cost_evidence: dict[str, str] = {
        "fixed_monthly_inr": str(configuration.expected_fixed_monthly_cost_inr),
        "reviewed_on": configuration.cost_reviewed_on.isoformat(),
    }
    if configuration.pricing is not None:
        cost_evidence["binding_sha256"] = configuration.pricing.binding_sha256
        cost_evidence["catalog_version"] = configuration.pricing.catalog_version
        cost_evidence["currency"] = configuration.pricing.currency
        cost_evidence["source_url"] = configuration.pricing.source_url
    return {
        "artifacts": artifact_evidence,
        "checks": [
            "account",
            "providers",
            "github_oidc_federation",
            *(
                (
                    "redis_provider_registration",
                    "redis_regional_resource_type",
                    "redis_exact_sku_advertisement",
                    "redis_applicable_restrictions",
                    "redis_quota_exposure",
                    "redis_allocation_not_provable",
                )
                if configuration.semantic_cache_enabled
                else (
                    "semantic_cache_explicitly_disabled",
                    "redis_resource_absent",
                    "embedding_configuration_absent",
                )
            ),
            *(("model_deployments",) if configuration.models else ()),
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
        "cost": cost_evidence,
        "environment": EXPECTED_ENVIRONMENT,
        "location": configuration.location,
        "models": model_evidence,
        "phase": phase,
        "redis": redis_evidence,
        "semantic_cache": {
            "embedding_configuration": (
                "REQUIRED" if configuration.semantic_cache_enabled else "NOT_CONFIGURED"
            ),
            "enabled": configuration.semantic_cache_enabled,
            "redis_resource": (
                "REQUIRED"
                if configuration.semantic_cache_enabled
                else "NOT_PROVISIONED"
            ),
            "status": (
                "ENABLED" if configuration.semantic_cache_enabled else "DISABLED"
            ),
        },
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
        choices=PREFLIGHT_PHASES,
        required=True,
    )
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--api-digest")
    parser.add_argument("--ui-digest")
    parser.add_argument("--classified-evidence", type=Path)
    parser.add_argument("--classified-evidence-sha256")
    parser.add_argument("--raw-whatif", type=Path)
    parser.add_argument("--raw-whatif-sha256")
    parser.add_argument("--effective-parameters", type=Path)
    parser.add_argument("--effective-parameters-sha256")
    parser.add_argument("--expected-commit-sha")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run preflight and write only redacted, non-secret evidence."""
    arguments = create_parser().parse_args(argv)
    try:
        configuration = load_configuration(os.environ, phase=arguments.phase)
        evidence = run_preflight(
            configuration,
            AzureCli(),
            phase=arguments.phase,
            repository_root=arguments.repository_root.resolve(),
            api_digest=arguments.api_digest,
            ui_digest=arguments.ui_digest,
            classified_evidence=arguments.classified_evidence,
            classified_evidence_sha256=arguments.classified_evidence_sha256,
            raw_whatif=arguments.raw_whatif,
            raw_whatif_sha256=arguments.raw_whatif_sha256,
            effective_parameters=arguments.effective_parameters,
            effective_parameters_sha256=arguments.effective_parameters_sha256,
            expected_commit_sha=arguments.expected_commit_sha,
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
