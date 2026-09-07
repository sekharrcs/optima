"""Fail-closed classification tests for the foundation Azure what-if result."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.whatif_classification import (
    WhatIfClassificationCode,
    WhatIfClassificationError,
    build_foundation_evidence,
    classify_foundation_whatif,
    compare_promotion_evidence,
    main,
    parameter_fingerprint,
)

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
RESOURCE_GROUP = "rg-optima-hackathon"
COMMIT_SHA = "a" * 40

FOUNDATION_TYPES = (
    "Microsoft.App/managedEnvironments",
    "Microsoft.ContainerRegistry/registries",
    "Microsoft.DocumentDB/databaseAccounts",
    "Microsoft.DocumentDB/databaseAccounts/sqlDatabases",
    "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers",
    "Microsoft.Insights/components",
    "Microsoft.OperationalInsights/workspaces",
    "Microsoft.ManagedIdentity/userAssignedIdentities",
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
    return _document(
        *(
            _change(resource_type, name=f"res{index}")
            for index, resource_type in enumerate(FOUNDATION_TYPES)
        )
    )


def test_expected_foundation_creates_are_approved() -> None:
    """Approve a first foundation deployment of only the expected resources."""
    classification = classify_foundation_whatif(
        _foundation_creates(),
        subscription_id=SUBSCRIPTION_ID,
        resource_group=RESOURCE_GROUP,
    )

    assert classification.change_counts == {"Create": len(FOUNDATION_TYPES)}
    assert len(classification.allowed_changes) == len(FOUNDATION_TYPES)


def test_idempotent_nochange_is_approved() -> None:
    """Approve an idempotent re-plan that reports only NoChange results."""
    document = _document(
        *(
            _change(resource_type, "NoChange", before={"x": 1}, after={"x": 1})
            for resource_type in FOUNDATION_TYPES
        )
    )

    classification = classify_foundation_whatif(
        document, subscription_id=SUBSCRIPTION_ID, resource_group=RESOURCE_GROUP
    )

    assert classification.change_counts == {"NoChange": len(FOUNDATION_TYPES)}


def test_missing_status_defaults_to_structured_changes() -> None:
    """Accept output without a status field when its changes are structured."""
    classification = classify_foundation_whatif(
        _document(_change("Microsoft.ContainerRegistry/registries"), status=None),
        subscription_id=SUBSCRIPTION_ID,
        resource_group=RESOURCE_GROUP,
    )

    assert classification.change_counts == {"Create": 1}


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


@pytest.mark.parametrize("change_type", ["Deploy", "Ignore", "NoEffect"])
def test_unsupported_change_types_fail_closed(change_type: str) -> None:
    """Reject known change types the foundation profile does not allow."""
    _assert_code(
        _document(_change("Microsoft.ContainerRegistry/registries", change_type)),
        WhatIfClassificationCode.UNSUPPORTED_CHANGE,
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


def test_parameter_fingerprint_is_deterministic_and_sensitive() -> None:
    """Produce a stable fingerprint that changes when a parameter changes."""
    base = {"a": "1", "b": "2"}
    assert parameter_fingerprint(base) == parameter_fingerprint({"b": "2", "a": "1"})
    assert parameter_fingerprint(base) != parameter_fingerprint({"a": "1", "b": "3"})


def test_evidence_redacts_subscription_and_omits_resource_ids() -> None:
    """Keep plan evidence free of subscription identifiers and raw resource IDs."""
    classification = classify_foundation_whatif(
        _foundation_creates(),
        subscription_id=SUBSCRIPTION_ID,
        resource_group=RESOURCE_GROUP,
    )

    evidence = build_foundation_evidence(
        classification,
        commit_sha=COMMIT_SHA,
        parameter_fingerprint_value="f" * 64,
        subscription_id=SUBSCRIPTION_ID,
    )

    serialized = json.dumps(evidence)
    assert SUBSCRIPTION_ID not in serialized
    assert "/subscriptions/" not in serialized
    assert evidence["subscription"] == "1111...5555"
    assert evidence["classification"] == "APPROVED"


def test_promotion_evidence_matches() -> None:
    """Accept apply evidence that matches the approved plan evidence."""
    plan = {
        "commit_sha": COMMIT_SHA,
        "parameter_fingerprint": "f" * 64,
        "classification": "APPROVED",
    }
    compare_promotion_evidence(plan, dict(plan))


def test_promotion_rejects_commit_mismatch() -> None:
    """Refuse promotion when the apply commit differs from the plan."""
    plan = {
        "commit_sha": COMMIT_SHA,
        "parameter_fingerprint": "f" * 64,
        "classification": "APPROVED",
    }
    apply = {**plan, "commit_sha": "b" * 40}
    with pytest.raises(WhatIfClassificationError) as error:
        compare_promotion_evidence(plan, apply)
    assert error.value.code is WhatIfClassificationCode.PROMOTION_MISMATCH


def test_promotion_rejects_fingerprint_mismatch() -> None:
    """Refuse promotion when the effective parameters differ from the plan."""
    plan = {
        "commit_sha": COMMIT_SHA,
        "parameter_fingerprint": "f" * 64,
        "classification": "APPROVED",
    }
    apply = {**plan, "parameter_fingerprint": "e" * 64}
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
    assert SUBSCRIPTION_ID not in output.read_text(encoding="utf-8")


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


def test_cli_promote_check_rejects_mismatch(tmp_path: Path) -> None:
    """Fail the promote-check command when apply evidence drifts from the plan."""
    plan = tmp_path / "plan.json"
    apply = tmp_path / "apply.json"
    plan.write_text(
        json.dumps(
            {
                "commit_sha": COMMIT_SHA,
                "parameter_fingerprint": "f" * 64,
                "classification": "APPROVED",
            }
        ),
        encoding="utf-8",
    )
    apply.write_text(
        json.dumps(
            {
                "commit_sha": "b" * 40,
                "parameter_fingerprint": "f" * 64,
                "classification": "APPROVED",
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["promote-check", "--plan", str(plan), "--apply", str(apply)])

    assert exit_code == 1
