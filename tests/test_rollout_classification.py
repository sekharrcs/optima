"""Fail-closed classification tests for the Container Apps rollout what-if.

Every adversarial fixture here fails closed against the complete closed
desired-state projection of each rollout resource. The reviewed baseline payloads
(``_api_after``/``_ui_after``/``_auth_after``/``_smoke_after``) are the only fully
valid projections; each regression tampers with exactly one projected field and
proves the classifier rejects it. These projection regressions did not exist at
PR head e6ed1c0, where the rollout classifier materially checked only
``containers[0].image`` and ``ingress.external`` and therefore accepted sidecars,
mutated target ports, disabled authentication, mutable smoke-job images, wrong
payload types, and a destructive delta that retained the transition fingerprint.
"""

from __future__ import annotations

import hashlib
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
CLIENT_ID = "22222222-3333-4444-5555-666666666666"
TENANT_ID = "d04cc813-b8d5-4eba-aca4-391c3278fd1a"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
API_IDENTITY = f"{SCOPE}/Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-api"
UI_IDENTITY = f"{SCOPE}/Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-ui"
# The deployment-source fingerprint binds the reviewed template and every module,
# including container-apps.bicep. It is computed once from the real repository
# source that the projection constants mirror, never from the what-if under test.
SOURCE_FINGERPRINT, SOURCE_FILE_COUNT = classifier.deployment_source_fingerprint(
    {
        "templateFile": "infra/resource-group.bicep",
        "parameterFile": "infra/environments/hackathon.runtime.bicepparam",
    }
)

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


def _api_after(
    *, external: bool = False, cache_mode: str = "false", digest: str = API_DIGEST
) -> dict[str, Any]:
    """The complete reviewed API container-app desired state projection input."""
    return {
        "identity": {
            "type": "UserAssigned",
            "userAssignedIdentities": {API_IDENTITY: {}},
        },
        "properties": {
            "configuration": {
                "activeRevisionsMode": "Single",
                "ingress": {
                    "external": external,
                    "targetPort": 8000,
                    "transport": "auto",
                    "allowInsecure": False,
                    "traffic": [{"latestRevision": True, "weight": 100}],
                },
                "secrets": [{"name": "application-insights-connection-string"}],
                "registries": [{"identity": API_IDENTITY, "server": REGISTRY}],
            },
            "template": {
                "containers": [
                    {
                        "name": "api",
                        "image": f"{REGISTRY}/optima-api@{digest}",
                        "env": [
                            {
                                "name": "OPTIMA_SEMANTIC_CACHE_ENABLED",
                                "value": cache_mode,
                            }
                        ],
                        "resources": {"cpu": 0.5, "memory": "1.0Gi"},
                    }
                ],
                "scale": {"minReplicas": 0, "maxReplicas": 3},
            },
        },
    }


def _ui_after(*, external: bool, digest: str = UI_DIGEST) -> dict[str, Any]:
    """The complete reviewed UI container-app desired state projection input."""
    return {
        "identity": {
            "type": "UserAssigned",
            "userAssignedIdentities": {UI_IDENTITY: {}},
        },
        "properties": {
            "configuration": {
                "activeRevisionsMode": "Single",
                "ingress": {
                    "external": external,
                    "targetPort": 8501,
                    "transport": "auto",
                    "allowInsecure": False,
                    "traffic": [{"latestRevision": True, "weight": 100}],
                },
                "secrets": [{"name": "ui-auth-client-secret"}],
                "registries": [{"identity": UI_IDENTITY, "server": REGISTRY}],
            },
            "template": {
                "containers": [
                    {
                        "name": "ui",
                        "image": f"{REGISTRY}/optima-ui@{digest}",
                        "resources": {"cpu": 0.5, "memory": "1.0Gi"},
                    }
                ],
                "scale": {"minReplicas": 0, "maxReplicas": 2},
            },
        },
    }


def _auth_after() -> dict[str, Any]:
    """The complete reviewed UI authentication config projection input."""
    return {
        "properties": {
            "platform": {"enabled": True},
            "globalValidation": {
                "unauthenticatedClientAction": "RedirectToLoginPage",
                "redirectToProvider": "azureActiveDirectory",
            },
            "identityProviders": {
                "azureActiveDirectory": {
                    "enabled": True,
                    "registration": {
                        "clientId": CLIENT_ID,
                        "clientSecretSettingName": "ui-auth-client-secret",
                        "openIdIssuer": ISSUER,
                    },
                    "validation": {
                        "allowedAudiences": [CLIENT_ID, f"api://{CLIENT_ID}"]
                    },
                }
            },
            "login": {"tokenStore": {"enabled": False}},
            "httpSettings": {"requireHttps": True},
        }
    }


def _smoke_after(*, digest: str = UI_DIGEST) -> dict[str, Any]:
    """The complete reviewed pre-exposure smoke-job projection input."""
    return {
        "identity": {
            "type": "UserAssigned",
            "userAssignedIdentities": {UI_IDENTITY: {}},
        },
        "properties": {
            "configuration": {
                "triggerType": "Manual",
                "replicaRetryLimit": 0,
                "registries": [{"identity": UI_IDENTITY, "server": REGISTRY}],
            },
            "template": {
                "containers": [
                    {"name": "smoke", "image": f"{REGISTRY}/optima-ui@{digest}"}
                ]
            },
        },
    }


def _foundation_nochanges() -> list[dict[str, Any]]:
    return [_nochange(resource_id) for resource_id in FOUNDATION_IDS]


def _create(resource_id: str, after: dict[str, Any]) -> dict[str, Any]:
    return {"resourceId": resource_id, "changeType": "Create", "after": after}


def _internal_apps() -> list[dict[str, Any]]:
    return [
        _create(API_ID, _api_after(external=False)),
        _create(UI_ID, _ui_after(external=False)),
        _create(UI_AUTH_ID, _auth_after()),
        _create(JOB_ID, _smoke_after()),
    ]


def _ui_exposure_delta() -> list[dict[str, Any]]:
    return [
        {
            "path": "properties.configuration.ingress.external",
            "propertyChangeType": "Modify",
            "before": False,
            "after": True,
            "children": None,
        }
    ]


def _public_apps() -> list[dict[str, Any]]:
    return [
        _nochange(API_ID, _api_after(external=False)),
        {
            "resourceId": UI_ID,
            "changeType": "Modify",
            "before": _ui_after(external=False),
            "after": _ui_after(external=True),
            "delta": _ui_exposure_delta(),
        },
        _nochange(UI_AUTH_ID, _auth_after()),
        _nochange(JOB_ID, _smoke_after()),
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
        ui_auth_client_id=CLIENT_ID,
        ui_auth_tenant_id=TENANT_ID,
        deployment_source_fingerprint=SOURCE_FINGERPRINT,
        deployment_source_file_count=SOURCE_FILE_COUNT,
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
            "after": _api_after(external=False),
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
    changes[11]["after"] = _ui_after(external=True)
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_internal_api_exposed_fails_closed() -> None:
    """The API must never be externally exposed, in either stage."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[10]["after"] = _api_after(external=True)
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_public_ui_without_transition_fails_closed() -> None:
    """A public exposure that is not an internal-to-external Modify fails closed."""
    changes = _foundation_nochanges() + _public_apps()
    exposed = _ui_after(external=True)
    changes[11] = _nochange(UI_ID, exposed)
    _assert_code(
        "public-ui", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_public_ui_already_external_before_fails_closed() -> None:
    """A UI Modify whose before is already external is not the reviewed transition."""
    changes = _foundation_nochanges() + _public_apps()
    exposed = _ui_after(external=True)
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
        "after": _api_after(external=False),
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
    changes[10]["after"] = _api_after(external=False, digest="sha256:" + "d" * 64)
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


def test_wrong_ui_image_digest_fails_closed() -> None:
    """The UI container must reference the exact pushed manifest digest."""
    changes = _foundation_nochanges() + _internal_apps()
    changes[11]["after"] = _ui_after(external=False, digest="sha256:" + "d" * 64)
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


def test_swapped_repository_image_fails_closed() -> None:
    """Binding the UI repository to the API resource fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    swapped = _api_after(external=False)
    swapped["properties"]["template"]["containers"][0]["image"] = (
        f"{REGISTRY}/optima-ui@{API_DIGEST}"
    )
    changes[10]["after"] = swapped
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


# --- F1 (BLOCKING) regressions: each reproduces one Astra bypass that the
# partial-field classifier at e6ed1c0 accepted while retaining the legitimate
# image/transition fingerprint.


def test_api_sidecar_container_fails_closed() -> None:
    """An injected sidecar next to the reviewed API container fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _api_after(external=False)
    payload["properties"]["template"]["containers"].append(
        {"name": "sidecar", "image": f"{REGISTRY}/optima-api@{API_DIGEST}"}
    )
    changes[10]["after"] = payload
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


def test_ui_sidecar_container_fails_closed() -> None:
    """An injected sidecar next to the reviewed UI container fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _ui_after(external=False)
    payload["properties"]["template"]["containers"].append(
        {"name": "sidecar", "image": f"{REGISTRY}/optima-ui@{UI_DIGEST}"}
    )
    changes[11]["after"] = payload
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


def test_api_target_port_change_fails_closed() -> None:
    """A mutated API ingress target port fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _api_after(external=False)
    payload["properties"]["configuration"]["ingress"]["targetPort"] = 9000
    changes[10]["after"] = payload
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_api_transport_change_fails_closed() -> None:
    """A mutated API ingress transport fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _api_after(external=False)
    payload["properties"]["configuration"]["ingress"]["transport"] = "tcp"
    changes[10]["after"] = payload
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_api_allow_insecure_change_fails_closed() -> None:
    """An API ingress that allows insecure traffic fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _api_after(external=False)
    payload["properties"]["configuration"]["ingress"]["allowInsecure"] = True
    changes[10]["after"] = payload
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_disabled_authentication_platform_fails_closed() -> None:
    """A rollout that disables the UI authentication platform fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    auth = _auth_after()
    auth["properties"]["platform"]["enabled"] = False
    changes[12]["after"] = auth
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_AUTHENTICATION)


def test_anonymous_unauthenticated_action_fails_closed() -> None:
    """A rollout that allows anonymous UI clients fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    auth = _auth_after()
    auth["properties"]["globalValidation"]["unauthenticatedClientAction"] = (
        "AllowAnonymous"
    )
    changes[12]["after"] = auth
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_AUTHENTICATION)


def test_enabled_token_store_fails_closed() -> None:
    """A rollout that enables the UI token store fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    auth = _auth_after()
    auth["properties"]["login"]["tokenStore"]["enabled"] = True
    changes[12]["after"] = auth
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_AUTHENTICATION)


def test_wrong_authentication_client_fails_closed() -> None:
    """A rollout that binds a different Entra application fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    auth = _auth_after()
    other = "99999999-8888-7777-6666-555555555555"
    auth["properties"]["identityProviders"]["azureActiveDirectory"]["registration"][
        "clientId"
    ] = other
    changes[12]["after"] = auth
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_AUTHENTICATION)


def test_smoke_job_mutable_image_fails_closed() -> None:
    """A smoke job bound to any image other than the pushed UI digest fails."""
    changes = _foundation_nochanges() + _internal_apps()
    job = _smoke_after(digest="sha256:" + "d" * 64)
    changes[13]["after"] = job
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


def test_smoke_job_api_image_fails_closed() -> None:
    """A smoke job that runs the API image instead of the reviewed UI image fails."""
    changes = _foundation_nochanges() + _internal_apps()
    job = _smoke_after()
    job["properties"]["template"]["containers"][0]["image"] = (
        f"{REGISTRY}/optima-api@{API_DIGEST}"
    )
    changes[13]["after"] = job
    _assert_code("internal", changes, WhatIfClassificationCode.ROLLOUT_IMAGE_BINDING)


def test_smoke_job_retry_change_fails_closed() -> None:
    """A smoke job that allows replica retries fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    job = _smoke_after()
    job["properties"]["configuration"]["replicaRetryLimit"] = 3
    changes[13]["after"] = job
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_APPLICATION_PROJECTION
    )


def test_missing_identity_fails_closed() -> None:
    """A rollout resource without the reviewed managed identity fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _api_after(external=False)
    del payload["identity"]
    changes[10]["after"] = payload
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_APPLICATION_PROJECTION
    )


def test_extra_secret_reference_fails_closed() -> None:
    """An undeclared secret reference fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _api_after(external=False)
    payload["properties"]["configuration"]["secrets"].append({"name": "injected"})
    changes[10]["after"] = payload
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_APPLICATION_PROJECTION
    )


def test_scale_bound_change_fails_closed() -> None:
    """A mutated replica ceiling fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _api_after(external=False)
    payload["properties"]["template"]["scale"]["maxReplicas"] = 50
    changes[10]["after"] = payload
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_APPLICATION_PROJECTION
    )


def test_disabled_cache_redis_env_fails_closed() -> None:
    """A disabled-cache API that still injects Redis environment fails closed."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _api_after(external=False)
    payload["properties"]["template"]["containers"][0]["env"].append(
        {"name": "OPTIMA_REDIS_HOST", "value": "redis.example.net"}
    )
    changes[10]["after"] = payload
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_APPLICATION_PROJECTION
    )


def test_wrong_payload_type_fails_closed() -> None:
    """A wrong-typed ingress payload fails closed instead of being ignored."""
    changes = _foundation_nochanges() + _internal_apps()
    payload = _api_after(external=False)
    payload["properties"]["configuration"]["ingress"] = ["external"]
    changes[10]["after"] = payload
    _assert_code(
        "internal", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_public_destructive_delta_with_retained_transition_fails_closed() -> None:
    """A public what-if that mutates more than the UI ingress fails closed.

    The after-state retains the legitimate external=false->true transition and the
    reviewed image digest, but also silently mutates the target port. The partial
    classifier at e6ed1c0 accepted this; the closed projection rejects it because
    the before/after projections differ in more than the ingress external flag.
    """
    changes = _foundation_nochanges() + _public_apps()
    tampered = _ui_after(external=True)
    tampered["properties"]["configuration"]["ingress"]["targetPort"] = 9000
    changes[11]["after"] = tampered
    _assert_code(
        "public-ui", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_public_extra_delta_leaf_fails_closed() -> None:
    """A public exposure delta that claims more than the ingress transition fails."""
    changes = _foundation_nochanges() + _public_apps()
    changes[11]["delta"] = _ui_exposure_delta() + [
        {
            "path": "properties.template.scale.maxReplicas",
            "propertyChangeType": "Modify",
            "before": 2,
            "after": 9,
            "children": None,
        }
    ]
    _assert_code(
        "public-ui", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_public_missing_exposure_delta_fails_closed() -> None:
    """A public UI Modify whose delta omits the ingress transition fails closed."""
    changes = _foundation_nochanges() + _public_apps()
    changes[11]["delta"] = []
    _assert_code(
        "public-ui", changes, WhatIfClassificationCode.ROLLOUT_INGRESS_TRANSITION
    )


def test_rollout_requires_authentication_identity() -> None:
    """A rollout without the reviewed UI authentication identity fails closed."""
    with pytest.raises(WhatIfClassificationError) as error:
        classify_rollout_whatif(
            _document(_foundation_nochanges() + _internal_apps()),
            rollout_stage="internal",
            subscription_id=SUBSCRIPTION,
            resource_group=GROUP,
            api_image_digest=API_DIGEST,
            ui_image_digest=UI_DIGEST,
            ui_auth_client_id="",
            ui_auth_tenant_id=TENANT_ID,
            deployment_source_fingerprint=SOURCE_FINGERPRINT,
            deployment_source_file_count=SOURCE_FILE_COUNT,
        )
    assert error.value.code == WhatIfClassificationCode.ROLLOUT_PARAMETER_MISMATCH


def test_rollout_requires_deployment_source_fingerprint() -> None:
    """A rollout without a bound deployment-source fingerprint fails closed."""
    with pytest.raises(WhatIfClassificationError) as error:
        classify_rollout_whatif(
            _document(_foundation_nochanges() + _internal_apps()),
            rollout_stage="internal",
            subscription_id=SUBSCRIPTION,
            resource_group=GROUP,
            api_image_digest=API_DIGEST,
            ui_image_digest=UI_DIGEST,
            ui_auth_client_id=CLIENT_ID,
            ui_auth_tenant_id=TENANT_ID,
            deployment_source_fingerprint="not-a-fingerprint",
            deployment_source_file_count=SOURCE_FILE_COUNT,
        )
    assert error.value.code == WhatIfClassificationCode.INVALID_DEPLOYMENT_SOURCE


def test_rollout_evidence_binds_projection_and_source() -> None:
    """Approved evidence commits to the projection and deployment-source bytes."""
    result = _classify("internal", _foundation_nochanges() + _internal_apps())
    assert result.deployment_source_fingerprint == SOURCE_FINGERPRINT
    assert result.deployment_source_file_count == SOURCE_FILE_COUNT
    for application in result.application_changes:
        assert len(application.projection_fingerprint) == 64
    evidence = classifier.build_rollout_evidence(
        result, commit_sha=COMMIT_SHA, parameter_fingerprint_value="0" * 64
    )
    assert evidence["deployment_source"]["fingerprint"] == SOURCE_FINGERPRINT
    assert evidence["deployment_source"]["file_count"] == SOURCE_FILE_COUNT
    fingerprints = {
        resource["role"]: resource["projection_fingerprint"]
        for resource in evidence["applications"]["resources"]
    }
    assert set(fingerprints) == {
        "api_container_app",
        "smoke_job",
        "ui_auth_config",
        "ui_container_app",
    }


def test_rollout_projection_tracks_bicep_source() -> None:
    """The reviewed projection constants must match the compiled Bicep source.

    This binds the classifier's expected desired state to the exact reviewed
    deployment source rather than deriving it from the observed what-if.
    """
    bicep = Path("infra/modules/container-apps.bicep").read_text(encoding="utf-8")
    assert "targetPort: 8000" in bicep
    assert "targetPort: 8501" in bicep
    assert "transport: 'auto'" in bicep
    assert "allowInsecure: false" in bicep
    assert "name: 'api'" in bicep
    assert "name: 'ui'" in bicep
    assert "name: 'smoke'" in bicep
    assert "clientSecretSettingName: 'ui-auth-client-secret'" in bicep
    assert "unauthenticatedClientAction: 'RedirectToLoginPage'" in bicep
    assert "triggerType: 'Manual'" in bicep
    assert "replicaRetryLimit: 0" in bicep
    assert classifier._ROLLOUT_API_TARGET_PORT == 8000
    assert classifier._ROLLOUT_UI_TARGET_PORT == 8501
    assert classifier._ROLLOUT_INGRESS_TRANSPORT == "auto"
    assert classifier._ROLLOUT_CLIENT_SECRET_SETTING_NAME == "ui-auth-client-secret"
    assert classifier._ROLLOUT_SMOKE_TRIGGER_TYPE == "Manual"
    assert classifier._ROLLOUT_SMOKE_REPLICA_RETRY_LIMIT == 0
    assert classifier._ROLLOUT_API_SCALE == (0, 3)
    assert classifier._ROLLOUT_UI_SCALE == (0, 2)


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
            ui_auth_client_id=CLIENT_ID,
            ui_auth_tenant_id=TENANT_ID,
            deployment_source_fingerprint=SOURCE_FINGERPRINT,
            deployment_source_file_count=SOURCE_FILE_COUNT,
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
        ui_auth_client_id=CLIENT_ID,
        ui_auth_tenant_id=TENANT_ID,
        deployment_source_fingerprint=SOURCE_FINGERPRINT,
        deployment_source_file_count=SOURCE_FILE_COUNT,
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
            ui_auth_client_id=CLIENT_ID,
            ui_auth_tenant_id=TENANT_ID,
            deployment_source_fingerprint=SOURCE_FINGERPRINT,
            deployment_source_file_count=SOURCE_FILE_COUNT,
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
            "uiAuthClientId": {"value": CLIENT_ID},
            "uiAuthTenantId": {"value": TENANT_ID},
        },
    }


def _write(path: Path, document: dict[str, Any]) -> tuple[Path, str]:
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
