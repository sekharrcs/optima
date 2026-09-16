"""Adversarial tests for category-B provider-echo convergence normalization.

These offline tests reproduce the observed deployed-state what-if for the exact
ten-resource foundation graph: eight NoChange resources, the cosmos account and
Application Insights component carrying only approved provider echoes, and one
policy-bound Azure OpenAI external Ignore. They prove that every approved echo
normalizes, that any genuine managed drift or malformed echo fails closed, and
that evidence v3 binds the normalization policy without leaking sensitive values.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import whatif_classification as classifier
from scripts.whatif_classification import (
    WhatIfClassificationCode,
    WhatIfClassificationError,
)

SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"
GROUP = "rg-optima-hackathon"
ENVIRONMENT = "hackathon"
SUFFIX = "abc123def456g"
COMMIT_SHA = "a" * 40

_GRAPH = classifier._expected_resource_graph(
    subscription_id=SUBSCRIPTION,
    resource_group=GROUP,
    environment_name=ENVIRONMENT,
    unique_suffix=SUFFIX,
)
_ID_BY_ROLE = {role: canonical for canonical, (role, _t, _n) in _GRAPH.items()}
COSMOS_ACCOUNT_ID = _ID_BY_ROLE["cosmos_account"]
APPI_ID = _ID_BY_ROLE["application_insights"]
COSMOS_NAME = _GRAPH[COSMOS_ACCOUNT_ID][2]
COSMOS_ENDPOINT = f"https://{COSMOS_NAME}.documents.azure.com:443/"

EXTERNAL_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
    "/providers/Microsoft.CognitiveServices/accounts/synthetic-external"
)

FOUNDATION_PARAMETERS = (
    "location=eastus2\n"
    "environmentName=hackathon\n"
    "resourceGroup=rg-optima-hackathon\n"
    "templateFile=infra/resource-group.bicep\n"
    "parameterFile=infra/environments/hackathon.foundation.bicepparam\n"
    "deployContainerApps=false\n"
    "exposePublicUi=false\n"
    "deployRuntimeAccess=false\n"
    "semanticCacheEnabled=false\n"
)


def _cosmos_profile(
    *, serverless: bool = True, analytical_disabled: bool = True
) -> dict[str, Any]:
    """Build the cosmos account payload the echo profile conditions require."""
    properties: dict[str, Any] = {}
    if analytical_disabled:
        properties["enableAnalyticalStorage"] = False
    if serverless:
        properties["capabilities"] = [{"name": "EnableServerless"}]
    return {"properties": properties}


def _cosmos_echo_delta() -> list[dict[str, Any]]:
    """The three approved cosmos account provider echoes."""
    return [
        {
            "path": "properties",
            "propertyChangeType": "Modify",
            "children": [
                {
                    "path": "analyticalStorageConfiguration",
                    "propertyChangeType": "Delete",
                    "before": {"schemaType": "WellDefined"},
                    "after": None,
                },
                {
                    "path": "enablePerRegionPerPartitionAutoscale",
                    "propertyChangeType": "Delete",
                    "before": False,
                    "after": None,
                },
                {
                    "path": "sqlEndpoint",
                    "propertyChangeType": "Delete",
                    "before": COSMOS_ENDPOINT,
                    "after": None,
                },
            ],
        }
    ]


def _appi_echo_delta() -> list[dict[str, Any]]:
    """The two approved Application Insights provider echoes."""
    return [
        {
            "path": "properties",
            "propertyChangeType": "Modify",
            "children": [
                {
                    "path": "Flow_Type",
                    "propertyChangeType": "Create",
                    "before": None,
                    "after": "Bluefield",
                },
                {
                    "path": "Request_Source",
                    "propertyChangeType": "Create",
                    "before": None,
                    "after": "rest",
                },
            ],
        }
    ]


def _cosmos_change(
    *,
    delta: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = _cosmos_profile() if payload is None else payload
    return {
        "resourceId": COSMOS_ACCOUNT_ID,
        "changeType": "Modify",
        "before": resolved,
        "after": resolved,
        "delta": _cosmos_echo_delta() if delta is None else delta,
    }


def _appi_change(*, delta: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "resourceId": APPI_ID,
        "changeType": "Modify",
        "before": {"properties": {}},
        "after": {"properties": {}},
        "delta": _appi_echo_delta() if delta is None else delta,
    }


LAW_ID = _ID_BY_ROLE["log_analytics_workspace"]

_UNSET = object()


def _law_features() -> dict[str, Any]:
    """The pinned Log Analytics desired-state features object."""
    return {
        "disableLocalAuth": True,
        "enableDataExport": False,
        "enableLogAccessUsingOnlyResourcePermissions": True,
        "immediatePurgeDataOn30Days": True,
    }


def _law_payload() -> dict[str, Any]:
    """A representative non-empty Log Analytics resource payload."""
    return {"properties": {"retentionInDays": 30, "features": _law_features()}}


def _law_noeffect_delta() -> list[dict[str, Any]]:
    """The single observed Log Analytics NoChange/NoEffect leaf node."""
    return [
        {
            "path": "properties.features",
            "propertyChangeType": "NoEffect",
            "before": None,
            "after": _law_features(),
            "children": None,
        }
    ]


def _law_change(
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    delta: Any = _UNSET,
    extension: Any = _UNSET,
) -> dict[str, Any]:
    """Build the observed Log Analytics NoChange resource carrying a NoEffect delta."""
    change: dict[str, Any] = {
        "resourceId": LAW_ID,
        "changeType": "NoChange",
        "before": _law_payload() if before is None else before,
        "after": _law_payload() if after is None else after,
        "delta": _law_noeffect_delta() if delta is _UNSET else delta,
    }
    if extension is not _UNSET:
        change["extension"] = extension
    return change


def deployed_document(
    *,
    cosmos: dict[str, Any] | None = None,
    appi: dict[str, Any] | None = None,
    law: dict[str, Any] | None = None,
    extra: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Reproduce the observed deployed-state what-if for the ten-resource graph."""
    changes: list[dict[str, Any]] = []
    for canonical, (role, _type, _name) in _GRAPH.items():
        if role == "cosmos_account":
            changes.append(cosmos if cosmos is not None else _cosmos_change())
        elif role == "application_insights":
            changes.append(appi if appi is not None else _appi_change())
        elif role == "log_analytics_workspace":
            changes.append(law if law is not None else _law_change())
        else:
            changes.append(
                {
                    "resourceId": canonical,
                    "changeType": "NoChange",
                    "before": {"p": 1},
                    "after": {"p": 1},
                    "delta": [],
                }
            )
    return {"status": "Succeeded", "changes": [*changes, *extra]}


def classify(document: Any, *, policy: Any = None) -> Any:
    return classifier.classify_foundation_whatif(
        document,
        subscription_id=SUBSCRIPTION,
        resource_group=GROUP,
        environment_name=ENVIRONMENT,
        external_policy=policy,
    )


def _assert_code(document: Any, code: WhatIfClassificationCode) -> None:
    with pytest.raises(WhatIfClassificationError) as error:
        classify(document)
    assert error.value.code is code


def evidence(document: Any | None = None) -> dict[str, Any]:
    return classifier.build_foundation_evidence(
        classify(deployed_document() if document is None else document),
        commit_sha=COMMIT_SHA,
        parameter_fingerprint_value="f" * 64,
        deployment_source_fingerprint_value="e" * 64,
        deployment_source_file_count=9,
    )


# --- Positive convergence (spec 40) -----------------------------------------


def test_deployed_state_normalizes_exactly_five_echoes() -> None:
    """The observed deployed-state what-if converges to ten managed NoChange."""
    result = classify(deployed_document())
    assert result.change_counts == {"Create": 0, "NoChange": 10}
    assert len(result.allowed_changes) == 10
    assert len(result.normalized_observations) == 5
    assert result.residual_unapproved_change_count == 0
    assert (
        result.convergence_policy_fingerprint
        == classifier.convergence_policy_fingerprint()
    )
    roles = {obs.resource_role for obs in result.normalized_observations}
    assert roles == {"cosmos_account", "application_insights"}


def test_normalized_observation_is_sanitized() -> None:
    """Normalized deltas publish only fingerprints, never the raw endpoint."""
    result = classify(deployed_document())
    serialized = json.dumps(
        [obs.to_document() for obs in result.normalized_observations]
    )
    assert COSMOS_ENDPOINT not in serialized
    assert SUBSCRIPTION not in serialized
    assert COSMOS_NAME not in serialized


def test_evidence_v3_round_trips_and_binds_policy() -> None:
    """Evidence v3 records the normalization policy and residual zero."""
    result = evidence()
    assert result["schema_version"] == classifier.EVIDENCE_SCHEMA_VERSION
    assert result["schema_version"].endswith("-v3")
    normalizations = result["normalizations"]
    assert (
        normalizations["convergence_policy_fingerprint"]
        == classifier.convergence_policy_fingerprint()
    )
    assert normalizations["residual_unapproved_change_count"] == 0
    assert len(normalizations["observations"]) == 5
    classifier.compare_promotion_evidence(result, copy.deepcopy(result))


def test_first_plan_over_missing_resources_has_no_normalizations() -> None:
    """A fresh create-only plan carries an empty, policy-bound normalization set."""
    document = {
        "status": "Succeeded",
        "changes": [
            {"resourceId": canonical, "changeType": "Create"} for canonical in _GRAPH
        ],
    }
    result = classify(document)
    assert result.change_counts == {"Create": 10, "NoChange": 0}
    assert result.normalized_observations == ()
    assert (
        result.convergence_policy_fingerprint
        == classifier.convergence_policy_fingerprint()
    )


def test_convergence_accepts_normalized_reconciliation() -> None:
    """Convergence accepts a create plan reaching a normalized NoChange state."""
    plan = evidence(
        {
            "status": "Succeeded",
            "changes": [
                {"resourceId": canonical, "changeType": "Create"}
                for canonical in _GRAPH
            ],
        }
    )
    converged = evidence(deployed_document())
    classifier.compare_convergence_evidence(plan, converged)


# --- Negative category-A: declared properties must stay blocking (spec 41) ---


def _cosmos_single_delta(path: str, op: str, before: Any, after: Any) -> dict[str, Any]:
    return _cosmos_change(
        delta=[
            {
                "path": "properties",
                "propertyChangeType": "Modify",
                "children": [
                    {
                        "path": path,
                        "propertyChangeType": op,
                        "before": before,
                        "after": after,
                    }
                ],
            }
        ]
    )


def _managed_modify(role: str, delta: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceId": _ID_BY_ROLE[role],
        "changeType": "Modify",
        "before": {"properties": {}},
        "after": {"properties": {}},
        "delta": delta,
    }


def _leaf(path: str, op: str, before: Any, after: Any) -> list[dict[str, Any]]:
    return [
        {
            "path": "properties",
            "propertyChangeType": "Modify",
            "children": [
                {
                    "path": path,
                    "propertyChangeType": op,
                    "before": before,
                    "after": after,
                }
            ],
        }
    ]


CATEGORY_A_DRIFT = [
    (
        "managed_environment",
        "peerAuthentication",
        "Modify",
        {"mtls": {"enabled": False}},
        {"mtls": {"enabled": True}},
    ),
    (
        "managed_environment",
        "peerTrafficConfiguration",
        "Modify",
        {"encryption": {"enabled": False}},
        {"encryption": {"enabled": True}},
    ),
    ("container_registry", "anonymousPullEnabled", "Modify", False, True),
    (
        "container_registry",
        "encryption",
        "Modify",
        {"status": "disabled"},
        {"status": "enabled"},
    ),
    (
        "container_registry",
        "policies",
        "Modify",
        {"azureADAuthenticationAsArmPolicy": {"status": "enabled"}},
        {"azureADAuthenticationAsArmPolicy": {"status": "disabled"}},
    ),
]


@pytest.mark.parametrize(("role", "path", "op", "before", "after"), CATEGORY_A_DRIFT)
def test_category_a_drift_fails_closed(
    role: str, path: str, op: str, before: Any, after: Any
) -> None:
    """A declared category-A property that drifts is never normalized."""
    document = deployed_document()
    for change in document["changes"]:
        if change["resourceId"] == _ID_BY_ROLE[role]:
            change.clear()
            change.update(_managed_modify(role, _leaf(path, op, before, after)))
    _assert_code(document, WhatIfClassificationCode.NORMALIZATION_REJECTED)


def test_cosmos_default_identity_drift_fails_closed() -> None:
    """The declared cosmos defaultIdentity is not a normalizable echo."""
    document = deployed_document(
        cosmos=_cosmos_single_delta(
            "defaultIdentity", "Modify", "FirstPartyIdentity", "SystemAssignedIdentity"
        )
    )
    _assert_code(document, WhatIfClassificationCode.NORMALIZATION_REJECTED)


# --- Negative category-B: exact binding required (spec 42) -------------------


CATEGORY_B_VIOLATIONS = {
    "wrong_after_flow_type": lambda: deployed_document(
        appi=_appi_change(delta=_leaf("Flow_Type", "Create", None, "Redfield"))
    ),
    "wrong_after_request_source": lambda: deployed_document(
        appi=_appi_change(delta=_leaf("Request_Source", "Create", None, "sdk"))
    ),
    "wrong_operation_sql_endpoint": lambda: deployed_document(
        cosmos=_cosmos_single_delta("sqlEndpoint", "Modify", COSMOS_ENDPOINT, "x")
    ),
    "foreign_cosmos_host": lambda: deployed_document(
        cosmos=_cosmos_single_delta(
            "sqlEndpoint", "Delete", "https://evil.documents.azure.com:443/", None
        )
    ),
    "http_cosmos_endpoint": lambda: deployed_document(
        cosmos=_cosmos_single_delta(
            "sqlEndpoint",
            "Delete",
            COSMOS_ENDPOINT.replace("https", "http"),
            None,
        )
    ),
    "alternate_port_cosmos_endpoint": lambda: deployed_document(
        cosmos=_cosmos_single_delta(
            "sqlEndpoint",
            "Delete",
            COSMOS_ENDPOINT.replace(":443/", ":8443/"),
            None,
        )
    ),
    "wrong_before_type_autoscale": lambda: deployed_document(
        cosmos=_cosmos_single_delta(
            "enablePerRegionPerPartitionAutoscale", "Delete", 0, None
        )
    ),
    "wrong_before_object": lambda: deployed_document(
        cosmos=_cosmos_single_delta(
            "analyticalStorageConfiguration",
            "Delete",
            {"schemaType": "FullFidelity"},
            None,
        )
    ),
    "analytical_waiver_when_enabled": lambda: deployed_document(
        cosmos=_cosmos_change(
            payload={
                "properties": {
                    "enableAnalyticalStorage": True,
                    "capabilities": [{"name": "EnableServerless"}],
                }
            }
        )
    ),
    "autoscale_waiver_without_serverless": lambda: deployed_document(
        cosmos=_cosmos_change(
            payload={
                "properties": {"enableAnalyticalStorage": False, "capabilities": []}
            }
        )
    ),
    "cross_role_echo": lambda: deployed_document(
        appi=_managed_modify(
            "application_insights",
            _leaf("sqlEndpoint", "Delete", COSMOS_ENDPOINT, None),
        )
    ),
    "additional_unapproved_delta": lambda: deployed_document(
        cosmos=_cosmos_change(
            delta=[
                {
                    "path": "properties",
                    "propertyChangeType": "Modify",
                    "children": [
                        {
                            "path": "analyticalStorageConfiguration",
                            "propertyChangeType": "Delete",
                            "before": {"schemaType": "WellDefined"},
                            "after": None,
                        },
                        {
                            "path": "minimalTlsVersion",
                            "propertyChangeType": "Modify",
                            "before": "Tls12",
                            "after": "Tls10",
                        },
                    ],
                }
            ]
        )
    ),
}


@pytest.mark.parametrize("name", sorted(CATEGORY_B_VIOLATIONS))
def test_category_b_violations_fail_closed(name: str) -> None:
    """Every off-policy provider echo fails closed rather than normalizing."""
    _assert_code(
        CATEGORY_B_VIOLATIONS[name](), WhatIfClassificationCode.NORMALIZATION_REJECTED
    )


# --- Action group (spec 43) --------------------------------------------------


def test_action_group_nochange_is_managed() -> None:
    """The exact adopted action group participates as a managed NoChange fact."""
    result = classify(deployed_document())
    roles = {change.resource_role for change in result.allowed_changes}
    assert "smart_detection_action_group" in roles


def test_missing_action_group_fails_graph() -> None:
    """A graph missing the action group is not the exact ten-resource contract."""
    document = deployed_document()
    document["changes"] = [
        change
        for change in document["changes"]
        if change["resourceId"] != _ID_BY_ROLE["smart_detection_action_group"]
    ]
    _assert_code(document, WhatIfClassificationCode.RESOURCE_GRAPH_MISMATCH)


def test_extra_action_group_fails_graph() -> None:
    """A duplicate action group breaks the exact ten-resource contract."""
    document = deployed_document()
    document["changes"].append(
        {
            "resourceId": (
                f"/subscriptions/{SUBSCRIPTION}/resourcegroups/{GROUP}/providers"
                "/microsoft.insights/actiongroups/another-group"
            ),
            "changeType": "NoChange",
            "before": {"p": 1},
            "after": {"p": 1},
            "delta": [],
        }
    )
    _assert_code(document, WhatIfClassificationCode.RESOURCE_GRAPH_MISMATCH)


def test_action_group_modify_is_not_normalized() -> None:
    """Action group receiver/enabled drift is a genuine blocking Modify."""
    document = deployed_document()
    for change in document["changes"]:
        if change["resourceId"] == _ID_BY_ROLE["smart_detection_action_group"]:
            change.clear()
            change.update(
                _managed_modify(
                    "smart_detection_action_group",
                    _leaf("enabled", "Modify", True, False),
                )
            )
    _assert_code(document, WhatIfClassificationCode.NORMALIZATION_REJECTED)


# --- Evidence v3 tamper resistance (spec 44) --------------------------------


def test_v2_evidence_is_rejected() -> None:
    """An old v2 evidence document is never accepted for a v3 operation."""
    result = evidence()
    legacy = copy.deepcopy(result)
    legacy["schema_version"] = "optima-foundation-whatif-evidence-v2"
    del legacy["normalizations"]
    with pytest.raises(WhatIfClassificationError) as error:
        classifier.compare_promotion_evidence(legacy, copy.deepcopy(legacy))
    assert error.value.code is WhatIfClassificationCode.PROMOTION_MISMATCH


@pytest.mark.parametrize(
    "mutation",
    [
        "policy_fingerprint",
        "residual_nonzero",
        "remove_observation",
        "duplicate_observation",
        "unbound_observation",
        "unknown_field",
    ],
)
def test_normalization_tampering_fails_closed(mutation: str) -> None:
    """Any normalization tamper, removal, addition or widening fails closed."""
    result = evidence()
    tampered = copy.deepcopy(result)
    normalizations = tampered["normalizations"]
    if mutation == "policy_fingerprint":
        normalizations["convergence_policy_fingerprint"] = "d" * 64
    elif mutation == "residual_nonzero":
        normalizations["residual_unapproved_change_count"] = 1
    elif mutation == "remove_observation":
        normalizations["observations"].pop()
    elif mutation == "duplicate_observation":
        normalizations["observations"].append(
            copy.deepcopy(normalizations["observations"][0])
        )
    elif mutation == "unbound_observation":
        normalizations["observations"][0]["json_path"] = "properties.diskEncryption"
    elif mutation == "unknown_field":
        normalizations["extra"] = True
    with pytest.raises(WhatIfClassificationError) as error:
        classifier.compare_promotion_evidence(result, tampered)
    assert error.value.code in {
        WhatIfClassificationCode.PROMOTION_MISMATCH,
        WhatIfClassificationCode.RESIDUAL_UNAPPROVED_CHANGE,
    }


def test_removing_a_normalized_delta_breaks_convergence() -> None:
    """Convergence re-validates the converged evidence and its policy binding."""
    plan = evidence(
        {
            "status": "Succeeded",
            "changes": [
                {"resourceId": canonical, "changeType": "Create"}
                for canonical in _GRAPH
            ],
        }
    )
    converged = evidence(deployed_document())
    converged["normalizations"]["convergence_policy_fingerprint"] = "d" * 64
    with pytest.raises(WhatIfClassificationError):
        classifier.compare_convergence_evidence(plan, converged)


def test_external_aoai_policy_stays_separate_and_unchanged() -> None:
    """Normalization never widens or perturbs the single AOAI external policy."""
    policy_document = {
        "schema_version": classifier.EXTERNAL_POLICY_SCHEMA_VERSION,
        "deployment_mode": "Incremental",
        "scope_fingerprint": classifier._scope_fingerprint(SUBSCRIPTION, GROUP),
        "resource_type": "microsoft.cognitiveservices/accounts",
        "resource_id_fingerprint_version": classifier.RESOURCE_ID_FINGERPRINT_VERSION,
        "resource_id_fingerprint": classifier.resource_identity_fingerprint(
            EXTERNAL_ID, subscription_id=SUBSCRIPTION, resource_group=GROUP
        ),
    }
    policy = classifier.parse_external_observation_policy(policy_document)
    ignore = {
        "resourceId": EXTERNAL_ID,
        "changeType": "Ignore",
        "before": {
            "id": EXTERNAL_ID,
            "type": "microsoft.cognitiveservices/accounts",
            "name": "synthetic-external",
            "properties": {"state": "PreserveCase"},
        },
    }
    result = classify(deployed_document(extra=(ignore,)), policy=policy)
    assert len(result.external_observations) == 1
    assert result.change_counts == {"Create": 0, "NoChange": 10}
    assert len(result.normalized_observations) == 5
    assert result.external_policy is policy
    assert result.external_observations[0].policy_fingerprint == (
        classifier.external_policy_fingerprint(policy)
    )


# --- Delta-parser hardening (closed field schema, fail closed) ---------------


def _cosmos_with(delta: list[dict[str, Any]]) -> dict[str, Any]:
    return deployed_document(cosmos=_cosmos_change(delta=delta))


_APPROVED_SQL_LEAF = {
    "path": "sqlEndpoint",
    "propertyChangeType": "Delete",
    "before": COSMOS_ENDPOINT,
    "after": None,
}


MALFORMED_DELTAS = {
    "missing_before_where_null_expected": lambda: deployed_document(
        appi=_appi_change(
            delta=[
                {
                    "path": "properties",
                    "propertyChangeType": "Modify",
                    "children": [
                        {
                            "path": "Flow_Type",
                            "propertyChangeType": "Create",
                            "after": "Bluefield",
                        }
                    ],
                }
            ]
        )
    ),
    "missing_after_where_null_expected": lambda: _cosmos_with(
        [
            {
                "path": "properties",
                "propertyChangeType": "Modify",
                "children": [
                    {
                        "path": "sqlEndpoint",
                        "propertyChangeType": "Delete",
                        "before": COSMOS_ENDPOINT,
                    }
                ],
            }
        ]
    ),
    "parent_delete_operation": lambda: _cosmos_with(
        [
            {
                "path": "properties",
                "propertyChangeType": "Delete",
                "children": [dict(_APPROVED_SQL_LEAF)],
            }
        ]
    ),
    "parent_unknown_operation": lambda: _cosmos_with(
        [
            {
                "path": "properties",
                "propertyChangeType": "Frobnicate",
                "children": [dict(_APPROVED_SQL_LEAF)],
            }
        ]
    ),
    "parent_before_after_mutation": lambda: _cosmos_with(
        [
            {
                "path": "properties",
                "propertyChangeType": "Modify",
                "before": {"analyticalStorageSchemaType": "WellDefined"},
                "after": {"analyticalStorageSchemaType": "FullFidelity"},
                "children": [dict(_APPROVED_SQL_LEAF)],
            }
        ]
    ),
    "unknown_node_field": lambda: _cosmos_with(
        [
            {
                "path": "properties",
                "propertyChangeType": "Modify",
                "children": [{**_APPROVED_SQL_LEAF, "unexpected": True}],
            }
        ]
    ),
    "empty_children": lambda: _cosmos_with(
        [{"path": "properties", "propertyChangeType": "Modify", "children": []}]
    ),
    "malformed_children": lambda: _cosmos_with(
        [{"path": "properties", "propertyChangeType": "Modify", "children": "nope"}]
    ),
    "ambiguous_path_segmentation": lambda: _cosmos_with(
        [
            {
                "path": "properties.sqlEndpoint",
                "propertyChangeType": "Delete",
                "before": COSMOS_ENDPOINT,
                "after": None,
            }
        ]
    ),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_DELTAS))
def test_malformed_delta_nodes_fail_closed(name: str) -> None:
    """Every malformed delta node fails closed as a sanitized normalization."""
    _assert_code(
        MALFORMED_DELTAS[name](), WhatIfClassificationCode.NORMALIZATION_REJECTED
    )


def test_captured_azure_delta_tree_normalizes_exactly_five() -> None:
    """A valid nested Azure delta tree normalizes exactly the five approved echoes."""
    result = classify(deployed_document())
    observed = sorted(
        (obs.resource_role, obs.json_path, obs.operation)
        for obs in result.normalized_observations
    )
    assert observed == [
        ("application_insights", "properties.Flow_Type", "Create"),
        ("application_insights", "properties.Request_Source", "Create"),
        ("cosmos_account", "properties.analyticalStorageConfiguration", "Delete"),
        (
            "cosmos_account",
            "properties.enablePerRegionPerPartitionAutoscale",
            "Delete",
        ),
        ("cosmos_account", "properties.sqlEndpoint", "Delete"),
    ]


# --- LAW NoChange/NoEffect exception -----------------------------------------

_LAW_FEATURES_FINGERPRINT = (
    "07b68d0b7cd33c7cdb95fc85fe98768406c8d4ba1b20719d8768a0dca0cdb413"
)
_CONVERGENCE_POLICY_FINGERPRINT = (
    "2f6482033f271c0504e2ef14c8a54e9170ca1e530c18c307613c1735b9bd21c6"
)


def _law_node(**overrides: Any) -> dict[str, Any]:
    """Build the observed NoEffect leaf node, applying field overrides."""
    node: dict[str, Any] = {
        "path": "properties.features",
        "propertyChangeType": "NoEffect",
        "before": None,
        "after": _law_features(),
        "children": None,
    }
    node.update(overrides)
    return node


def test_law_noeffect_nochange_is_accepted_as_a_normal_nochange() -> None:
    """The observed Log Analytics NoEffect delta classifies as a NoChange fact."""
    result = classify(deployed_document())
    assert result.change_counts == {"Create": 0, "NoChange": 10}
    law = next(
        item
        for item in result.allowed_changes
        if item.resource_role == "log_analytics_workspace"
    )
    assert law.change_type == "NoChange"
    # The exception adds no sixth normalized Modify observation.
    assert len(result.normalized_observations) == 5
    assert "log_analytics_workspace" not in {
        obs.resource_role for obs in result.normalized_observations
    }
    assert result.residual_unapproved_change_count == 0


def test_law_noeffect_delta_binds_into_the_raw_change_fingerprint() -> None:
    """The accepted NoEffect delta is bound into the raw change fingerprint."""
    with_delta = classify(deployed_document()).change_fingerprint
    without_delta = classify(
        deployed_document(
            law={
                "resourceId": LAW_ID,
                "changeType": "NoChange",
                "before": {"p": 1},
                "after": {"p": 1},
                "delta": [],
            }
        )
    ).change_fingerprint
    assert with_delta != without_delta


def test_law_noeffect_evidence_keeps_residual_zero_and_schema_v3() -> None:
    """Evidence v3 records LAW as a NoChange fact with residual zero."""
    result = evidence(deployed_document())
    assert result["schema_version"].endswith("-v3")
    normalizations = result["normalizations"]
    assert normalizations["residual_unapproved_change_count"] == 0
    assert len(normalizations["observations"]) == 5


_LAW_NEGATIVE_DELTAS: dict[str, Any] = {
    "absent_children": lambda: _law_change(
        delta=[{k: v for k, v in _law_node().items() if k != "children"}]
    ),
    "empty_children_list": lambda: _law_change(delta=[_law_node(children=[])]),
    "nonempty_children_list": lambda: _law_change(
        delta=[_law_node(children=[_law_node()])]
    ),
    "recursive_descendants": lambda: _law_change(
        delta=[_law_node(children=[{"path": "x", "children": [_law_node()]}])]
    ),
    "wrong_path": lambda: _law_change(
        delta=[_law_node(path="properties.retentionInDays")]
    ),
    "wrong_operation": lambda: _law_change(
        delta=[_law_node(propertyChangeType="Modify")]
    ),
    "unknown_field": lambda: _law_change(delta=[{**_law_node(), "unexpected": True}]),
    "missing_before_field": lambda: _law_change(
        delta=[{k: v for k, v in _law_node().items() if k != "before"}]
    ),
    "before_not_null": lambda: _law_change(delta=[_law_node(before={"x": 1})]),
    "arbitrary_after_object": lambda: _law_change(
        delta=[_law_node(after={"disableLocalAuth": True})]
    ),
    "tampered_after_value": lambda: _law_change(
        delta=[_law_node(after={**_law_features(), "disableLocalAuth": False})]
    ),
    "more_than_one_delta_entry": lambda: _law_change(delta=[_law_node(), _law_node()]),
    "resource_before_after_inequality": lambda: _law_change(
        after={"properties": {"retentionInDays": 90, "features": _law_features()}}
    ),
    "empty_before_after_objects": lambda: _law_change(before={}, after={}),
    "non_null_extension": lambda: _law_change(extension={"resourceId": "x"}),
}


@pytest.mark.parametrize("name", sorted(_LAW_NEGATIVE_DELTAS))
def test_law_noeffect_variants_fail_closed(name: str) -> None:
    """Every deviation from the single captured LAW NoEffect shape fails closed."""
    _assert_code(
        deployed_document(law=_LAW_NEGATIVE_DELTAS[name]()),
        WhatIfClassificationCode.MALFORMED_CHANGE,
    )


def test_law_noeffect_delta_on_a_non_law_resource_fails_closed() -> None:
    """A NoEffect delta on any non-LAW managed role is rejected via the validator."""
    with pytest.raises(WhatIfClassificationError) as error:
        classifier._validate_law_noeffect_nochange(
            _law_change(),
            resource_role="cosmos_account",
            resource_type="microsoft.documentdb/databaseaccounts",
        )
    assert error.value.code is WhatIfClassificationCode.MALFORMED_CHANGE


def test_plain_nochange_without_delta_keeps_default_contract() -> None:
    """An empty/absent NoChange delta stays accepted under existing semantics."""
    result = classify(
        deployed_document(
            law={
                "resourceId": LAW_ID,
                "changeType": "NoChange",
                "before": {"p": 1},
                "after": {"p": 1},
                "delta": [],
            }
        )
    )
    assert result.change_counts == {"Create": 0, "NoChange": 10}


def test_contradictory_nochange_before_after_still_rejected() -> None:
    """A contradictory NoChange before/after payload still fails closed."""
    _assert_code(
        deployed_document(
            law={
                "resourceId": LAW_ID,
                "changeType": "NoChange",
                "before": {"p": 1},
                "after": {"p": 2},
                "delta": [],
            }
        ),
        WhatIfClassificationCode.MALFORMED_CHANGE,
    )


def test_convergence_policy_fingerprint_binds_the_law_exception() -> None:
    """The convergence-policy fingerprint deterministically binds the LAW rule."""
    assert (
        classifier.convergence_policy_fingerprint() == _CONVERGENCE_POLICY_FINGERPRINT
    )
    assert classifier._LAW_FEATURES_FINGERPRINT == _LAW_FEATURES_FINGERPRINT
    canonical = classifier._canonical_json_text(classifier._LAW_FEATURES_DESIRED_STATE)
    assert (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        == _LAW_FEATURES_FINGERPRINT
    )


def test_convergence_policy_fingerprint_changed_from_echo_only() -> None:
    """Binding the LAW exception changes the fingerprint from the echo-only value."""
    echo_only = classifier._versioned_fingerprint(
        classifier.CONVERGENCE_POLICY_FINGERPRINT_VERSION,
        [
            {
                "resource_role": rule.resource_role,
                "resource_type": rule.resource_type,
                "json_path": rule.json_path,
                "operation": rule.operation,
                "before_kind": rule.before_kind,
                "after_kind": rule.after_kind,
                "profile": rule.profile,
            }
            for rule in classifier._PROVIDER_ECHO_RULES
        ],
    )
    assert classifier.convergence_policy_fingerprint() != echo_only


def test_five_provider_echo_rules_remain_unchanged() -> None:
    """The five Modify provider-echo rules stay intact alongside the exception."""
    assert len(classifier._PROVIDER_ECHO_RULES) == 5
    result = classify(deployed_document())
    assert len(result.normalized_observations) == 5


@pytest.fixture(scope="module")
def compiled_monitoring() -> dict[str, Any]:
    """Compile the monitoring module with the pinned standalone Bicep executable."""
    command = os.environ.get("OPTIMA_BICEP_COMMAND") or shutil.which("bicep")
    if command is None:
        pytest.skip("Set OPTIMA_BICEP_COMMAND to the Bicep 0.46.1 executable")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603
        [command, "build", str(root / "infra/modules/monitoring.bicep"), "--stdout"],
        capture_output=True,
        text=True,
        check=True,
    )
    template: dict[str, Any] = json.loads(result.stdout)
    return template


def test_compiled_monitoring_features_match_classifier_expectation(
    compiled_monitoring: dict[str, Any],
) -> None:
    """The compiled Log Analytics features stay synchronized with the classifier."""
    workspace = compiled_monitoring["resources"]["workspace"]
    features = workspace["properties"]["features"]
    assert features == classifier._LAW_FEATURES_DESIRED_STATE
    canonical = classifier._canonical_json_text(features)
    assert (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        == classifier._LAW_FEATURES_FINGERPRINT
    )
