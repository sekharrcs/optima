"""Fail-closed classification of the OPTIMA foundation Azure what-if result.

The classifier accepts only a successful, complete structured what-if for the
exact nine-resource foundation graph. It emits sanitized, versioned evidence
whose fingerprints bind the target scope, deployment source, effective
parameters, and complete canonical resource-change payload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

EXIT_SUCCESS = 0
EXIT_FAILURE = 1

EVIDENCE_SCHEMA_VERSION = "optima-foundation-whatif-evidence-v1"
SCOPE_FINGERPRINT_VERSION = "optima-foundation-scope-v1"
SOURCE_FINGERPRINT_VERSION = "optima-foundation-deployment-source-v1"
PARAMETER_FINGERPRINT_VERSION = "optima-foundation-parameters-v1"
CHANGE_FINGERPRINT_VERSION = "optima-foundation-resource-changes-v1"

ALLOWED_CHANGE_TYPES = frozenset({"Create", "NoChange"})
_OFFICIAL_CHANGE_TYPES = frozenset(
    {"Create", "Delete", "Deploy", "Ignore", "Modify", "NoChange", "Unsupported"}
)
_TOP_LEVEL_FIELDS = frozenset(
    {"status", "changes", "potentialChanges", "diagnostics", "error"}
)
_CHANGE_FIELDS = frozenset(
    {
        "after",
        "before",
        "changeType",
        "delta",
        "deploymentId",
        "extension",
        "identifiers",
        "resourceId",
        "symbolicName",
        "unsupportedReason",
    }
)
_FOUNDATION_PARAMETER_KEYS = frozenset(
    {
        "deployContainerApps",
        "deployRuntimeAccess",
        "environmentName",
        "exposePublicUi",
        "location",
        "parameterFile",
        "resourceGroup",
        "semanticCacheEnabled",
        "templateFile",
    }
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_SUBSCRIPTION_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_UNIQUE_SUFFIX = re.compile(r"[a-z0-9]{13}")
_MODULE_DECLARATION = re.compile(
    r"(?m)^\s*module\s+[A-Za-z_][A-Za-z0-9_]*\s+'([^'\r\n]+)'"
)
_MODULE_LINE = re.compile(r"(?m)^\s*module\b")
_USING_DECLARATION = re.compile(r"(?m)^\s*using\s+'([^'\r\n]+)'\s*$")

_RESOURCE_ROLE_TYPES = {
    "api_identity": "microsoft.managedidentity/userassignedidentities",
    "application_insights": "microsoft.insights/components",
    "container_registry": "microsoft.containerregistry/registries",
    "cosmos_account": "microsoft.documentdb/databaseaccounts",
    "cosmos_container": (
        "microsoft.documentdb/databaseaccounts/sqldatabases/containers"
    ),
    "cosmos_database": "microsoft.documentdb/databaseaccounts/sqldatabases",
    "log_analytics_workspace": "microsoft.operationalinsights/workspaces",
    "managed_environment": "microsoft.app/managedenvironments",
    "ui_identity": "microsoft.managedidentity/userassignedidentities",
}
EXPECTED_FOUNDATION_RESOURCE_TYPES = frozenset(_RESOURCE_ROLE_TYPES.values())


class WhatIfClassificationCode(StrEnum):
    """Stable fail-closed outcome codes for the foundation what-if classifier."""

    MALFORMED_DOCUMENT = "WHATIF_MALFORMED_DOCUMENT"
    OPERATION_NOT_SUCCEEDED = "WHATIF_OPERATION_NOT_SUCCEEDED"
    SERVICE_ERROR = "WHATIF_SERVICE_ERROR"
    POTENTIAL_CHANGES = "WHATIF_POTENTIAL_CHANGES"
    DIAGNOSTICS = "WHATIF_DIAGNOSTICS"
    NO_STRUCTURED_CHANGES = "WHATIF_NO_STRUCTURED_CHANGES"
    MALFORMED_CHANGE = "WHATIF_MALFORMED_CHANGE"
    UNSUPPORTED_CHANGE = "WHATIF_UNSUPPORTED_CHANGE"
    UNCLASSIFIABLE_CHANGE_TYPE = "WHATIF_UNCLASSIFIABLE_CHANGE_TYPE"
    DELETE_REJECTED = "WHATIF_DELETE_REJECTED"
    REPLACEMENT_REJECTED = "WHATIF_REPLACEMENT_REJECTED"
    UNEXPECTED_MODIFY = "WHATIF_UNEXPECTED_MODIFY"
    MALFORMED_RESOURCE_ID = "WHATIF_MALFORMED_RESOURCE_ID"
    RESOURCE_OUTSIDE_SCOPE = "WHATIF_RESOURCE_OUTSIDE_SCOPE"
    AZURE_OPENAI_CHANGE = "WHATIF_AZURE_OPENAI_CHANGE"
    ROLE_ASSIGNMENT_CHANGE = "WHATIF_ROLE_ASSIGNMENT_CHANGE"
    REDIS_CHANGE = "WHATIF_REDIS_CHANGE"
    APPLICATION_CHANGE = "WHATIF_APPLICATION_CHANGE"
    UNEXPECTED_RESOURCE_TYPE = "WHATIF_UNEXPECTED_RESOURCE_TYPE"
    DUPLICATE_RESOURCE = "WHATIF_DUPLICATE_RESOURCE"
    RESOURCE_GRAPH_MISMATCH = "WHATIF_RESOURCE_GRAPH_MISMATCH"
    INVALID_PARAMETERS = "WHATIF_INVALID_PARAMETERS"
    INVALID_DEPLOYMENT_SOURCE = "WHATIF_INVALID_DEPLOYMENT_SOURCE"
    PROMOTION_MISMATCH = "WHATIF_PROMOTION_MISMATCH"


class WhatIfClassificationError(RuntimeError):
    """A fail-closed foundation what-if outcome with a stable code."""

    def __init__(self, code: WhatIfClassificationCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True)
class ParsedResourceId:
    """A canonical group-scoped ARM resource identifier."""

    canonical_id: str
    resource_type: str
    resource_name: str


@dataclass(frozen=True)
class AllowedChange:
    """One approved foundation change reduced to non-sensitive facts."""

    change_type: str
    resource_type: str
    resource_name: str
    resource_role: str


@dataclass(frozen=True)
class FoundationWhatIfClassification:
    """Result of classifying a foundation what-if as safe to apply."""

    resource_group: str
    environment_name: str
    allowed_changes: tuple[AllowedChange, ...]
    change_counts: Mapping[str, int]
    scope_fingerprint: str
    change_fingerprint: str


def _raise_malformed(message: str) -> None:
    """Raise a malformed-document error without including raw input."""
    raise WhatIfClassificationError(
        WhatIfClassificationCode.MALFORMED_DOCUMENT, message
    )


def _has_control_character(value: str) -> bool:
    """Return whether a string contains a Unicode control character."""
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validate_json_value(value: Any) -> None:
    """Require a value to belong to strict, finite JSON's data model."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _raise_malformed("Structured evidence contains a non-finite number")
        return
    if isinstance(value, Decimal):
        if not value.is_finite():
            _raise_malformed("Structured evidence contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _raise_malformed("Structured evidence contains a non-string key")
            _validate_json_value(item)
        return
    _raise_malformed("Structured evidence contains an unsupported JSON value")


def _canonical_number(value: int | float | Decimal) -> str:
    """Render one finite JSON number in a stable, bounded representation."""
    if isinstance(value, float):
        if not math.isfinite(value):
            _raise_malformed("Structured evidence contains a non-finite number")
        number = Decimal(str(value))
    elif isinstance(value, Decimal):
        number = value
    else:
        number = Decimal(value)
    if not number.is_finite():
        _raise_malformed("Structured evidence contains a non-finite number")
    if number.is_zero():
        return "0"

    normalized = number.normalize()
    sign, digits, exponent = normalized.as_tuple()
    finite_exponent = cast(int, exponent)
    digit_text = "".join(str(digit) for digit in digits)
    adjusted = len(digit_text) + finite_exponent - 1
    prefix = "-" if sign else ""
    if -6 <= adjusted < 21:
        if finite_exponent >= 0:
            return prefix + digit_text + ("0" * finite_exponent)
        point = len(digit_text) + finite_exponent
        if point > 0:
            return prefix + digit_text[:point] + "." + digit_text[point:]
        return prefix + "0." + ("0" * -point) + digit_text
    mantissa = digit_text[0]
    if len(digit_text) > 1:
        mantissa += "." + digit_text[1:]
    return f"{prefix}{mantissa}e{adjusted}"


def _canonical_json_text(value: Any) -> str:
    """Serialize strict JSON deterministically while preserving array order."""
    _validate_json_value(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json_text(item) for item in value) + "]"
    if isinstance(value, dict):
        items = (
            f"{_canonical_json_text(key)}:{_canonical_json_text(value[key])}"
            for key in sorted(value)
        )
        return "{" + ",".join(items) + "}"
    raise AssertionError("Strict JSON validation and serialization diverged")


def _versioned_fingerprint(version: str, value: Any) -> str:
    """Hash one canonical payload with an explicit domain/version separator."""
    serialized = f"{version}\n{_canonical_json_text(value)}".encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _parse_resource_id(
    resource_id: str,
    *,
    subscription_id: str,
    resource_group: str,
) -> ParsedResourceId:
    """Parse one complete canonical group-scoped ARM ID case-insensitively."""
    if (
        not resource_id
        or resource_id != resource_id.strip()
        or not resource_id.startswith("/")
        or resource_id.endswith("/")
        or "//" in resource_id
        or "\\" in resource_id
        or "%" in resource_id
        or "?" in resource_id
        or "#" in resource_id
        or _has_control_character(resource_id)
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_RESOURCE_ID,
            "What-if change entry has a noncanonical resource ID",
        )

    segments = resource_id.split("/")[1:]
    if (
        len(segments) < 4
        or segments[0].casefold() != "subscriptions"
        or segments[2].casefold() != "resourcegroups"
        or segments[1].casefold() != subscription_id.casefold()
        or segments[3].casefold() != resource_group.casefold()
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.RESOURCE_OUTSIDE_SCOPE,
            "What-if change entry is not in the exact approved resource group scope",
        )
    if (
        len(segments) < 8
        or any(not segment or segment in {".", ".."} for segment in segments)
        or segments[4].casefold() != "providers"
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_RESOURCE_ID,
            "What-if change entry has an invalid group-scoped resource path",
        )

    provider_tail = segments[5:]
    if (
        len(provider_tail) < 3
        or (len(provider_tail) - 1) % 2 != 0
        or any(segment.casefold() == "providers" for segment in provider_tail[1:])
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_RESOURCE_ID,
            "What-if change entry has an invalid provider/type/name chain",
        )

    namespace = provider_tail[0].casefold()
    type_segments = [segment.casefold() for segment in provider_tail[1::2]]
    canonical_segments = [segment.casefold() for segment in segments]
    return ParsedResourceId(
        canonical_id="/" + "/".join(canonical_segments),
        resource_type="/".join([namespace, *type_segments]),
        resource_name=provider_tail[-1].casefold(),
    )


def _denied_type_code(resource_type: str) -> WhatIfClassificationCode:
    """Map a rejected resource type to its most specific denial code."""
    if resource_type.startswith("microsoft.cognitiveservices/"):
        return WhatIfClassificationCode.AZURE_OPENAI_CHANGE
    if resource_type.startswith("microsoft.authorization/roleassignments"):
        return WhatIfClassificationCode.ROLE_ASSIGNMENT_CHANGE
    if resource_type.startswith("microsoft.cache/"):
        return WhatIfClassificationCode.REDIS_CHANGE
    application_prefixes = ("microsoft.app/containerapps", "microsoft.app/jobs")
    if any(resource_type.startswith(prefix) for prefix in application_prefixes):
        return WhatIfClassificationCode.APPLICATION_CHANGE
    return WhatIfClassificationCode.UNEXPECTED_RESOURCE_TYPE


def _validate_change_shape(change: Any) -> dict[str, Any]:
    """Validate the closed official resource-change shape used by this gate."""
    if not isinstance(change, dict):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_CHANGE,
            "What-if change entry is not a JSON object",
        )
    if set(change) - _CHANGE_FIELDS:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_CHANGE,
            "What-if change entry contains unsupported fields",
        )
    if "changeType" not in change or "resourceId" not in change:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_CHANGE,
            "What-if change entry is missing required fields",
        )
    _validate_json_value(change)

    for field in ("changeType", "resourceId"):
        if not isinstance(change[field], str) or not change[field].strip():
            raise WhatIfClassificationError(
                WhatIfClassificationCode.MALFORMED_CHANGE,
                "What-if change entry has an invalid required string",
            )
    for field in ("deploymentId", "symbolicName", "unsupportedReason"):
        if (
            field in change
            and change[field] is not None
            and not isinstance(change[field], str)
        ):
            raise WhatIfClassificationError(
                WhatIfClassificationCode.MALFORMED_CHANGE,
                "What-if change entry has an invalid optional string",
            )
    for field in ("before", "after", "extension"):
        if (
            field in change
            and change[field] is not None
            and not isinstance(change[field], dict)
        ):
            raise WhatIfClassificationError(
                WhatIfClassificationCode.MALFORMED_CHANGE,
                "What-if change entry has an invalid structured payload",
            )
    if (
        "delta" in change
        and change["delta"] is not None
        and not isinstance(change["delta"], list)
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_CHANGE,
            "What-if change entry has an invalid delta payload",
        )
    if "identifiers" in change and change["identifiers"] is not None:
        identifiers = change["identifiers"]
        if not isinstance(identifiers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in identifiers.items()
        ):
            raise WhatIfClassificationError(
                WhatIfClassificationCode.MALFORMED_CHANGE,
                "What-if change entry has invalid identifiers",
            )
    return change


def _validate_change_semantics(change: Mapping[str, Any]) -> str:
    """Accept only internally consistent Create and NoChange operations."""
    change_type = cast(str, change["changeType"])
    if change.get("unsupportedReason") not in (None, ""):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.UNSUPPORTED_CHANGE,
            "What-if reported an unsupported resource change",
        )
    if change_type not in _OFFICIAL_CHANGE_TYPES:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.UNCLASSIFIABLE_CHANGE_TYPE,
            "What-if reported an unclassifiable resource change type",
        )
    if change_type == "Delete":
        raise WhatIfClassificationError(
            WhatIfClassificationCode.DELETE_REJECTED,
            "Foundation what-if must not delete any resource",
        )
    if change_type == "Modify":
        raise WhatIfClassificationError(
            WhatIfClassificationCode.UNEXPECTED_MODIFY,
            "Foundation what-if must not modify existing resources",
        )
    if change_type not in ALLOWED_CHANGE_TYPES:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.UNSUPPORTED_CHANGE,
            "Foundation what-if contains a disallowed resource change type",
        )
    if change_type == "Create" and change.get("before") not in (None, {}):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.REPLACEMENT_REJECTED,
            "Foundation what-if must not replace an existing resource",
        )
    if change_type == "NoChange":
        before = change.get("before")
        after = change.get("after")
        if (
            before is not None
            and after is not None
            and _canonical_json_text(before) != _canonical_json_text(after)
        ) or change.get("delta") not in (None, []):
            raise WhatIfClassificationError(
                WhatIfClassificationCode.MALFORMED_CHANGE,
                "NoChange evidence contains a contradictory payload",
            )
    return change_type


def _expected_resource_graph(
    *,
    subscription_id: str,
    resource_group: str,
    environment_name: str,
    unique_suffix: str,
) -> dict[str, tuple[str, str, str]]:
    """Return canonical IDs mapped to resource role, type, and final name."""
    scope = (
        f"/subscriptions/{subscription_id}/resourcegroups/{resource_group}/providers"
    ).casefold()
    cosmos_account = f"cosmos-optima-{unique_suffix}"
    resources = {
        "api_identity": (
            "microsoft.managedidentity/userassignedidentities",
            f"id-optima-api-{environment_name}",
        ),
        "ui_identity": (
            "microsoft.managedidentity/userassignedidentities",
            f"id-optima-ui-{environment_name}",
        ),
        "container_registry": (
            "microsoft.containerregistry/registries",
            f"acroptima{unique_suffix}",
        ),
        "log_analytics_workspace": (
            "microsoft.operationalinsights/workspaces",
            f"law-optima-{environment_name}",
        ),
        "application_insights": (
            "microsoft.insights/components",
            f"appi-optima-{environment_name}",
        ),
        "cosmos_account": (
            "microsoft.documentdb/databaseaccounts",
            cosmos_account,
        ),
        "cosmos_database": (
            "microsoft.documentdb/databaseaccounts/sqldatabases",
            "optima",
        ),
        "cosmos_container": (
            "microsoft.documentdb/databaseaccounts/sqldatabases/containers",
            "runs",
        ),
        "managed_environment": (
            "microsoft.app/managedenvironments",
            f"cae-optima-{environment_name}",
        ),
    }
    graph: dict[str, tuple[str, str, str]] = {}
    for role, (resource_type, resource_name) in resources.items():
        namespace, *type_segments = resource_type.split("/")
        if role == "cosmos_database":
            tail = f"{namespace}/databaseaccounts/{cosmos_account}/sqldatabases/optima"
        elif role == "cosmos_container":
            tail = (
                f"{namespace}/databaseaccounts/{cosmos_account}/sqldatabases/"
                "optima/containers/runs"
            )
        else:
            tail = f"{namespace}/{type_segments[0]}/{resource_name}"
        graph[f"{scope}/{tail}".casefold()] = (
            role,
            resource_type,
            resource_name,
        )
    return graph


def _scope_fingerprint(subscription_id: str, resource_group: str) -> str:
    """Bind evidence to an exact target without exposing its subscription ID."""
    return _versioned_fingerprint(
        SCOPE_FINGERPRINT_VERSION,
        {
            "resource_group": resource_group.casefold(),
            "subscription_id": subscription_id.casefold(),
        },
    )


def classify_foundation_whatif(
    document: Any,
    *,
    subscription_id: str,
    resource_group: str,
    environment_name: str = "hackathon",
) -> FoundationWhatIfClassification:
    """Classify a structured what-if result, raising on any unsafe evidence."""
    if not isinstance(document, dict):
        _raise_malformed("What-if output is not a JSON object")
    _validate_json_value(document)
    if set(document) - _TOP_LEVEL_FIELDS:
        _raise_malformed("What-if output contains unsupported top-level fields")
    if document.get("status") != "Succeeded":
        raise WhatIfClassificationError(
            WhatIfClassificationCode.OPERATION_NOT_SUCCEEDED,
            "What-if operation did not report the exact Succeeded status",
        )
    if "error" in document and document["error"] is not None:
        if not isinstance(document["error"], dict):
            _raise_malformed("What-if output has an invalid service error field")
        raise WhatIfClassificationError(
            WhatIfClassificationCode.SERVICE_ERROR,
            "What-if operation reported a service error",
        )
    for field, code in (
        ("potentialChanges", WhatIfClassificationCode.POTENTIAL_CHANGES),
        ("diagnostics", WhatIfClassificationCode.DIAGNOSTICS),
    ):
        if field in document:
            value = document[field]
            if not isinstance(value, list):
                _raise_malformed(f"What-if output has an invalid {field} field")
            if value:
                raise WhatIfClassificationError(
                    code,
                    f"Foundation what-if rejects nonempty {field}",
                )

    changes = document.get("changes")
    if not isinstance(changes, list) or not changes:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.NO_STRUCTURED_CHANGES,
            "What-if output has no complete structured changes array",
        )
    if not _SUBSCRIPTION_ID.fullmatch(subscription_id):
        _raise_malformed("Approved subscription ID is not canonical")
    for value, label in (
        (resource_group, "resource group"),
        (environment_name, "environment name"),
    ):
        if (
            not value
            or value != value.strip()
            or any(separator in value for separator in ("/", "\\", "%"))
            or _has_control_character(value)
        ):
            _raise_malformed(f"Approved {label} is not canonical")

    parsed_changes: list[tuple[dict[str, Any], str, ParsedResourceId]] = []
    seen_resource_ids: set[str] = set()
    counts = {"Create": 0, "NoChange": 0}
    for raw_change in changes:
        change = _validate_change_shape(raw_change)
        change_type = _validate_change_semantics(change)
        resource_id = _parse_resource_id(
            change["resourceId"],
            subscription_id=subscription_id,
            resource_group=resource_group,
        )
        if resource_id.resource_type not in EXPECTED_FOUNDATION_RESOURCE_TYPES:
            raise WhatIfClassificationError(
                _denied_type_code(resource_id.resource_type),
                "Foundation what-if contains a resource type outside the contract",
            )
        if resource_id.canonical_id in seen_resource_ids:
            raise WhatIfClassificationError(
                WhatIfClassificationCode.DUPLICATE_RESOURCE,
                "Foundation what-if contains a duplicate resource",
            )
        seen_resource_ids.add(resource_id.canonical_id)
        counts[change_type] += 1
        parsed_changes.append((change, change_type, resource_id))

    registries = [
        parsed
        for _, _, parsed in parsed_changes
        if parsed.resource_type == "microsoft.containerregistry/registries"
    ]
    accounts = [
        parsed
        for _, _, parsed in parsed_changes
        if parsed.resource_type == "microsoft.documentdb/databaseaccounts"
    ]
    if len(registries) != 1 or len(accounts) != 1:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.RESOURCE_GRAPH_MISMATCH,
            "Foundation what-if does not contain the exact generated-name resources",
        )
    registry_match = re.fullmatch(
        r"acroptima([a-z0-9]{13})", registries[0].resource_name
    )
    cosmos_match = re.fullmatch(
        r"cosmos-optima-([a-z0-9]{13})", accounts[0].resource_name
    )
    if (
        registry_match is None
        or cosmos_match is None
        or registry_match.group(1) != cosmos_match.group(1)
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.RESOURCE_GRAPH_MISMATCH,
            "Foundation generated resource names do not share one valid suffix",
        )

    expected_graph = _expected_resource_graph(
        subscription_id=subscription_id,
        resource_group=resource_group,
        environment_name=environment_name.casefold(),
        unique_suffix=registry_match.group(1),
    )
    if seen_resource_ids != set(expected_graph):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.RESOURCE_GRAPH_MISMATCH,
            "Foundation what-if does not match the exact nine-resource graph",
        )

    allowed: list[AllowedChange] = []
    canonical_payloads: list[dict[str, Any]] = []
    for change, change_type, resource_id in parsed_changes:
        role, resource_type, resource_name = expected_graph[resource_id.canonical_id]
        allowed.append(
            AllowedChange(
                change_type=change_type,
                resource_type=resource_type,
                resource_name=resource_name,
                resource_role=role,
            )
        )
        canonical_change = dict(change)
        canonical_change["resourceId"] = resource_id.canonical_id
        canonical_payloads.append(canonical_change)

    canonical_payloads.sort(key=_canonical_json_text)
    allowed.sort(key=lambda item: item.resource_role)
    return FoundationWhatIfClassification(
        resource_group=resource_group.casefold(),
        environment_name=environment_name.casefold(),
        allowed_changes=tuple(allowed),
        change_counts=counts,
        scope_fingerprint=_scope_fingerprint(subscription_id, resource_group),
        change_fingerprint=_versioned_fingerprint(
            CHANGE_FINGERPRINT_VERSION, canonical_payloads
        ),
    )


def parameter_fingerprint(parameters: Mapping[str, str]) -> str:
    """Return a versioned fingerprint over the complete effective parameters."""
    document = {str(key): str(value) for key, value in parameters.items()}
    return _versioned_fingerprint(PARAMETER_FINGERPRINT_VERSION, document)


def _parse_parameter_lines(text: str) -> dict[str, str]:
    """Parse ``key=value`` deployment parameter lines into a mapping."""
    parameters: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        normalized_key = key.strip()
        if not separator or not normalized_key or normalized_key in parameters:
            raise WhatIfClassificationError(
                WhatIfClassificationCode.INVALID_PARAMETERS,
                "Deployment parameters are malformed or duplicated",
            )
        parameters[normalized_key] = value
    if set(parameters) != _FOUNDATION_PARAMETER_KEYS:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_PARAMETERS,
            "Deployment parameters do not match the closed foundation profile",
        )
    return parameters


def _validate_foundation_parameters(
    parameters: Mapping[str, str],
    *,
    resource_group: str,
) -> None:
    """Require the exact non-application foundation deployment profile."""
    expected = {
        "deployContainerApps": "false",
        "deployRuntimeAccess": "false",
        "exposePublicUi": "false",
        "location": "eastus2",
        "resourceGroup": resource_group,
        "semanticCacheEnabled": "false",
    }
    if any(parameters.get(key) != value for key, value in expected.items()):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_PARAMETERS,
            "Deployment parameters are not the approved foundation profile",
        )
    environment_name = parameters.get("environmentName", "")
    if (
        not environment_name
        or environment_name != environment_name.strip()
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", environment_name)
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_PARAMETERS,
            "Deployment environment name is not canonical",
        )


def _resolve_source_reference(
    *,
    root: Path,
    parent: Path,
    reference: str,
    allow_parent: bool,
) -> Path:
    """Resolve one canonical source reference and keep it inside the root."""
    raw_parts = reference.split("/")
    if (
        not reference
        or reference.startswith("/")
        or "\\" in reference
        or "//" in reference
        or "%" in reference
        or _has_control_character(reference)
        or any(not part or part == "." for part in raw_parts)
        or (not allow_parent and ".." in raw_parts)
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE,
            "Deployment source contains a noncanonical path",
        )
    unresolved = parent / Path(*raw_parts)
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE,
            "Deployment source escapes the deterministic root",
        ) from error
    if unresolved.is_symlink() or not candidate.is_file():
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE,
            "Deployment source file is missing or unsupported",
        )
    return candidate


def deployment_source_fingerprint(
    parameters: Mapping[str, str],
    *,
    source_root: Path | None = None,
) -> tuple[str, int]:
    """Fingerprint the actual template, parameter file, and recursive modules."""
    root = (source_root or Path(__file__).resolve().parents[1]).resolve()
    if not root.is_dir():
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE,
            "Deterministic deployment source root is unavailable",
        )
    template = _resolve_source_reference(
        root=root,
        parent=root,
        reference=parameters.get("templateFile", ""),
        allow_parent=False,
    )
    parameter_file = _resolve_source_reference(
        root=root,
        parent=root,
        reference=parameters.get("parameterFile", ""),
        allow_parent=False,
    )

    pending = [template]
    contents: dict[Path, bytes] = {}
    while pending:
        source = pending.pop()
        if source in contents:
            continue
        try:
            content = source.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise WhatIfClassificationError(
                WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE,
                "Deployment source cannot be read as UTF-8",
            ) from error
        module_references = _MODULE_DECLARATION.findall(text)
        if len(module_references) != len(_MODULE_LINE.findall(text)):
            raise WhatIfClassificationError(
                WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE,
                "Deployment template contains an unsupported module reference",
            )
        contents[source] = content
        for reference in module_references:
            pending.append(
                _resolve_source_reference(
                    root=root,
                    parent=source.parent,
                    reference=reference,
                    allow_parent=True,
                )
            )

    try:
        parameter_content = parameter_file.read_bytes()
        parameter_text = parameter_content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE,
            "Deployment parameter source cannot be read as UTF-8",
        ) from error
    using_matches = _USING_DECLARATION.findall(parameter_text)
    if len(using_matches) != 1:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE,
            "Deployment parameter source has an unsupported using declaration",
        )
    using_target = _resolve_source_reference(
        root=root,
        parent=parameter_file.parent,
        reference=using_matches[0],
        allow_parent=True,
    )
    if using_target != template:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE,
            "Deployment parameter source targets a different template",
        )
    contents[parameter_file] = parameter_content

    digest = hashlib.sha256()
    digest.update(SOURCE_FINGERPRINT_VERSION.encode("ascii") + b"\0")
    for path in sorted(contents, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = contents[path]
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest(), len(contents)


def _validate_fingerprint(value: Any) -> bool:
    """Return whether a value is one canonical SHA-256 fingerprint."""
    return isinstance(value, str) and _HEX_SHA256.fullmatch(value) is not None


def _validate_resource_facts(
    resources: Any,
    *,
    environment_name: str,
) -> None:
    """Validate the closed sanitized resource-fact projection."""
    if (
        not isinstance(resources, list)
        or len(resources) != 9
        or not all(isinstance(fact, dict) for fact in resources)
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion evidence does not contain nine resource facts",
        )
    facts_by_role: dict[str, dict[str, str]] = {}
    for fact in resources:
        if set(fact) != {
            "change_type",
            "resource_name",
            "resource_role",
            "resource_type",
        } or not all(isinstance(value, str) and value for value in fact.values()):
            raise WhatIfClassificationError(
                WhatIfClassificationCode.PROMOTION_MISMATCH,
                "Promotion resource fact has an unsupported schema",
            )
        role = fact["resource_role"]
        if (
            role in facts_by_role
            or role not in _RESOURCE_ROLE_TYPES
            or fact["resource_type"] != _RESOURCE_ROLE_TYPES[role]
            or fact["change_type"] not in ALLOWED_CHANGE_TYPES
            or fact["resource_name"] != fact["resource_name"].casefold()
            or any(marker in fact["resource_name"] for marker in ("/", "\\", "%"))
            or _has_control_character(fact["resource_name"])
        ):
            raise WhatIfClassificationError(
                WhatIfClassificationCode.PROMOTION_MISMATCH,
                "Promotion resource facts do not match the foundation contract",
            )
        facts_by_role[role] = fact
    if resources != sorted(resources, key=lambda item: item["resource_role"]):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion resource facts are not in canonical order",
        )
    if set(facts_by_role) != set(_RESOURCE_ROLE_TYPES):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion resource roles do not match the foundation contract",
        )

    suffixes: list[str] = []
    for role, prefix in (
        ("container_registry", "acroptima"),
        ("cosmos_account", "cosmos-optima-"),
    ):
        name = facts_by_role[role]["resource_name"]
        if not name.startswith(prefix):
            raise WhatIfClassificationError(
                WhatIfClassificationCode.PROMOTION_MISMATCH,
                "Promotion generated resource names are invalid",
            )
        suffixes.append(name[len(prefix) :])
    if suffixes[0] != suffixes[1] or not _UNIQUE_SUFFIX.fullmatch(suffixes[0]):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion generated resource suffixes do not match",
        )

    expected_names = {
        "api_identity": f"id-optima-api-{environment_name}",
        "application_insights": f"appi-optima-{environment_name}",
        "container_registry": f"acroptima{suffixes[0]}",
        "cosmos_account": f"cosmos-optima-{suffixes[0]}",
        "cosmos_container": "runs",
        "cosmos_database": "optima",
        "log_analytics_workspace": f"law-optima-{environment_name}",
        "managed_environment": f"cae-optima-{environment_name}",
        "ui_identity": f"id-optima-ui-{environment_name}",
    }
    if any(
        facts_by_role[role]["resource_name"] != name
        for role, name in expected_names.items()
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion resource names do not match the foundation contract",
        )


def _validate_evidence(document: Any) -> dict[str, Any]:
    """Validate and return one closed, versioned promotion evidence document."""
    if not isinstance(document, dict) or set(document) != {
        "changes",
        "classification",
        "commit_sha",
        "deployment_source",
        "parameters",
        "schema_version",
        "target",
    }:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion evidence has an unsupported top-level schema",
        )
    if (
        document["schema_version"] != EVIDENCE_SCHEMA_VERSION
        or document["classification"] != "APPROVED"
        or not isinstance(document["commit_sha"], str)
        or _COMMIT_SHA.fullmatch(document["commit_sha"]) is None
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion evidence has invalid identity fields",
        )

    target = document["target"]
    if not isinstance(target, dict) or set(target) != {
        "environment_name",
        "resource_group",
        "scope_fingerprint",
    }:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion target evidence has an unsupported schema",
        )
    if (
        not isinstance(target["environment_name"], str)
        or not target["environment_name"]
        or target["environment_name"] != target["environment_name"].casefold()
        or not isinstance(target["resource_group"], str)
        or not target["resource_group"]
        or target["resource_group"] != target["resource_group"].casefold()
        or not _validate_fingerprint(target["scope_fingerprint"])
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion target evidence has invalid values",
        )

    source = document["deployment_source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"file_count", "fingerprint"}
        or not _validate_fingerprint(source["fingerprint"])
        or not isinstance(source["file_count"], int)
        or isinstance(source["file_count"], bool)
        or source["file_count"] < 2
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion deployment-source evidence is invalid",
        )
    parameters = document["parameters"]
    if (
        not isinstance(parameters, dict)
        or set(parameters) != {"fingerprint"}
        or not _validate_fingerprint(parameters["fingerprint"])
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion parameter evidence is invalid",
        )
    changes = document["changes"]
    if (
        not isinstance(changes, dict)
        or set(changes) != {"counts", "fingerprint", "resources"}
        or not _validate_fingerprint(changes["fingerprint"])
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion resource-change evidence is invalid",
        )
    counts = changes["counts"]
    if (
        not isinstance(counts, dict)
        or set(counts) != ALLOWED_CHANGE_TYPES
        or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in counts.values()
        )
        or sum(counts.values()) != 9
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion change counts are invalid",
        )
    _validate_resource_facts(
        changes["resources"], environment_name=target["environment_name"]
    )
    actual_counts = {"Create": 0, "NoChange": 0}
    for fact in changes["resources"]:
        actual_counts[fact["change_type"]] += 1
    if counts != actual_counts:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Promotion change counts contradict the resource facts",
        )
    return document


def build_foundation_evidence(
    classification: FoundationWhatIfClassification,
    *,
    commit_sha: str,
    parameter_fingerprint_value: str,
    deployment_source_fingerprint_value: str,
    deployment_source_file_count: int,
) -> dict[str, Any]:
    """Build and validate a sanitized, deterministic plan-evidence document."""
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "classification": "APPROVED",
        "commit_sha": commit_sha.casefold(),
        "target": {
            "resource_group": classification.resource_group,
            "environment_name": classification.environment_name,
            "scope_fingerprint": classification.scope_fingerprint,
        },
        "deployment_source": {
            "fingerprint": deployment_source_fingerprint_value,
            "file_count": deployment_source_file_count,
        },
        "parameters": {"fingerprint": parameter_fingerprint_value},
        "changes": {
            "fingerprint": classification.change_fingerprint,
            "counts": dict(classification.change_counts),
            "resources": [
                {
                    "change_type": change.change_type,
                    "resource_type": change.resource_type,
                    "resource_name": change.resource_name,
                    "resource_role": change.resource_role,
                }
                for change in classification.allowed_changes
            ],
        },
    }
    return _validate_evidence(evidence)


def compare_promotion_evidence(plan: Any, apply: Any) -> None:
    """Fail closed unless apply evidence exactly matches approved plan evidence."""
    validated_plan = _validate_evidence(plan)
    validated_apply = _validate_evidence(apply)
    if validated_plan != validated_apply:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Foundation apply evidence does not match the approved plan",
        )


def _summarize(evidence: Mapping[str, Any]) -> str:
    """Render a readable summary containing no subscription or full resource ID."""
    target = evidence["target"]
    source = evidence["deployment_source"]
    parameters = evidence["parameters"]
    changes = evidence["changes"]
    lines = [
        "Foundation what-if classification: APPROVED",
        f"  schema: {evidence['schema_version']}",
        f"  commit: {evidence['commit_sha']}",
        f"  resource group: {target['resource_group']}",
        f"  environment: {target['environment_name']}",
        f"  scope fingerprint: {target['scope_fingerprint']}",
        f"  source fingerprint: {source['fingerprint']} ({source['file_count']} files)",
        f"  parameter fingerprint: {parameters['fingerprint']}",
        f"  change fingerprint: {changes['fingerprint']}",
        f"  change counts: {changes['counts']}",
    ]
    for change in changes["resources"]:
        lines.append(
            f"  {change['change_type']}: {change['resource_role']} "
            f"{change['resource_type']} ({change['resource_name']})"
        )
    return "\n".join(lines) + "\n"


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys at every nesting level."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject JavaScript-style non-finite constants accepted by json.loads."""
    raise ValueError(f"unsupported JSON constant {value}")


def _load_json(path: Path, code: WhatIfClassificationCode) -> Any:
    """Load strict JSON from disk and convert failures to sanitized errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise WhatIfClassificationError(code, f"Cannot read {path.name}") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
            parse_float=Decimal,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise WhatIfClassificationError(
            code, f"{path.name} is not strict JSON"
        ) from error


def _read_parameter_file(path: Path) -> dict[str, str]:
    """Read and parse the effective deployment parameter summary."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.INVALID_PARAMETERS,
            "Cannot read deployment parameter summary",
        ) from error
    return _parse_parameter_lines(text)


def _run_classify(arguments: argparse.Namespace) -> None:
    """Classify a what-if result and write sanitized plan evidence."""
    document = _load_json(arguments.whatif, WhatIfClassificationCode.MALFORMED_DOCUMENT)
    parameters = _read_parameter_file(arguments.parameters_file)
    _validate_foundation_parameters(parameters, resource_group=arguments.resource_group)
    source_fingerprint, source_file_count = deployment_source_fingerprint(
        parameters, source_root=arguments.source_root
    )
    classification = classify_foundation_whatif(
        document,
        subscription_id=arguments.subscription_id,
        resource_group=arguments.resource_group,
        environment_name=parameters["environmentName"],
    )
    evidence = build_foundation_evidence(
        classification,
        commit_sha=arguments.commit_sha,
        parameter_fingerprint_value=parameter_fingerprint(parameters),
        deployment_source_fingerprint_value=source_fingerprint,
        deployment_source_file_count=source_file_count,
    )
    serialized = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(serialized, encoding="utf-8")
    print(_summarize(evidence), end="")


def _run_promote_check(arguments: argparse.Namespace) -> None:
    """Fail closed unless apply evidence matches the approved plan evidence."""
    plan = _load_json(arguments.plan, WhatIfClassificationCode.PROMOTION_MISMATCH)
    apply = _load_json(arguments.apply, WhatIfClassificationCode.PROMOTION_MISMATCH)
    compare_promotion_evidence(plan, apply)
    print("Foundation promotion evidence matches the approved plan\n", end="")


def create_parser() -> argparse.ArgumentParser:
    """Create the foundation what-if classification command-line parser."""
    parser = argparse.ArgumentParser(
        description="Classify and gate the OPTIMA foundation Azure what-if result."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser(
        "classify", help="Classify structured what-if output and emit evidence."
    )
    classify.add_argument("--whatif", type=Path, required=True)
    classify.add_argument("--subscription-id", required=True)
    classify.add_argument("--resource-group", required=True)
    classify.add_argument("--commit-sha", required=True)
    classify.add_argument("--parameters-file", type=Path, required=True)
    classify.add_argument(
        "--source-root",
        type=Path,
        help="Deterministic root containing the template and parameter source files.",
    )
    classify.add_argument("--output", type=Path)
    classify.set_defaults(handler=_run_classify)

    promote = subparsers.add_parser(
        "promote-check",
        help="Verify apply evidence matches the approved plan evidence.",
    )
    promote.add_argument("--plan", type=Path, required=True)
    promote.add_argument("--apply", type=Path, required=True)
    promote.set_defaults(handler=_run_promote_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the classifier and report a fail-closed exit code."""
    arguments = create_parser().parse_args(argv)
    try:
        arguments.handler(arguments)
    except WhatIfClassificationError as error:
        print(f"WHATIF CLASSIFICATION FAILED: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
