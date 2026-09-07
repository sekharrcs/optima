"""Fail-closed classification of the OPTIMA foundation Azure what-if result.

The approved foundation profile is ``deployContainerApps=false``,
``exposePublicUi=false``, ``deployRuntimeAccess=false``, and
``semanticCacheEnabled=false``. In that profile ``infra/resource-group.bicep``
creates only the eight foundation resource types enumerated below. This module
parses structured ``az deployment group what-if`` output (never console text),
allows only expected ``Create`` operations and legitimate ``NoChange`` results
inside the approved resource group, and fails closed on everything else:
deletions, replacements, unexpected modifications, unsupported or
unclassifiable changes, Azure OpenAI account or deployment changes, role
assignments, Redis or embedding resources, Container Apps or application
revisions, resources outside the approved resource group, any resource type not
declared by the foundation contract, and any missing, truncated, or malformed
structured evidence. Summaries and evidence redact subscription and tenant
identifiers and never echo the raw what-if body.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

EXIT_SUCCESS = 0
EXIT_FAILURE = 1

# Resource types created by infra/resource-group.bicep under the approved
# foundation profile. Derived from the reviewed Bicep resource declarations:
#   modules/identities.bicep         -> ManagedIdentity/userAssignedIdentities (x2)
#   modules/container-registry.bicep -> ContainerRegistry/registries
#   modules/monitoring.bicep         -> OperationalInsights/workspaces
#                                       + Insights/components
#   modules/cosmos.bicep             -> DocumentDB/databaseAccounts
#                                       + .../sqlDatabases + .../containers
#   modules/container-apps.bicep     -> App/managedEnvironments
# The gated redis, runtime-access, container app, job, and authConfig resources
# are deliberately excluded so that an unrelated resource can never be approved.
EXPECTED_FOUNDATION_RESOURCE_TYPES = frozenset(
    {
        "microsoft.app/managedenvironments",
        "microsoft.containerregistry/registries",
        "microsoft.documentdb/databaseaccounts",
        "microsoft.documentdb/databaseaccounts/sqldatabases",
        "microsoft.documentdb/databaseaccounts/sqldatabases/containers",
        "microsoft.insights/components",
        "microsoft.operationalinsights/workspaces",
        "microsoft.managedidentity/userassignedidentities",
    }
)
ALLOWED_CHANGE_TYPES = frozenset({"Create", "NoChange"})
_KNOWN_CHANGE_TYPES = frozenset(
    {
        "Create",
        "Delete",
        "Deploy",
        "Ignore",
        "Modify",
        "NoChange",
        "NoEffect",
        "Unsupported",
    }
)


class WhatIfClassificationCode(StrEnum):
    """Stable fail-closed outcome codes for the foundation what-if classifier."""

    MALFORMED_DOCUMENT = "WHATIF_MALFORMED_DOCUMENT"
    OPERATION_NOT_SUCCEEDED = "WHATIF_OPERATION_NOT_SUCCEEDED"
    NO_STRUCTURED_CHANGES = "WHATIF_NO_STRUCTURED_CHANGES"
    MALFORMED_CHANGE = "WHATIF_MALFORMED_CHANGE"
    UNSUPPORTED_CHANGE = "WHATIF_UNSUPPORTED_CHANGE"
    UNCLASSIFIABLE_CHANGE_TYPE = "WHATIF_UNCLASSIFIABLE_CHANGE_TYPE"
    DELETE_REJECTED = "WHATIF_DELETE_REJECTED"
    REPLACEMENT_REJECTED = "WHATIF_REPLACEMENT_REJECTED"
    UNEXPECTED_MODIFY = "WHATIF_UNEXPECTED_MODIFY"
    RESOURCE_OUTSIDE_SCOPE = "WHATIF_RESOURCE_OUTSIDE_SCOPE"
    AZURE_OPENAI_CHANGE = "WHATIF_AZURE_OPENAI_CHANGE"
    ROLE_ASSIGNMENT_CHANGE = "WHATIF_ROLE_ASSIGNMENT_CHANGE"
    REDIS_CHANGE = "WHATIF_REDIS_CHANGE"
    APPLICATION_CHANGE = "WHATIF_APPLICATION_CHANGE"
    UNEXPECTED_RESOURCE_TYPE = "WHATIF_UNEXPECTED_RESOURCE_TYPE"
    PROMOTION_MISMATCH = "WHATIF_PROMOTION_MISMATCH"


class WhatIfClassificationError(RuntimeError):
    """A fail-closed foundation what-if outcome with a stable code."""

    def __init__(self, code: WhatIfClassificationCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True)
class AllowedChange:
    """One approved foundation change reduced to non-sensitive facts."""

    change_type: str
    resource_type: str
    resource_name: str


@dataclass(frozen=True)
class FoundationWhatIfClassification:
    """Result of classifying a foundation what-if as safe to apply."""

    resource_group: str
    allowed_changes: tuple[AllowedChange, ...]
    change_counts: Mapping[str, int]


def _redact_identifier(value: str) -> str:
    """Return a short non-recoverable identifier fragment for evidence."""
    if len(value) < 9:
        return "redacted"
    return f"{value[:4]}...{value[-4:]}"


def _resource_type_of(resource_id: str) -> str | None:
    """Derive the fully-qualified resource type from an ARM resource ID."""
    marker = "/providers/"
    lowered = resource_id.casefold()
    index = lowered.rfind(marker)
    if index < 0:
        return None
    tail = resource_id[index + len(marker) :]
    segments = [segment for segment in tail.split("/") if segment]
    if len(segments) < 2:
        return None
    namespace = segments[0]
    type_segments = segments[1::2]
    if not type_segments:
        return None
    return "/".join([namespace, *type_segments]).casefold()


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


def classify_foundation_whatif(
    document: Any,
    *,
    subscription_id: str,
    resource_group: str,
) -> FoundationWhatIfClassification:
    """Classify a structured what-if result, raising on any unsafe change."""
    if not isinstance(document, dict):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_DOCUMENT,
            "What-if output is not a JSON object",
        )
    status = document.get("status")
    if status is not None and status != "Succeeded":
        raise WhatIfClassificationError(
            WhatIfClassificationCode.OPERATION_NOT_SUCCEEDED,
            "What-if operation did not report a Succeeded status",
        )
    changes = document.get("changes")
    if not isinstance(changes, list):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.NO_STRUCTURED_CHANGES,
            "What-if output has no structured changes array",
        )
    if not changes:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.NO_STRUCTURED_CHANGES,
            "What-if output reported no foundation changes",
        )
    approved_prefix = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/"
    ).casefold()
    allowed: list[AllowedChange] = []
    counts: dict[str, int] = {}
    for change in changes:
        allowed.append(
            _classify_change(change, approved_prefix=approved_prefix, counts=counts)
        )
    return FoundationWhatIfClassification(
        resource_group=resource_group,
        allowed_changes=tuple(allowed),
        change_counts=dict(sorted(counts.items())),
    )


def _classify_change(
    change: Any,
    *,
    approved_prefix: str,
    counts: dict[str, int],
) -> AllowedChange:
    """Validate one what-if change entry and return its approved facts."""
    if not isinstance(change, dict):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_CHANGE,
            "What-if change entry is not a JSON object",
        )
    change_type = change.get("changeType")
    resource_id = change.get("resourceId")
    if not isinstance(change_type, str) or not change_type.strip():
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_CHANGE,
            "What-if change entry has no change type",
        )
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_CHANGE,
            "What-if change entry has no resource ID",
        )
    if change.get("unsupportedReason") not in (None, ""):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.UNSUPPORTED_CHANGE,
            f"What-if reported an unsupported {change_type} change",
        )
    if change_type not in _KNOWN_CHANGE_TYPES:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.UNCLASSIFIABLE_CHANGE_TYPE,
            f"What-if reported an unclassifiable change type {change_type!r}",
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
            f"Foundation what-if rejects {change_type} changes",
        )
    if change_type == "Create" and change.get("before") not in (None, {}):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.REPLACEMENT_REJECTED,
            "Foundation what-if must not replace an existing resource",
        )
    if not resource_id.casefold().startswith(approved_prefix):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.RESOURCE_OUTSIDE_SCOPE,
            "Foundation what-if changed a resource outside the approved group",
        )
    resource_type = _resource_type_of(resource_id)
    if resource_type is None:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_CHANGE,
            "What-if change entry has an unparsable resource ID",
        )
    if resource_type not in EXPECTED_FOUNDATION_RESOURCE_TYPES:
        raise WhatIfClassificationError(
            _denied_type_code(resource_type),
            f"Foundation what-if rejects resource type {resource_type}",
        )
    counts[change_type] = counts.get(change_type, 0) + 1
    return AllowedChange(
        change_type=change_type,
        resource_type=resource_type,
        resource_name=resource_id.rsplit("/", maxsplit=1)[-1],
    )


def parameter_fingerprint(parameters: Mapping[str, str]) -> str:
    """Return a stable SHA-256 fingerprint over the effective parameter set."""
    document = {str(key): str(value) for key, value in parameters.items()}
    serialized = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _parse_parameter_lines(text: str) -> dict[str, str]:
    """Parse ``key=value`` deployment parameter lines into a mapping."""
    parameters: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise WhatIfClassificationError(
                WhatIfClassificationCode.MALFORMED_CHANGE,
                "Deployment parameter line is not key=value",
            )
        normalized_key = key.strip()
        if normalized_key in parameters:
            raise WhatIfClassificationError(
                WhatIfClassificationCode.MALFORMED_CHANGE,
                f"Duplicate deployment parameter {normalized_key}",
            )
        parameters[normalized_key] = value
    if not parameters:
        raise WhatIfClassificationError(
            WhatIfClassificationCode.MALFORMED_CHANGE,
            "No deployment parameters were supplied",
        )
    return parameters


def build_foundation_evidence(
    classification: FoundationWhatIfClassification,
    *,
    commit_sha: str,
    parameter_fingerprint_value: str,
    subscription_id: str,
) -> dict[str, Any]:
    """Build a sanitized, deterministic plan-evidence document."""
    return {
        "classification": "APPROVED",
        "commit_sha": commit_sha,
        "parameter_fingerprint": parameter_fingerprint_value,
        "resource_group": classification.resource_group,
        "subscription": _redact_identifier(subscription_id),
        "change_counts": dict(classification.change_counts),
        "allowed_changes": [
            {
                "change_type": change.change_type,
                "resource_type": change.resource_type,
                "resource_name": change.resource_name,
            }
            for change in classification.allowed_changes
        ],
    }


def compare_promotion_evidence(plan: Any, apply: Any) -> None:
    """Fail closed unless apply evidence matches the approved plan evidence."""
    if not isinstance(plan, dict) or not isinstance(apply, dict):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Plan or apply evidence is not a JSON object",
        )
    for field in ("commit_sha", "parameter_fingerprint"):
        plan_value = plan.get(field)
        apply_value = apply.get(field)
        if (
            not isinstance(plan_value, str)
            or not plan_value
            or plan_value != apply_value
        ):
            raise WhatIfClassificationError(
                WhatIfClassificationCode.PROMOTION_MISMATCH,
                f"Foundation apply {field} does not match the approved plan",
            )
    if (
        plan.get("classification") != "APPROVED"
        or apply.get("classification") != "APPROVED"
    ):
        raise WhatIfClassificationError(
            WhatIfClassificationCode.PROMOTION_MISMATCH,
            "Foundation plan and apply must both be APPROVED",
        )


def _summarize(evidence: Mapping[str, Any]) -> str:
    """Render a readable, secret-free classification summary."""
    lines = [
        "Foundation what-if classification: APPROVED",
        f"  commit: {evidence['commit_sha']}",
        f"  parameter fingerprint: {evidence['parameter_fingerprint']}",
        f"  resource group: {evidence['resource_group']}",
        f"  subscription: {evidence['subscription']}",
        f"  change counts: {evidence['change_counts']}",
    ]
    for change in evidence["allowed_changes"]:
        lines.append(
            f"  {change['change_type']}: {change['resource_type']} "
            f"({change['resource_name']})"
        )
    return "\n".join(lines) + "\n"


def _load_json(path: Path, code: WhatIfClassificationCode) -> Any:
    """Load JSON from disk, converting decode failures into fail-closed errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise WhatIfClassificationError(code, f"Cannot read {path.name}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise WhatIfClassificationError(
            code, f"{path.name} is not valid JSON"
        ) from error


def _run_classify(arguments: argparse.Namespace) -> None:
    """Classify a what-if result and write sanitized plan evidence."""
    document = _load_json(arguments.whatif, WhatIfClassificationCode.MALFORMED_DOCUMENT)
    classification = classify_foundation_whatif(
        document,
        subscription_id=arguments.subscription_id,
        resource_group=arguments.resource_group,
    )
    parameters = _parse_parameter_lines(
        arguments.parameters_file.read_text(encoding="utf-8")
    )
    evidence = build_foundation_evidence(
        classification,
        commit_sha=arguments.commit_sha,
        parameter_fingerprint_value=parameter_fingerprint(parameters),
        subscription_id=arguments.subscription_id,
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
