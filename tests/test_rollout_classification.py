"""Fail-closed classification tests for the Container Apps rollout what-if.

Every adversarial fixture here fails closed against the exact rollout state
transition contract. These regressions did not exist at PR head 9705e4c, where
the internal and public rollouts ran ``az deployment group what-if`` and then
``create`` with no classification at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import whatif_classification as classifier
from scripts.whatif_classification import (
    WhatIfClassificationCode,
    WhatIfClassificationError,
    classify_rollout_whatif,
    main,
)

SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"
GROUP = "rg-optima-hackathon"
SCOPE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers"
SUFFIX = "abc123def456g"
API_DIGEST = "sha256:" + ("a" * 64)
UI_DIGEST = "sha256:" + ("b" * 64)
REGISTRY = f"acroptima{SUFFIX}.azurecr.io"
COMMIT_SHA = "c" * 40

FOUNDATION_IDS = (
    f"{SCOPE}/Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-api-hackathon",
    f"{SCOPE}/Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-ui-hackathon",
    f"{SCOPE}/Microsoft.ContainerRegistry/registries/acroptima{SUFFIX}",
    f"{SCOPE}/Microsoft.OperationalInsights/workspaces/law-optima-hackathon",
    f"{SCOPE}/Microsoft.Insights/components/appi-optima-hackathon",
    f"{SCOPE}/Microsoft.DocumentDB/databaseAccounts/cosmos-optima-{SUFFIX}",
    (
        f"{SCOPE}/Microsoft.DocumentDB/databaseAccounts/cosmos-optima-{SUFFIX}"
        "/sqlDatabases/optima"
    ),
    (
        f"{SCOPE}/Microsoft.DocumentDB/databaseAccounts/cosmos-optima-{SUFFIX}"
        "/sqlDatabases/optima/containers/runs"
    ),
    f"{SCOPE}/Microsoft.App/managedEnvironments/cae-optima-hackathon",
    f"{SCOPE}/Microsoft.Insights/actionGroups/Application Insights Smart Detection",
)
API_ID = f"{SCOPE}/Microsoft.App/containerApps/ca-optima-api-hackathon"
UI_ID = f"{SCOPE}/Microsoft.App/containerApps/ca-optima-ui-hackathon"
UI_AUTH_ID = (
    f"{SCOPE}/Microsoft.App/containerApps/ca-optima-ui-hackathon/authConfigs/current"
)
JOB_ID = f"{SCOPE}/Microsoft.App/jobs/caj-optima-smoke-hackathon"


def _nochange(resource_id: str, payload: Any | None = None) -> dict[str, Any]:
    body = payload if payload is not None else {"properties": {"state": "same"}}
    return {
        "resourceId": resource_id,
        "changeType": "NoChange",
        "before": body,
        "after": body,
        "delta": [],
    }


def _app_payload(image: str, external: bool) -> dict[str, Any]:
    return {
        "properties": {
            "configuration": {"ingress": {"external": external, "targetPort": 8000}},
            "template": {"containers": [{"name": "c", "image": image}]},
        }
    }


def _foundation_nochanges() -> list[dict[str, Any]]:
    return [_nochange(resource_id) for resource_id in FOUNDATION_IDS]


def _internal_apps() -> list[dict[str, Any]]:
    return [
        {
            "resourceId": API_ID,
            "changeType": "Create",
            "after": _app_payload(f"{REGISTRY}/optima-api@{API_DIGEST}", False),
        },
        {
            "resourceId": UI_ID,
            "changeType": "Create",
            "after": _app_payload(f"{REGISTRY}/optima-ui@{UI_DIGEST}", False),
        },
        {
            "resourceId": UI_AUTH_ID,
            "changeType": "Create",
            "after": {"properties": {"platform": {"enabled": True}}},
        },
        {
            "resourceId": JOB_ID,
            "changeType": "Create",
            "after": {"properties": {"template": {"containers": []}}},
        },
    ]


def _public_apps() -> list[dict[str, Any]]:
    api_payload = _app_payload(f"{REGISTRY}/optima-api@{API_DIGEST}", False)
    ui_internal = _app_payload(f"{REGISTRY}/optima-ui@{UI_DIGEST}", False)
    ui_external = _app_payload(f"{REGISTRY}/optima-ui@{UI_DIGEST}", True)
    auth = {"properties": {"platform": {"enabled": True}}}
    job: dict[str, Any] = {"properties": {"template": {"containers": []}}}
    return [
        _nochange(API_ID, api_payload),
        {
            "resourceId": UI_ID,
            "changeType": "Modify",
            "before": ui_internal,
            "after": ui_external,
            "delta": [
                {
                    "path": "properties.configuration.ingress.external",
                    "propertyChangeType": "Modify",
                    "before": False,
                    "after": True,
                    "children": None,
                }
            ],
        },
        _nochange(UI_AUTH_ID, auth),
        _nochange(JOB_ID, job),
    ]


def _document(changes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "Succeeded", "changes": changes}


def _classify(stage: str, changes: list[dict[str, Any]]) -> Any:
    return classify_rollout_whatif(
        _document(changes),
        rollout_stage=stage,
        subscription_id=SUBSCRIPTION,
        resource_group=GROUP,
        environment_name="hackathon",
        api_image_digest=API_DIGEST,
        ui_image_digest=UI_DIGEST,
    )


def _assert_code(
    stage: str, changes: list[dict[str, Any]], code: WhatIfClassificationCode
) -> None:
    with pytest.raises(WhatIfClassificationError) as error:
        _classify(stage, changes)
    assert error.value.code == code


def test_internal_rollout_is_approved() -> None:
    """The exact internal graph with internal ingress and bound images passes."""
    result = _classify("internal", _foundation_nochanges() + _internal_apps())
    assert result.rollout_stage == "internal"
    assert result.foundation_change_counts == {"Create": 0, "NoChange": 10}
    ui = next(a for a in result.application_changes if a.role == "ui_container_app")
    assert ui.ingress_external is False


def test_public_ui_exposure_is_approved() -> None:
    """A single UI ingress Modify from internal to external passes."""
    result = _classify("public-ui", _foundation_nochanges() + _public_apps())
    ui = next(a for a in result.application_changes if a.role == "ui_container_app")
    assert ui.change_type == "Modify"
    assert ui.ingress_external is True


def test_delete_in_internal_what_if_fails_closed() -> None:
    """A Delete anywhere in the internal rollout fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[10] = {"resourceId": API_ID, "changeType": "Delete"}
    _assert_code("internal", changes, WhatIfClassificationCode.DELETE_REJECTED)


def test_delete_in_public_what_if_fails_closed() -> None:
    """A Delete anywhere in the public exposure fails closed."""
    changes = _foundation_nochanges() + _public_apps()
    changes[11] = {"resourceId": UI_ID, "changeType": "Delete"}
    _assert_code("public-ui", changes, WhatIfClassificationCode.DELETE_REJECTED)


def test_replacement_fails_closed() -> None:
    """A Create with a non-empty before is a replacement and fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[10]["before"] = {"properties": {"old": True}}
    _assert_code("internal", changes, WhatIfClassificationCode.REPLACEMENT_REJECTED)


def test_unexpected_extra_application_fails_closed() -> None:
    """An application resource outside the exact graph fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    extra_id = f"{SCOPE}/Microsoft.App/containerApps/ca-optima-extra-hackathon"
    changes.append(
        {
            "resourceId": extra_id,
            "changeType": "Create",
            "after": _app_payload(f"{REGISTRY}/optima-api@{API_DIGEST}", False),
        }
    )
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_APPLICATION_GRAPH_MISMATCH
    )


def test_missing_expected_application_fails_closed() -> None:
    """A rollout missing one required application resource fails closed."""
    changes = _foundation_nochanges() + _internal_apps()[:3]
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_APPLICATION_GRAPH_MISMATCH
    )


def test_duplicate_application_fails_closed() -> None:
    """A duplicated application resource id fails closed."""
    apps = _internal_apps()
    apps.append(dict(apps[0]))
    _assert_code(
        "internal",
        _foundation_nochanges() + apps,
        WhatIfClassificationCode.DUPLICATE_RESOURCE,
    )


def test_internal_ui_exposed_fails_closed() -> None:
    """Internal rollout with an externally exposed UI fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[11]["after"] = _app_payload(f"{REGISTRY}/optima-ui@{UI_DIGEST}", True)
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_internal_api_exposed_fails_closed() -> None:
    """The API must never be externally exposed, in either stage."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[10]["after"] = _app_payload(f"{REGISTRY}/optima-api@{API_DIGEST}", True)
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_public_ui_without_transition_fails_closed() -> None:
    """A public exposure that is not an internal-to-external Modify fails closed."""
    changes = _foundation_nochanges() + _public_apps()
    exposed = _app_payload(f"{REGISTRY}/optima-ui@{UI_DIGEST}", True)
    changes[11] = _nochange(UI_ID, exposed)
    _assert_code(
        "public-ui", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_public_ui_already_external_before_fails_closed() -> None:
    """A UI Modify whose before is already external is not the reviewed transition."""
    changes = _foundation_nochanges() + _public_apps()
    exposed = _app_payload(f"{REGISTRY}/optima-ui@{UI_DIGEST}", True)
    changes[11]["before"] = exposed
    _assert_code(
        "public-ui", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_public_api_change_fails_closed() -> None:
    """Public exposure must not change the API application at all."""
    changes = _foundation_nochanges() + _public_apps()
    changes[10] = {
        "resourceId": API_ID,
        "changeType": "Modify",
        "before": {"properties": {}},
        "after": _app_payload(f"{REGISTRY}/optima-api@{API_DIGEST}", False),
        "delta": [
            {
                "path": "properties.template.containers",
                "propertyChangeType": "Modify",
                "before": None,
                "after": 1,
                "children": None,
            }
        ],
    }
    _assert_code(
        "public-ui",
        changes,
        WhatIfClassificationCode.ROLLOUT_UNEXPECTED_APPLICATION_CHANGE,
    )


def test_public_smoke_job_change_fails_closed() -> None:
    """Public exposure must not change the smoke job."""
    changes = _foundation_nochanges() + _public_apps()
    changes[13] = {
        "resourceId": JOB_ID,
        "changeType": "Modify",
        "before": {"properties": {}},
        "after": {"properties": {"template": {"containers": []}}},
        "delta": [
            {
                "path": "properties.template",
                "propertyChangeType": "Modify",
                "before": None,
                "after": 1,
                "children": None,
            }
        ],
    }
    _assert_code(
        "public-ui",
        changes,
        WhatIfClassificationCode.ROLLOUT_UNEXPECTED_APPLICATION_CHANGE,
    )


def test_wrong_api_image_digest_fails_closed() -> None:
    """The API container must reference the exact pushed manifest digest."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[10]["after"] = _app_payload(
        f"{REGISTRY}/optima-api@sha256:{'d' * 64}", False
    )
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


def test_wrong_ui_image_digest_fails_closed() -> None:
    """The UI container must reference the exact pushed manifest digest."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[11]["after"] = _app_payload(
        f"{REGISTRY}/optima-ui@sha256:{'d' * 64}", False
    )
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


def test_swapped_repository_image_fails_closed() -> None:
    """Binding the UI digest to the API repository fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[10]["after"] = _app_payload(f"{REGISTRY}/optima-ui@{API_DIGEST}", False)
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


def test_foundation_create_in_rollout_fails_closed() -> None:
    """A rollout that would create a foundation resource fails closed."""
    changes = [
        {"resourceId": resource_id, "changeType": "Create"}
        for resource_id in FOUNDATION_IDS
    ] + _internal_apps()
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_FOUNDATION_NOT_CONVERGED
    )


def test_foundation_delete_in_rollout_fails_closed() -> None:
    """A rollout that would delete a foundation resource fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[0] = {"resourceId": FOUNDATION_IDS[0], "changeType": "Delete"}
    _assert_code("internal", changes, WhatIfClassificationCode.DELETE_REJECTED)


def test_unexpected_foundation_type_fails_closed() -> None:
    """A rollout that touches a role assignment fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    changes.append(
        {
            "resourceId": (
                f"{SCOPE}/Microsoft.Authorization/roleAssignments/"
                "00000000-0000-0000-0000-000000000123"
            ),
            "changeType": "Create",
        }
    )
    _assert_code("internal", changes, WhatIfClassificationCode.ROLE_ASSIGNMENT_CHANGE)


def test_unknown_stage_fails_closed() -> None:
    """An unrecognized rollout stage fails closed."""
    _assert_code(
        "canary",
        _foundation_nochanges() + _internal_apps(),
        WhatIfClassificationCode.ROLLOUT_STAGE_MISMATCH,
    )


@pytest.mark.parametrize("digest", ["sha256:" + ("0" * 64), "not-a-digest", ""])
def test_placeholder_or_malformed_digest_fails_closed(digest: str) -> None:
    """A placeholder or malformed image digest fails closed."""
    with pytest.raises(WhatIfClassificationError) as error:
        classify_rollout_whatif(
            _document(_foundation_nochanges() + _internal_apps()),
            rollout_stage="internal",
            subscription_id=SUBSCRIPTION,
            resource_group=GROUP,
            api_image_digest=digest,
            ui_image_digest=UI_DIGEST,
        )
    assert error.value.code == WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING


def _policy_document() -> dict[str, str]:
    external_id = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
        "/providers/Microsoft.CognitiveServices/accounts/aoai-optima"
    )
    return {
        "schema_version": classifier.EXTERNAL_POLICY_SCHEMA_VERSION,
        "deployment_mode": "Incremental",
        "scope_fingerprint": classifier._scope_fingerprint(SUBSCRIPTION, GROUP),
        "resource_type": "microsoft.cognitiveservices/accounts",
        "resource_id_fingerprint_version": classifier.RESOURCE_ID_FINGERPRINT_VERSION,
        "resource_id_fingerprint": classifier.resource_identity_fingerprint(
            external_id, subscription_id=SUBSCRIPTION, resource_group=GROUP
        ),
    }


def _aoai_ignore() -> dict[str, Any]:
    body = {
        "id": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
            "/providers/Microsoft.CognitiveServices/accounts/aoai-optima"
        ),
        "name": "aoai-optima",
        "type": "Microsoft.CognitiveServices/accounts",
        "location": "eastus2",
        "kind": "OpenAI",
        "resourceGroup": GROUP,
        "sku": {"name": "S0"},
        "tags": {},
    }
    return {
        "resourceId": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
            "/providers/Microsoft.CognitiveServices/accounts/aoai-optima"
        ),
        "changeType": "Ignore",
        "before": body,
        "after": body,
    }


def test_internal_rollout_binds_the_reviewed_external_observation() -> None:
    """The single reviewed AOAI Ignore flows through the rollout classifier."""
    policy = classifier.parse_external_observation_policy(_policy_document())
    changes = _foundation_nochanges() + [_aoai_ignore()] + _internal_apps()
    result = classify_rollout_whatif(
        _document(changes),
        rollout_stage="internal",
        subscription_id=SUBSCRIPTION,
        resource_group=GROUP,
        external_policy=policy,
        api_image_digest=API_DIGEST,
        ui_image_digest=UI_DIGEST,
    )
    assert len(result.external_observations) == 1


def test_rollout_rejects_a_second_external_observation() -> None:
    """More than the one configured external observation fails closed."""
    policy = classifier.parse_external_observation_policy(_policy_document())
    second = dict(_aoai_ignore())
    second["resourceId"] = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
        "/providers/Microsoft.CognitiveServices/accounts/aoai-second"
    )
    changes = _foundation_nochanges() + [_aoai_ignore(), second] + _internal_apps()
    with pytest.raises(WhatIfClassificationError) as error:
        classify_rollout_whatif(
            _document(changes),
            rollout_stage="internal",
            subscription_id=SUBSCRIPTION,
            resource_group=GROUP,
            external_policy=policy,
            api_image_digest=API_DIGEST,
            ui_image_digest=UI_DIGEST,
        )
    assert error.value.code == WhatIfClassificationCode.EXTERNAL_OBSERVATION_MISMATCH


def _rollout_parameters(*, expose_public_ui: bool) -> dict[str, Any]:
    return {
        "$schema": classifier.DEPLOYMENT_PARAMETERS_SCHEMA,
        "contentVersion": "1.0.0.0",
        "parameters": {
            "deployContainerApps": {"value": True},
            "deployRuntimeAccess": {"value": False},
            "exposePublicUi": {"value": expose_public_ui},
            "semanticCacheEnabled": {"value": False},
            "apiImageDigest": {"value": API_DIGEST},
            "uiImageDigest": {"value": UI_DIGEST},
            "environmentName": {"value": "hackathon"},
        },
    }


def _write(path: Path, document: dict[str, Any]) -> tuple[Path, str]:
    import hashlib

    content = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _run_cli(
    tmp_path: Path,
    *,
    stage: str,
    changes: list[dict[str, Any]],
    parameters: dict[str, Any],
) -> int:
    whatif_path = tmp_path / "whatif.json"
    whatif_path.write_text(json.dumps(_document(changes)), encoding="utf-8")
    params_path, params_sha = _write(tmp_path / "params.json", parameters)
    return main(
        [
            "classify-rollout",
            "--stage",
            stage,
            "--whatif",
            str(whatif_path),
            "--subscription-id",
            SUBSCRIPTION,
            "--resource-group",
            GROUP,
            "--commit-sha",
            COMMIT_SHA,
            "--parameters-file",
            str(params_path),
            "--parameters-sha256",
            params_sha,
            "--template-file",
            "infra/resource-group.bicep",
            "--parameter-source-file",
            "infra/environments/hackathon.runtime.bicepparam",
            "--api-image-digest",
            API_DIGEST,
            "--ui-image-digest",
            UI_DIGEST,
            "--output",
            str(tmp_path / "evidence.json"),
        ]
    )


def test_cli_internal_rollout_binds_and_approves(tmp_path: Path) -> None:
    """The CLI binds the immutable artifact and writes approved evidence."""
    exit_code = _run_cli(
        tmp_path,
        stage="internal",
        changes=_foundation_nochanges() + _internal_apps(),
        parameters=_rollout_parameters(expose_public_ui=False),
    )
    assert exit_code == 0
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["classification"] == "APPROVED"
    assert evidence["rollout_stage"] == "internal"
    assert evidence["images"] == {"api": API_DIGEST, "ui": UI_DIGEST}


def test_cli_rejects_parameter_stage_mismatch(tmp_path: Path) -> None:
    """A public-exposure parameter artifact cannot authorize an internal what-if."""
    exit_code = _run_cli(
        tmp_path,
        stage="internal",
        changes=_foundation_nochanges() + _internal_apps(),
        parameters=_rollout_parameters(expose_public_ui=True),
    )
    assert exit_code == 1
    assert not (tmp_path / "evidence.json").exists()


def test_cli_rejects_parameter_digest_mismatch(tmp_path: Path) -> None:
    """A parameter artifact that binds a different digest fails closed."""
    parameters = _rollout_parameters(expose_public_ui=False)
    parameters["parameters"]["apiImageDigest"]["value"] = "sha256:" + ("e" * 64)
    exit_code = _run_cli(
        tmp_path,
        stage="internal",
        changes=_foundation_nochanges() + _internal_apps(),
        parameters=parameters,
    )
    assert exit_code == 1
    assert not (tmp_path / "evidence.json").exists()


def test_cli_rejects_tampered_parameter_sha256(tmp_path: Path) -> None:
    """The classifier binds the exact parameter artifact digest the create uses."""
    whatif_path = tmp_path / "whatif.json"
    whatif_path.write_text(
        json.dumps(_document(_foundation_nochanges() + _internal_apps())),
        encoding="utf-8",
    )
    params_path, _ = _write(
        tmp_path / "params.json", _rollout_parameters(expose_public_ui=False)
    )
    exit_code = main(
        [
            "classify-rollout",
            "--stage",
            "internal",
            "--whatif",
            str(whatif_path),
            "--subscription-id",
            SUBSCRIPTION,
            "--resource-group",
            GROUP,
            "--commit-sha",
            COMMIT_SHA,
            "--parameters-file",
            str(params_path),
            "--parameters-sha256",
            "f" * 64,
            "--template-file",
            "infra/resource-group.bicep",
            "--parameter-source-file",
            "infra/environments/hackathon.runtime.bicepparam",
            "--api-image-digest",
            API_DIGEST,
            "--ui-image-digest",
            UI_DIGEST,
            "--output",
            str(tmp_path / "evidence.json"),
        ]
    )
    assert exit_code == 1
    assert not (tmp_path / "evidence.json").exists()
