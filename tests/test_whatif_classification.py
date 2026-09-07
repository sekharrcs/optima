"""Fail-closed classification tests for the foundation Azure what-if result."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.whatif_classification import (
    EVIDENCE_SCHEMA_VERSION,
    WhatIfClassificationCode,
    WhatIfClassificationError,
    build_foundation_evidence,
    classify_foundation_whatif,
    compare_promotion_evidence,
    deployment_source_fingerprint,
    main,
    parameter_fingerprint,
)

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
RESOURCE_GROUP = "rg-optima-hackathon"
COMMIT_SHA = "a" * 40
ENVIRONMENT_NAME = "hackathon"
UNIQUE_SUFFIX = "abc123def456g"

SCOPE = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers"
COSMOS_ACCOUNT = f"cosmos-optima-{UNIQUE_SUFFIX}"
FOUNDATION_IDS = (
    f"{SCOPE}/Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-api-hackathon",
    f"{SCOPE}/Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-ui-hackathon",
    f"{SCOPE}/Microsoft.ContainerRegistry/registries/acroptima{UNIQUE_SUFFIX}",
    f"{SCOPE}/Microsoft.OperationalInsights/workspaces/law-optima-hackathon",
    f"{SCOPE}/Microsoft.Insights/components/appi-optima-hackathon",
    f"{SCOPE}/Microsoft.DocumentDB/databaseAccounts/{COSMOS_ACCOUNT}",
    (
        f"{SCOPE}/Microsoft.DocumentDB/databaseAccounts/{COSMOS_ACCOUNT}"
        "/sqlDatabases/optima"
    ),
    (
        f"{SCOPE}/Microsoft.DocumentDB/databaseAccounts/{COSMOS_ACCOUNT}"
        "/sqlDatabases/optima/containers/runs"
    ),
    f"{SCOPE}/Microsoft.App/managedEnvironments/cae-optima-hackathon",
)

FOUNDATION_PARAMETERS = (
    "location=eastus2\n"
    "environmentName=hackathon\n"
    "resourceGroup=rg-optima-hackathon\n"
    "templateFile=infra/resource-group.bicep\n"
    "parameterFile=infra/environments/hackathon.runtime.bicepparam\n"
    "deployContainerApps=false\n"
    "exposePublicUi=false\n"
    "deployRuntimeAccess=false\n"
    "semanticCacheEnabled=false\n"
)


def _resource_id(
    resource_type: str,
    name: str = "res",
    *,
    subscription: str = SUBSCRIPTION_ID,
    resource_group: str | None = RESOURCE_GROUP,
) -> str:
    """Build a realistic ARM resource ID for a resource type."""
    segments = resource_type.split("/")
    namespace = segments[0]
    type_names = segments[1:]
    if resource_group is None:
        prefix = f"/subscriptions/{subscription}/providers/{namespace}"
    else:
        prefix = (
            f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
            f"/providers/{namespace}"
        )
    parts = [prefix]
    for index, type_name in enumerate(type_names):
        suffix = name if index == len(type_names) - 1 else f"{name}{index}"
        parts.append(f"{type_name}/{suffix}")
    return "/".join(parts)


def _change(
    resource_type: str,
    change_type: str = "Create",
    name: str = "res",
    **extra: Any,
) -> dict[str, Any]:
    """Build a single what-if change entry."""
    change: dict[str, Any] = {
        "resourceId": _resource_id(resource_type, name),
        "changeType": change_type,
    }
    change.update(extra)
    return change


def _change_id(
    resource_id: str, change_type: str = "Create", **extra: Any
) -> dict[str, Any]:
    """Build a change for one exact ARM resource ID."""
    change: dict[str, Any] = {
        "resourceId": resource_id,
        "changeType": change_type,
    }
    change.update(extra)
    return change


def _document(
    *changes: dict[str, Any], status: str | None = "Succeeded"
) -> dict[str, Any]:
    """Build a structured what-if operation result."""
    document: dict[str, Any] = {"changes": list(changes)}
    if status is not None:
        document["status"] = status
    return document


def _foundation_creates() -> dict[str, Any]:
    """Build a first-deployment what-if creating every foundation resource."""
    return _document(*(_change_id(resource_id) for resource_id in FOUNDATION_IDS))


def _foundation_nochanges() -> dict[str, Any]:
    """Build a converged what-if for the exact foundation graph."""
    return _document(
        *(
            _change_id(
                resource_id,
                "NoChange",
                before={"properties": {"state": "same"}},
                after={"properties": {"state": "same"}},
                delta=[],
            )
            for resource_id in FOUNDATION_IDS
        )
    )


def _classification(document: Any | None = None) -> Any:
    """Classify one exact foundation graph fixture."""
    return classify_foundation_whatif(
        _foundation_creates() if document is None else document,
        subscription_id=SUBSCRIPTION_ID,
        resource_group=RESOURCE_GROUP,
        environment_name=ENVIRONMENT_NAME,
    )


def _evidence(document: Any | None = None) -> dict[str, Any]:
    """Build valid closed promotion evidence for a graph fixture."""
    return build_foundation_evidence(
        _classification(document),
        commit_sha=COMMIT_SHA,
        parameter_fingerprint_value="f" * 64,
        deployment_source_fingerprint_value="e" * 64,
        deployment_source_file_count=9,
    )


def test_expected_foundation_creates_are_approved() -> None:
    """Approve a first foundation deployment of only the expected resources."""
    classification = _classification()

    assert classification.change_counts == {"Create": 9, "NoChange": 0}
    assert len(classification.allowed_changes) == 9
    assert {change.resource_role for change in classification.allowed_changes} == {
        "api_identity",
        "application_insights",
        "container_registry",
        "cosmos_account",
        "cosmos_container",
        "cosmos_database",
        "log_analytics_workspace",
        "managed_environment",
        "ui_identity",
    }


def test_idempotent_nochange_is_approved() -> None:
    """Approve an idempotent re-plan that reports only NoChange results."""
    classification = _classification(_foundation_nochanges())

    assert classification.change_counts == {"Create": 0, "NoChange": 9}


def test_missing_status_fails_closed() -> None:
    """Reject output without an exact successful terminal status."""
    _assert_code(
        _document(*_foundation_creates()["changes"], status=None),
        WhatIfClassificationCode.OPERATION_NOT_SUCCEEDED,
    )


def _assert_code(document: Any, code: WhatIfClassificationCode) -> None:
    """Assert classification fails closed with the expected stable code."""
    with pytest.raises(WhatIfClassificationError) as error:
        classify_foundation_whatif(
            document, subscription_id=SUBSCRIPTION_ID, resource_group=RESOURCE_GROUP
        )
    assert error.value.code is code


def test_delete_fails_closed() -> None:
    """Reject any deletion in the foundation what-if."""
    _assert_code(
        _document(_change("Microsoft.ContainerRegistry/registries", "Delete")),
        WhatIfClassificationCode.DELETE_REJECTED,
    )


def test_replacement_fails_closed() -> None:
    """Reject a create that replaces an existing resource."""
    _assert_code(
        _document(
            _change(
                "Microsoft.ContainerRegistry/registries",
                "Create",
                before={"name": "existing"},
            )
        ),
        WhatIfClassificationCode.REPLACEMENT_REJECTED,
    )


def test_unexpected_modify_fails_closed() -> None:
    """Reject a modification of an existing foundation resource."""
    _assert_code(
        _document(_change("Microsoft.DocumentDB/databaseAccounts", "Modify")),
        WhatIfClassificationCode.UNEXPECTED_MODIFY,
    )


def test_role_assignment_fails_closed() -> None:
    """Reject any role assignment change."""
    _assert_code(
        _document(_change("Microsoft.Authorization/roleAssignments")),
        WhatIfClassificationCode.ROLE_ASSIGNMENT_CHANGE,
    )


def test_azure_openai_account_fails_closed() -> None:
    """Reject an Azure OpenAI account change."""
    _assert_code(
        _document(_change("Microsoft.CognitiveServices/accounts")),
        WhatIfClassificationCode.AZURE_OPENAI_CHANGE,
    )


def test_azure_openai_deployment_fails_closed() -> None:
    """Reject an Azure OpenAI (embedding or model) deployment change."""
    _assert_code(
        _document(_change("Microsoft.CognitiveServices/accounts/deployments")),
        WhatIfClassificationCode.AZURE_OPENAI_CHANGE,
    )


def test_redis_fails_closed() -> None:
    """Reject any Managed Redis change."""
    _assert_code(
        _document(_change("Microsoft.Cache/redisEnterprise")),
        WhatIfClassificationCode.REDIS_CHANGE,
    )


def test_redis_access_policy_fails_closed() -> None:
    """Reject a Redis access-policy assignment change."""
    _assert_code(
        _document(
            _change("Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments")
        ),
        WhatIfClassificationCode.REDIS_CHANGE,
    )


def test_container_app_fails_closed() -> None:
    """Reject any Container App revision change."""
    _assert_code(
        _document(_change("Microsoft.App/containerApps")),
        WhatIfClassificationCode.APPLICATION_CHANGE,
    )


def test_smoke_job_fails_closed() -> None:
    """Reject the application smoke job change."""
    _assert_code(
        _document(_change("Microsoft.App/jobs")),
        WhatIfClassificationCode.APPLICATION_CHANGE,
    )


def test_unknown_resource_type_fails_closed() -> None:
    """Reject a resource type outside the reviewed foundation contract."""
    _assert_code(
        _document(_change("Microsoft.Storage/storageAccounts")),
        WhatIfClassificationCode.UNEXPECTED_RESOURCE_TYPE,
    )


@pytest.mark.parametrize("change_type", ["Deploy", "Ignore", "Unsupported"])
def test_unsupported_change_types_fail_closed(change_type: str) -> None:
    """Reject known change types the foundation profile does not allow."""
    _assert_code(
        _document(_change("Microsoft.ContainerRegistry/registries", change_type)),
        WhatIfClassificationCode.UNSUPPORTED_CHANGE,
    )


def test_resource_level_noeffect_is_unclassifiable() -> None:
    """Reject NoEffect because it is not an official resource change type."""
    _assert_code(
        _document(_change("Microsoft.ContainerRegistry/registries", "NoEffect")),
        WhatIfClassificationCode.UNCLASSIFIABLE_CHANGE_TYPE,
    )


def test_unclassifiable_change_type_fails_closed() -> None:
    """Reject an unknown change type instead of guessing intent."""
    _assert_code(
        _document(_change("Microsoft.ContainerRegistry/registries", "Frobnicate")),
        WhatIfClassificationCode.UNCLASSIFIABLE_CHANGE_TYPE,
    )


def test_unsupported_reason_fails_closed() -> None:
    """Reject any change that Azure marked unsupported."""
    _assert_code(
        _document(
            _change(
                "Microsoft.ContainerRegistry/registries",
                "Create",
                unsupportedReason="preview resource",
            )
        ),
        WhatIfClassificationCode.UNSUPPORTED_CHANGE,
    )


def test_resource_in_other_group_fails_closed() -> None:
    """Reject a resource created outside the approved resource group."""
    change = {
        "resourceId": _resource_id(
            "Microsoft.ContainerRegistry/registries", resource_group="rg-other"
        ),
        "changeType": "Create",
    }
    _assert_code(_document(change), WhatIfClassificationCode.RESOURCE_OUTSIDE_SCOPE)


def test_subscription_scoped_change_fails_closed() -> None:
    """Reject a subscription-scoped change outside the resource group."""
    change = {
        "resourceId": _resource_id(
            "Microsoft.Authorization/roleAssignments", resource_group=None
        ),
        "changeType": "Create",
    }
    _assert_code(_document(change), WhatIfClassificationCode.RESOURCE_OUTSIDE_SCOPE)


def test_non_object_document_fails_closed() -> None:
    """Reject a what-if payload that is not a JSON object."""
    _assert_code([], WhatIfClassificationCode.MALFORMED_DOCUMENT)


def test_missing_changes_fails_closed() -> None:
    """Reject a what-if payload with no structured changes array."""
    _assert_code(
        {"status": "Succeeded"}, WhatIfClassificationCode.NO_STRUCTURED_CHANGES
    )


def test_changes_not_a_list_fails_closed() -> None:
    """Reject truncated or schema-drifted changes that are not a list."""
    _assert_code(
        {"status": "Succeeded", "changes": "truncated"},
        WhatIfClassificationCode.NO_STRUCTURED_CHANGES,
    )


def test_empty_changes_fails_closed() -> None:
    """Reject an empty change set that reports no foundation intent."""
    _assert_code(_document(), WhatIfClassificationCode.NO_STRUCTURED_CHANGES)


def test_operation_not_succeeded_fails_closed() -> None:
    """Reject a what-if whose own operation did not succeed."""
    _assert_code(
        _document(_change("Microsoft.ContainerRegistry/registries"), status="Failed"),
        WhatIfClassificationCode.OPERATION_NOT_SUCCEEDED,
    )


def test_malformed_change_entry_fails_closed() -> None:
    """Reject a change entry missing its change type or resource ID."""
    _assert_code(
        _document({"resourceId": "/subscriptions/x"}),
        WhatIfClassificationCode.MALFORMED_CHANGE,
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "potentialChanges",
            [_change("Microsoft.CognitiveServices/accounts")],
            WhatIfClassificationCode.POTENTIAL_CHANGES,
        ),
        (
            "diagnostics",
            [{"level": "Info", "code": "Info", "message": "unreviewed"}],
            WhatIfClassificationCode.DIAGNOSTICS,
        ),
        (
            "diagnostics",
            [{"level": "Warning", "code": "Warning", "message": "unreviewed"}],
            WhatIfClassificationCode.DIAGNOSTICS,
        ),
        (
            "diagnostics",
            [{"level": "Error", "code": "Error", "message": "failed"}],
            WhatIfClassificationCode.DIAGNOSTICS,
        ),
        (
            "error",
            {"code": "DeploymentWhatIfError", "message": "failed"},
            WhatIfClassificationCode.SERVICE_ERROR,
        ),
    ],
)
def test_official_unreviewed_fields_fail_closed(
    field: str, value: Any, code: WhatIfClassificationCode
) -> None:
    """Reject every unreviewed official operation-result signal."""
    document = _foundation_creates()
    document[field] = value
    _assert_code(document, code)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("potentialChanges", {}),
        ("diagnostics", "warning"),
        ("error", "failed"),
    ],
)
def test_malformed_official_fields_fail_closed(field: str, value: Any) -> None:
    """Reject malformed official operation-result fields."""
    document = _foundation_creates()
    document[field] = value
    _assert_code(document, WhatIfClassificationCode.MALFORMED_DOCUMENT)


def test_empty_reviewed_official_fields_are_accepted() -> None:
    """Accept explicitly empty optional arrays and a null service error."""
    document = _foundation_creates()
    document.update(potentialChanges=[], diagnostics=[], error=None)

    assert len(_classification(document).allowed_changes) == 9


def test_unknown_top_level_field_fails_closed() -> None:
    """Reject a future schema field until its security meaning is reviewed."""
    document = _foundation_creates()
    document["futureField"] = []
    _assert_code(document, WhatIfClassificationCode.MALFORMED_DOCUMENT)


def test_unknown_or_malformed_change_field_fails_closed() -> None:
    """Reject unsupported official-schema shapes within a resource change."""
    document = _foundation_creates()
    document["changes"][0]["futureField"] = "unreviewed"
    _assert_code(document, WhatIfClassificationCode.MALFORMED_CHANGE)

    document = _foundation_creates()
    document["changes"][0]["identifiers"] = ["invalid"]
    _assert_code(document, WhatIfClassificationCode.MALFORMED_CHANGE)


def test_mixed_case_canonical_ids_are_accepted() -> None:
    """Treat ARM scope, provider, type, and resource-name casing uniformly."""
    document = _foundation_creates()
    for change in document["changes"]:
        change["resourceId"] = change["resourceId"].swapcase()

    assert (
        _classification(document).change_fingerprint
        == _classification().change_fingerprint
    )


@pytest.mark.parametrize(
    "malformed_id",
    [
        FOUNDATION_IDS[2] + "/",
        FOUNDATION_IDS[2].replace("/providers/", "//providers/"),
        FOUNDATION_IDS[2].replace("/providers/", "/junk/parent/providers/"),
        FOUNDATION_IDS[2].replace("/registries/", "/registries\\"),
        FOUNDATION_IDS[2].replace("/providers/", "/%2Fproviders%2F"),
        FOUNDATION_IDS[2] + "\n",
        FOUNDATION_IDS[2].rsplit("/", 1)[0],
        (
            f"{SCOPE}/Microsoft.Authorization/roleAssignments/outer/providers/"
            f"Microsoft.ContainerRegistry/registries/acroptima{UNIQUE_SUFFIX}"
        ),
        (
            f"{SCOPE}/Microsoft.Authorization/roleAssignments/outer/providers/"
            "Microsoft.Cache/redisEnterprise/hidden/providers/"
            f"Microsoft.ContainerRegistry/registries/acroptima{UNIQUE_SUFFIX}"
        ),
    ],
)
def test_malformed_resource_ids_fail_closed(malformed_id: str) -> None:
    """Reject separators, redundant scope, missing names, and provider smuggling."""
    document = _foundation_creates()
    document["changes"][2]["resourceId"] = malformed_id
    _assert_code(document, WhatIfClassificationCode.MALFORMED_RESOURCE_ID)


def test_exact_resource_group_scope_is_required() -> None:
    """Reject a prefix collision with the approved resource-group name."""
    document = _foundation_creates()
    document["changes"][2]["resourceId"] = FOUNDATION_IDS[2].replace(
        RESOURCE_GROUP, f"{RESOURCE_GROUP}-other"
    )
    _assert_code(document, WhatIfClassificationCode.RESOURCE_OUTSIDE_SCOPE)


def test_duplicate_and_extra_resources_fail_closed() -> None:
    """Reject duplicate IDs and extra same-type resources."""
    document = _foundation_creates()
    document["changes"].append(copy.deepcopy(document["changes"][0]))
    _assert_code(document, WhatIfClassificationCode.DUPLICATE_RESOURCE)

    document = _foundation_creates()
    document["changes"].append(
        _change_id(f"{SCOPE}/Microsoft.ContainerRegistry/registries/acroptextr00001")
    )
    _assert_code(document, WhatIfClassificationCode.RESOURCE_GRAPH_MISMATCH)


def test_missing_resource_fails_closed() -> None:
    """Require all nine resource instances, including both identities."""
    document = _foundation_creates()
    document["changes"].pop(1)
    _assert_code(document, WhatIfClassificationCode.RESOURCE_GRAPH_MISMATCH)


@pytest.mark.parametrize(
    ("index", "old", "new"),
    [
        (0, "id-optima-api-hackathon", "id-optima-api-other"),
        (1, "id-optima-ui-hackathon", "id-optima-ui-other"),
        (3, "law-optima-hackathon", "law-optima-other"),
        (4, "appi-optima-hackathon", "appi-optima-other"),
        (8, "cae-optima-hackathon", "cae-optima-other"),
        (6, "/sqlDatabases/optima", "/sqlDatabases/other"),
        (7, "/containers/runs", "/containers/other"),
        (7, COSMOS_ACCOUNT, f"cosmos-optima-{'z' * 13}"),
    ],
)
def test_wrong_graph_name_or_parent_fails_closed(
    index: int, old: str, new: str
) -> None:
    """Reject wrong names and wrong Cosmos account/database parents."""
    document = _foundation_creates()
    document["changes"][index]["resourceId"] = document["changes"][index][
        "resourceId"
    ].replace(old, new)
    _assert_code(document, WhatIfClassificationCode.RESOURCE_GRAPH_MISMATCH)


def test_registry_and_cosmos_suffixes_must_match() -> None:
    """Require the ACR and Cosmos account to share one 13-character suffix."""
    document = _foundation_creates()
    document["changes"][2]["resourceId"] = FOUNDATION_IDS[2].replace(
        UNIQUE_SUFFIX, "z" * 13
    )
    _assert_code(document, WhatIfClassificationCode.RESOURCE_GRAPH_MISMATCH)


def test_order_independent_change_fingerprint() -> None:
    """Hash equivalent resource-change sets identically regardless of order."""
    reordered = _foundation_creates()
    reordered["changes"].reverse()

    assert (
        _classification().change_fingerprint
        == _classification(reordered).change_fingerprint
    )


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        (
            "after",
            {"properties": {"sku": "Basic"}},
            {"properties": {"sku": "Standard"}},
        ),
        ("delta", [{"path": "properties.a", "propertyChangeType": "Create"}], []),
        ("identifiers", {"apiVersion": "1"}, {"apiVersion": "2"}),
    ],
)
def test_security_relevant_change_payload_affects_fingerprint(
    field: str, first: Any, second: Any
) -> None:
    """Bind full structured before/after/delta/identifier payload to approval."""
    plan = _foundation_creates()
    apply = copy.deepcopy(plan)
    plan["changes"][2][field] = first
    apply["changes"][2][field] = second

    assert (
        _classification(plan).change_fingerprint
        != _classification(apply).change_fingerprint
    )


def test_different_safe_looking_change_types_have_different_fingerprints() -> None:
    """Supersede reduced allowed-change equality with complete change binding."""
    apply = _foundation_creates()
    apply["changes"][2]["changeType"] = "NoChange"

    assert (
        _classification().change_fingerprint
        != _classification(apply).change_fingerprint
    )


def test_nochange_contradiction_fails_closed() -> None:
    """Reject a NoChange entry whose structured payload reports a difference."""
    document = _foundation_nochanges()
    document["changes"][0]["after"] = {"properties": {"state": "different"}}
    _assert_code(document, WhatIfClassificationCode.MALFORMED_CHANGE)


def test_parameter_fingerprint_is_deterministic_and_sensitive() -> None:
    """Produce a stable fingerprint that changes when a parameter changes."""
    base = {"a": "1", "b": "2"}
    assert parameter_fingerprint(base) == parameter_fingerprint({"b": "2", "a": "1"})
    assert parameter_fingerprint(base) != parameter_fingerprint({"a": "1", "b": "3"})


def _write_source_tree(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Write a minimal deterministic Bicep source graph for fingerprint tests."""
    infra = tmp_path / "infra"
    modules = infra / "modules"
    environments = infra / "environments"
    modules.mkdir(parents=True)
    environments.mkdir()
    (infra / "resource-group.bicep").write_text(
        "module child 'modules/child.bicep' = {\n  name: 'child'\n}\n",
        encoding="utf-8",
    )
    (modules / "child.bicep").write_text(
        "param location string = 'eastus2'\n", encoding="utf-8"
    )
    (environments / "foundation.bicepparam").write_text(
        "using '../resource-group.bicep'\nparam location = 'eastus2'\n",
        encoding="utf-8",
    )
    parameters = {
        "templateFile": "infra/resource-group.bicep",
        "parameterFile": "infra/environments/foundation.bicepparam",
    }
    return tmp_path, parameters


def test_deployment_source_fingerprint_covers_template_parameter_and_modules(
    tmp_path: Path,
) -> None:
    """Change the digest when any actual deployment source content changes."""
    root, parameters = _write_source_tree(tmp_path)
    original, count = deployment_source_fingerprint(parameters, source_root=root)
    assert count == 3

    module = root / "infra" / "modules" / "child.bicep"
    module.write_text("param location string = 'westus'\n", encoding="utf-8")
    module_drift, _ = deployment_source_fingerprint(parameters, source_root=root)
    assert module_drift != original

    module.write_text("param location string = 'eastus2'\n", encoding="utf-8")
    parameter_file = root / "infra" / "environments" / "foundation.bicepparam"
    parameter_file.write_text(
        "using '../resource-group.bicep'\nparam location = 'westus'\n",
        encoding="utf-8",
    )
    parameter_drift, _ = deployment_source_fingerprint(parameters, source_root=root)
    assert parameter_drift != original


def test_scope_fingerprint_is_sensitive_to_subscription_and_group() -> None:
    """Bind evidence to the full target scope without exposing the subscription."""
    other_subscription = "22222222-2222-3333-4444-555555555555"
    other_document = _foundation_creates()
    for change in other_document["changes"]:
        change["resourceId"] = change["resourceId"].replace(
            SUBSCRIPTION_ID, other_subscription
        )
    other = classify_foundation_whatif(
        other_document,
        subscription_id=other_subscription,
        resource_group=RESOURCE_GROUP,
    )

    assert _classification().scope_fingerprint != other.scope_fingerprint


def test_evidence_redacts_subscription_and_omits_resource_ids() -> None:
    """Keep plan evidence free of subscription identifiers and raw resource IDs."""
    evidence = _evidence()

    serialized = json.dumps(evidence)
    assert SUBSCRIPTION_ID not in serialized
    assert "/subscriptions/" not in serialized
    assert evidence["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert evidence["classification"] == "APPROVED"
    assert len(evidence["changes"]["resources"]) == 9
    assert (
        evidence["target"]["scope_fingerprint"] == _classification().scope_fingerprint
    )
    assert evidence["changes"]["fingerprint"] == _classification().change_fingerprint


def test_promotion_evidence_matches() -> None:
    """Accept apply evidence that matches the approved plan evidence."""
    plan = _evidence()
    compare_promotion_evidence(plan, dict(plan))


def test_promotion_rejects_commit_mismatch() -> None:
    """Refuse promotion when the apply commit differs from the plan."""
    plan = _evidence()
    apply = {**plan, "commit_sha": "b" * 40}
    with pytest.raises(WhatIfClassificationError) as error:
        compare_promotion_evidence(plan, apply)
    assert error.value.code is WhatIfClassificationCode.PROMOTION_MISMATCH


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("target", "scope_fingerprint"),
        ("deployment_source", "fingerprint"),
        ("parameters", "fingerprint"),
        ("changes", "fingerprint"),
    ],
)
def test_promotion_rejects_every_fingerprint_drift(section: str, field: str) -> None:
    """Refuse target, source, parameter, and complete change-set drift."""
    plan = _evidence()
    apply = copy.deepcopy(plan)
    apply[section][field] = "d" * 64
    with pytest.raises(WhatIfClassificationError) as error:
        compare_promotion_evidence(plan, apply)
    assert error.value.code is WhatIfClassificationCode.PROMOTION_MISMATCH


def test_promotion_rejects_different_approved_change_set() -> None:
    """Reject different safe-looking changes even with forged matching counts."""
    plan = _evidence()
    apply = copy.deepcopy(plan)
    apply["changes"]["resources"][2]["change_type"] = "NoChange"
    apply["changes"]["counts"] = {"Create": 8, "NoChange": 1}

    with pytest.raises(WhatIfClassificationError) as error:
        compare_promotion_evidence(plan, apply)
    assert error.value.code is WhatIfClassificationCode.PROMOTION_MISMATCH


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_schema",
        "unknown_top_level",
        "unknown_nested",
        "unknown_count",
        "missing_change_fingerprint",
        "missing_resource_fact_field",
        "resource_fact_unknown_field",
        "noncanonical_resource_order",
        "count_contradiction",
    ],
)
def test_promotion_rejects_unknown_missing_or_contradictory_schema(
    mutation: str,
) -> None:
    """Validate the closed evidence schema before comparing promotion facts."""
    plan = _evidence()
    apply = copy.deepcopy(plan)
    if mutation == "missing_schema":
        del apply["schema_version"]
    elif mutation == "unknown_top_level":
        apply["unknown"] = True
    elif mutation == "unknown_nested":
        apply["target"]["subscription"] = "redacted"
    elif mutation == "unknown_count":
        apply["changes"]["counts"]["Modify"] = 0
    elif mutation == "missing_change_fingerprint":
        del apply["changes"]["fingerprint"]
    elif mutation == "missing_resource_fact_field":
        del apply["changes"]["resources"][0]["resource_role"]
    elif mutation == "resource_fact_unknown_field":
        apply["changes"]["resources"][0]["resource_id"] = "redacted"
    elif mutation == "noncanonical_resource_order":
        apply["changes"]["resources"].reverse()
    elif mutation == "count_contradiction":
        apply["changes"]["counts"] = {"Create": 8, "NoChange": 1}

    with pytest.raises(WhatIfClassificationError) as error:
        compare_promotion_evidence(plan, apply)
    assert error.value.code is WhatIfClassificationCode.PROMOTION_MISMATCH


def _write_parameters(tmp_path: Path) -> Path:
    parameters = tmp_path / "parameters.txt"
    parameters.write_text(FOUNDATION_PARAMETERS, encoding="utf-8")
    return parameters


def test_cli_classify_writes_sanitized_evidence(tmp_path: Path) -> None:
    """Run the classify command end to end and emit approved evidence."""
    whatif = tmp_path / "whatif.json"
    whatif.write_text(json.dumps(_foundation_creates()), encoding="utf-8")
    output = tmp_path / "evidence.json"

    exit_code = main(
        [
            "classify",
            "--whatif",
            str(whatif),
            "--subscription-id",
            SUBSCRIPTION_ID,
            "--resource-group",
            RESOURCE_GROUP,
            "--commit-sha",
            COMMIT_SHA,
            "--parameters-file",
            str(_write_parameters(tmp_path)),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["classification"] == "APPROVED"
    assert evidence["commit_sha"] == COMMIT_SHA
    assert evidence["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert len(evidence["changes"]["resources"]) == 9
    assert evidence["deployment_source"]["file_count"] == 9
    assert SUBSCRIPTION_ID not in output.read_text(encoding="utf-8")
    assert "/subscriptions/" not in output.read_text(encoding="utf-8")


def test_cli_classify_fails_closed_on_forbidden_change(tmp_path: Path) -> None:
    """Fail the classify command when the what-if includes a forbidden change."""
    whatif = tmp_path / "whatif.json"
    whatif.write_text(
        json.dumps(_document(_change("Microsoft.Cache/redisEnterprise"))),
        encoding="utf-8",
    )
    output = tmp_path / "evidence.json"

    exit_code = main(
        [
            "classify",
            "--whatif",
            str(whatif),
            "--subscription-id",
            SUBSCRIPTION_ID,
            "--resource-group",
            RESOURCE_GROUP,
            "--commit-sha",
            COMMIT_SHA,
            "--parameters-file",
            str(_write_parameters(tmp_path)),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 1
    assert not output.exists()


def test_cli_classify_fails_closed_on_malformed_json(tmp_path: Path) -> None:
    """Fail the classify command when the what-if file is not valid JSON."""
    whatif = tmp_path / "whatif.json"
    whatif.write_text("{ not valid json", encoding="utf-8")

    exit_code = main(
        [
            "classify",
            "--whatif",
            str(whatif),
            "--subscription-id",
            SUBSCRIPTION_ID,
            "--resource-group",
            RESOURCE_GROUP,
            "--commit-sha",
            COMMIT_SHA,
            "--parameters-file",
            str(_write_parameters(tmp_path)),
        ]
    )

    assert exit_code == 1


@pytest.mark.parametrize(
    "whatif_text",
    [
        '{"status":"Succeeded","status":"Succeeded","changes":[]}',
        '{"status":"Succeeded","changes":[{"changeType":"Create",'
        '"resourceId":"redacted","after":{"value":NaN}}]}',
        '{"status":"Succeeded","changes":[{"changeType":"Create",'
        '"resourceId":"redacted","after":{"value":Infinity}}]}',
    ],
)
def test_cli_classify_rejects_duplicate_keys_and_nonfinite_json(
    tmp_path: Path, whatif_text: str
) -> None:
    """Reject JSON extensions that could make fingerprints parser-dependent."""
    whatif = tmp_path / "whatif.json"
    whatif.write_text(whatif_text, encoding="utf-8")

    exit_code = main(
        [
            "classify",
            "--whatif",
            str(whatif),
            "--subscription-id",
            SUBSCRIPTION_ID,
            "--resource-group",
            RESOURCE_GROUP,
            "--commit-sha",
            COMMIT_SHA,
            "--parameters-file",
            str(_write_parameters(tmp_path)),
        ]
    )

    assert exit_code == 1


def test_cli_promote_check_accepts_strict_matching_evidence(tmp_path: Path) -> None:
    """Accept two strict evidence files with exact canonical facts."""
    plan = tmp_path / "plan.json"
    apply = tmp_path / "apply.json"
    evidence = _evidence()
    plan.write_text(json.dumps(evidence), encoding="utf-8")
    apply.write_text(json.dumps(evidence), encoding="utf-8")

    assert main(["promote-check", "--plan", str(plan), "--apply", str(apply)]) == 0


def test_cli_promote_check_rejects_mismatch(tmp_path: Path) -> None:
    """Fail the promote-check command when apply evidence drifts from the plan."""
    plan = tmp_path / "plan.json"
    apply = tmp_path / "apply.json"
    plan_evidence = _evidence()
    apply_evidence = copy.deepcopy(plan_evidence)
    apply_evidence["commit_sha"] = "b" * 40
    plan.write_text(json.dumps(plan_evidence), encoding="utf-8")
    apply.write_text(json.dumps(apply_evidence), encoding="utf-8")

    exit_code = main(["promote-check", "--plan", str(plan), "--apply", str(apply)])

    assert exit_code == 1


def test_cli_promote_check_rejects_duplicate_evidence_key(tmp_path: Path) -> None:
    """Reject duplicate keys before validating or comparing evidence."""
    plan = tmp_path / "plan.json"
    apply = tmp_path / "apply.json"
    plan.write_text('{"schema_version":"v1","schema_version":"v1"}', encoding="utf-8")
    apply.write_text(json.dumps(_evidence()), encoding="utf-8")

    assert main(["promote-check", "--plan", str(plan), "--apply", str(apply)]) == 1
