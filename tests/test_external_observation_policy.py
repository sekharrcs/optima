"""Synthetic, offline checks for an exact-bound external Ignore observation."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import whatif_classification as classifier

SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"
GROUP = "rg-optima-hackathon"
EXTERNAL_TYPE = "microsoft.cognitiveservices/accounts"
EXTERNAL_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
    "/providers/Microsoft.CognitiveServices/accounts/synthetic-external"
)


def policy_document() -> dict[str, str]:
    """Build a synthetic configured binding independent of what-if output."""
    return {
        "schema_version": classifier.EXTERNAL_POLICY_SCHEMA_VERSION,
        "deployment_mode": "Incremental",
        "scope_fingerprint": classifier._scope_fingerprint(SUBSCRIPTION, GROUP),
        "resource_type": EXTERNAL_TYPE,
        "resource_id_fingerprint_version": classifier.RESOURCE_ID_FINGERPRINT_VERSION,
        "resource_id_fingerprint": classifier.resource_identity_fingerprint(
            EXTERNAL_ID, subscription_id=SUBSCRIPTION, resource_group=GROUP
        ),
    }


def test_policy_is_immutable_and_closed() -> None:
    """Accept exactly the declared immutable policy contract."""
    policy = classifier.parse_external_observation_policy(policy_document())
    assert policy.to_document() == policy_document()
    with pytest.raises(FrozenInstanceError):
        cast(Any, policy).deployment_mode = "Complete"
    assert classifier.ALLOWED_CHANGE_TYPES == {"Create", "NoChange"}


@pytest.mark.parametrize(
    "document",
    [None, {}, [], False, {**policy_document(), "extra": "rejected"}],
)
def test_policy_rejects_incomplete_or_unknown_schema(document: Any) -> None:
    """A malformed or partially configured policy never becomes disabled."""
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.parse_external_observation_policy(document)


@pytest.mark.parametrize("field", sorted(policy_document()))
@pytest.mark.parametrize("value", [None, True, 1, [], {}, "", "unreviewed"])
def test_policy_rejects_invalid_field_types_and_values(field: str, value: Any) -> None:
    """Reject booleans, malformed fingerprints and unversioned identities."""
    document: dict[str, Any] = policy_document()
    document[field] = value
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.parse_external_observation_policy(document)


def test_explicit_identity_fingerprint_is_case_insensitive() -> None:
    """ARM identity casing does not change the configured fingerprint."""
    assert (
        classifier.resource_identity_fingerprint(
            EXTERNAL_ID.upper(),
            subscription_id=SUBSCRIPTION,
            resource_group=GROUP.upper(),
        )
        == policy_document()["resource_id_fingerprint"]
    )


def observation() -> dict[str, Any]:
    """Build only synthetic observation properties and identities."""
    return {
        "resourceId": EXTERNAL_ID,
        "changeType": "Ignore",
        "before": {
            "id": EXTERNAL_ID,
            "type": EXTERNAL_TYPE,
            "name": "synthetic-external",
            "properties": {"state": "PreserveCase", "capacity": 1},
        },
    }


def foundation_document() -> dict[str, Any]:
    """Use the exact existing graph constructor with a fixed synthetic suffix."""
    graph = classifier._expected_resource_graph(
        subscription_id=SUBSCRIPTION,
        resource_group=GROUP,
        environment_name="hackathon",
        unique_suffix="abc123def456g",
    )
    return {
        "status": "Succeeded",
        "changes": [
            *({"resourceId": identity, "changeType": "Create"} for identity in graph),
            observation(),
        ],
    }


def classify(document: Any = None) -> classifier.FoundationWhatIfClassification:
    """Explicitly inject the synthetic policy into the pure core API."""
    return classifier.classify_foundation_whatif(
        foundation_document() if document is None else document,
        subscription_id=SUBSCRIPTION,
        resource_group=GROUP,
        external_policy=classifier.parse_external_observation_policy(policy_document()),
    )


def test_exact_observation_keeps_nine_managed_changes() -> None:
    """Exactly one external Ignore does not become a managed fact or count."""
    result = classify()
    assert len(result.allowed_changes) == 9
    assert result.change_counts == {"Create": 9, "NoChange": 0}
    assert len(result.external_observations) == 1
    assert result.external_observations[0].resource_type == EXTERNAL_TYPE
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.classify_foundation_whatif(
            foundation_document(), subscription_id=SUBSCRIPTION, resource_group=GROUP
        )


@pytest.mark.parametrize("operation", ["missing", "additional", "duplicate", "graph"])
def test_observation_and_managed_cardinality(operation: str) -> None:
    """Reject missing, additional or duplicated observations and missing roles."""
    document = foundation_document()
    if operation == "missing":
        document["changes"].pop()
    elif operation == "graph":
        document["changes"].pop(0)
    else:
        extra = observation()
        if operation == "additional":
            extra["resourceId"] += "-other"
        document["changes"].append(extra)
    with pytest.raises(classifier.WhatIfClassificationError):
        classify(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resourceId", EXTERNAL_ID + "-other"),
        ("resourceId", EXTERNAL_ID.replace(GROUP, "wrong-group")),
        ("before", None),
        ("before", {}),
        ("after", {}),
        ("after", {"properties": {}}),
        ("delta", [{}]),
        ("delta", False),
        ("extension", {}),
        ("unsupportedReason", "unsupported"),
        ("unreviewed", None),
        ("identifiers", {"name": "wrong"}),
        ("identifiers", {"principalId": "unreviewed"}),
        ("deploymentId", EXTERNAL_ID),
    ],
)
def test_observation_rejects_unsafe_payload(field: str, value: Any) -> None:
    """The observation path validates rather than strips unsafe fields."""
    document = foundation_document()
    document["changes"][-1][field] = value
    with pytest.raises(classifier.WhatIfClassificationError):
        classify(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", EXTERNAL_ID + "-other"),
        ("id", None),
        ("type", "microsoft.storage/storageaccounts"),
        ("name", "wrong"),
        ("resourceId", EXTERNAL_ID + "-other"),
        ("identity", {"type": "SystemAssigned"}),
        ("unknown", None),
        ("properties", False),
    ],
)
def test_before_rejects_contradictory_or_unreviewed_fields(
    field: str, value: Any
) -> None:
    """Before must describe the configured resource without unreviewed identity."""
    document = foundation_document()
    document["changes"][-1]["before"][field] = value
    with pytest.raises(classifier.WhatIfClassificationError):
        classify(document)


def test_observation_complete_payload_and_identity_canonicalization() -> None:
    """Canonicalize ARM identity casing, not arbitrary property values."""
    document = foundation_document()
    observed = document["changes"][-1]
    observed["identifiers"] = {"id": EXTERNAL_ID, "type": EXTERNAL_TYPE}
    original = classify(document)
    upper = copy.deepcopy(document)
    upper["changes"].reverse()
    for change in upper["changes"]:
        change["resourceId"] = change["resourceId"].upper()
        if change["changeType"] == "Ignore":
            for field in ("id", "type", "name"):
                change["before"][field] = change["before"][field].upper()
            change["identifiers"] = {
                key: value.upper() for key, value in change["identifiers"].items()
            }
    assert classify(upper) == original
    observed["before"]["properties"]["state"] = "preservecase"
    drifted = classify(document)
    assert drifted.change_fingerprint != original.change_fingerprint
    assert drifted.external_observations != original.external_observations


def evidence(document: Any = None) -> dict[str, Any]:
    """Build real v2 evidence from synthetic input without reading artifacts."""
    return classifier.build_foundation_evidence(
        classify(document),
        commit_sha="a" * 40,
        parameter_fingerprint_value="b" * 64,
        deployment_source_fingerprint_value="c" * 64,
        deployment_source_file_count=3,
    )


def converged_document() -> dict[str, Any]:
    """Converge only the managed graph, preserving the external payload."""
    document = foundation_document()
    for change in document["changes"][:-1]:
        change["changeType"] = "NoChange"
    return document


def test_evidence_is_closed_sanitized_and_v2() -> None:
    """Only nine managed facts and hashed external evidence are published."""
    result = evidence()
    assert result["schema_version"] == "optima-foundation-whatif-evidence-v2"
    assert classifier.CHANGE_FINGERPRINT_VERSION.endswith("-v2")
    assert result["deployment_mode"] == "Incremental"
    assert result["external_policy"]["definition"] == policy_document()
    assert len(result["changes"]["resources"]) == 9
    assert result["changes"]["counts"] == {"Create": 9, "NoChange": 0}
    assert len(result["external_observations"]) == 1
    serialized = json.dumps(result)
    for sensitive in (SUBSCRIPTION, EXTERNAL_ID, "synthetic-external", "PreserveCase"):
        assert sensitive not in serialized
    classifier.compare_promotion_evidence(result, copy.deepcopy(result))


@pytest.mark.parametrize(
    "mutation",
    [
        "old_schema",
        "unknown",
        "mode",
        "policy_digest",
        "policy_definition",
        "observation_missing",
        "observation_extra",
        "observation_unknown",
        "observation_policy",
        "observation_id",
        "observation_scope",
        "observation_type",
        "payload_bool",
        "count_ignore",
        "count_bool",
        "policy_disabled",
        "managed_ignore",
        "policy_scope",
        "policy_unknown",
    ],
)
def test_evidence_validator_rejects_forgery_even_against_itself(mutation: str) -> None:
    """Strict schema and binding validation runs before any equality check."""
    result = evidence()
    if mutation == "old_schema":
        result["schema_version"] = "optima-foundation-whatif-evidence-v1"
    elif mutation == "unknown":
        result["unknown"] = None
    elif mutation == "mode":
        result["deployment_mode"] = "Complete"
    elif mutation == "policy_digest":
        result["external_policy"]["fingerprint"] = "d" * 64
    elif mutation == "policy_definition":
        result["external_policy"]["definition"]["resource_id_fingerprint"] = "d" * 64
    elif mutation == "policy_unknown":
        result["external_policy"]["definition"]["unknown"] = None
    elif mutation == "policy_scope":
        result["target"]["scope_fingerprint"] = "d" * 64
    elif mutation == "policy_disabled":
        result["external_policy"] = {
            "definition": None,
            "fingerprint": classifier.external_policy_fingerprint(None),
        }
    elif mutation == "observation_missing":
        result["external_observations"] = []
    elif mutation == "observation_extra":
        result["external_observations"] *= 2
    elif mutation == "observation_unknown":
        result["external_observations"][0]["raw_id"] = EXTERNAL_ID
    elif mutation.startswith("observation_"):
        field = {
            "policy": "policy_fingerprint",
            "id": "resource_id_fingerprint",
            "scope": "scope_fingerprint",
            "type": "resource_type",
        }[mutation.removeprefix("observation_")]
        result["external_observations"][0][field] = "d" * 64
    elif mutation == "payload_bool":
        result["external_observations"][0]["payload_fingerprint"] = True
    elif mutation == "count_ignore":
        result["changes"]["counts"]["Ignore"] = 1
    elif mutation == "count_bool":
        result["changes"]["counts"]["NoChange"] = False
    elif mutation == "managed_ignore":
        result["changes"]["resources"][0]["change_type"] = "Ignore"
    with pytest.raises(classifier.WhatIfClassificationError) as caught:
        classifier.compare_promotion_evidence(result, result)
    assert caught.value.code is classifier.WhatIfClassificationCode.PROMOTION_MISMATCH


def test_convergence_permits_only_managed_convergence() -> None:
    """Full promotion remains strict; convergence permits Create to NoChange."""
    plan = evidence()
    converged = evidence(converged_document())
    classifier.compare_convergence_evidence(plan, converged)
    classifier.compare_convergence_evidence(converged, converged)
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.compare_promotion_evidence(plan, converged)
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.compare_convergence_evidence(converged, plan)


@pytest.mark.parametrize(
    "section", ["commit_sha", "target", "deployment_source", "parameters", "external"]
)
def test_convergence_rejects_binding_and_external_payload_drift(section: str) -> None:
    """A converged managed graph cannot authorize changed external properties."""
    document = converged_document()
    if section == "external":
        document["changes"][-1]["before"]["properties"]["state"] = "drift"
    converged = evidence(document)
    if section == "commit_sha":
        converged[section] = "e" * 40
    elif section == "target":
        converged[section]["resource_group"] = "another-group"
    elif section in {"deployment_source", "parameters"}:
        converged[section]["fingerprint"] = "e" * 64
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.compare_convergence_evidence(evidence(), converged)
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.compare_promotion_evidence(evidence(), converged)


@pytest.fixture
def cli_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Create synthetic CLI inputs and prevent local toolchain probing."""
    monkeypatch.setattr(classifier, "_toolchain_versions", lambda: {})
    monkeypatch.setattr(os, "environ", {})
    source = tmp_path / "infra"
    source.mkdir()
    (source / "main.bicep").write_text("param location string\n", encoding="utf-8")
    (source / "main.bicepparam").write_text("using 'main.bicep'\n", encoding="utf-8")
    parameters = {
        "location": "eastus2",
        "environmentName": "hackathon",
        "resourceGroup": GROUP,
        "templateFile": "infra/main.bicep",
        "parameterFile": "infra/main.bicepparam",
        "deployContainerApps": "false",
        "exposePublicUi": "false",
        "deployRuntimeAccess": "false",
        "semanticCacheEnabled": "false",
    }
    (tmp_path / "parameters.txt").write_text(
        "\n".join(f"{key}={value}" for key, value in parameters.items()),
        encoding="utf-8",
    )
    (tmp_path / "whatif.json").write_text(
        json.dumps(foundation_document()), encoding="utf-8"
    )
    return [
        "classify",
        "--whatif",
        str(tmp_path / "whatif.json"),
        "--subscription-id",
        SUBSCRIPTION,
        "--resource-group",
        GROUP,
        "--commit-sha",
        "a" * 40,
        "--parameters-file",
        str(tmp_path / "parameters.txt"),
        "--source-root",
        str(tmp_path),
        "--output",
        str(tmp_path / "approved.json"),
        "--failure-diagnostics",
        str(tmp_path / "failed.json"),
    ]


def test_cli_explicit_opt_in_and_convergence(
    cli_fixture: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise classify and convergence-check with only synthetic inputs."""
    monkeypatch.setattr(
        os,
        "environ",
        {classifier.EXTERNAL_POLICY_ENV: json.dumps(policy_document())},
    )
    args = [
        *cli_fixture,
        "--deployment-mode",
        "Incremental",
        "--external-policy-env",
        classifier.EXTERNAL_POLICY_ENV,
    ]
    assert classifier.main(args) == 0
    plan = tmp_path / "approved.json"
    converged = tmp_path / "converged.json"
    plan_evidence = json.loads(plan.read_text(encoding="utf-8"))
    result = evidence(converged_document())
    result["parameters"] = plan_evidence["parameters"]
    result["deployment_source"] = plan_evidence["deployment_source"]
    converged.write_text(json.dumps(result), encoding="utf-8")
    assert (
        classifier.main(
            ["convergence-check", "--plan", str(plan), "--converged", str(converged)]
        )
        == 0
    )
    result["external_observations"][0]["payload_fingerprint"] = "f" * 64
    converged.write_text(json.dumps(result), encoding="utf-8")
    assert (
        classifier.main(
            ["convergence-check", "--plan", str(plan), "--converged", str(converged)]
        )
        == 1
    )


@pytest.mark.parametrize(
    "text",
    [
        " ",
        "null",
        "false",
        "0",
        "[]",
        "{}",
        "SYNTHETIC_SECRET",
        '{"x": NaN}',
        '{"x":1,"x":2}',
        json.dumps({**policy_document(), "secret": "SYNTHETIC_SECRET"}),
    ],
    ids=[
        "space",
        "null",
        "bool",
        "number",
        "array",
        "partial",
        "malformed",
        "nonfinite",
        "duplicate",
        "unknown",
    ],
)
def test_cli_invalid_policy_is_sanitized_and_nonpromotable(
    text: str,
    cli_fixture: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed policy never disables the gate or leaks environment contents."""
    monkeypatch.setattr(os, "environ", {classifier.EXTERNAL_POLICY_ENV: text})
    (tmp_path / "approved.json").write_text("stale", encoding="utf-8")
    assert (
        classifier.main(
            [*cli_fixture, "--external-policy-env", classifier.EXTERNAL_POLICY_ENV]
        )
        == 1
    )
    captured = capsys.readouterr()
    failed_text = (tmp_path / "failed.json").read_text(encoding="utf-8")
    assert "SYNTHETIC_SECRET" not in captured.out + captured.err + failed_text
    assert "WHATIF_INVALID_EXTERNAL_POLICY" in captured.err
    assert not (tmp_path / "approved.json").exists()
    failed = json.loads(failed_text)
    assert failed["classification"] == "FAILED"
    assert failed["promotable"] is False


@pytest.mark.parametrize("text", [None, ""])
def test_cli_missing_or_empty_policy_disables_only_explicitly(
    text: str | None,
    cli_fixture: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing/empty policy permits nine managed changes but never an Ignore."""
    monkeypatch.setattr(
        os,
        "environ",
        {} if text is None else {classifier.EXTERNAL_POLICY_ENV: text},
    )
    args = [*cli_fixture, "--external-policy-env", classifier.EXTERNAL_POLICY_ENV]
    assert classifier.main(args) == 1
    document = foundation_document()
    document["changes"].pop()
    (tmp_path / "whatif.json").write_text(json.dumps(document), encoding="utf-8")
    assert classifier.main(args) == 0
    result = json.loads((tmp_path / "approved.json").read_text(encoding="utf-8"))
    assert result["external_policy"] == {
        "definition": None,
        "fingerprint": classifier.external_policy_fingerprint(None),
    }
    assert result["external_observations"] == []
    assert result["deployment_mode"] == "Incremental"


def test_cli_and_core_do_not_implicitly_read_environment(
    cli_fixture: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy variable has no effect unless the caller explicitly selects it."""
    monkeypatch.setattr(
        os,
        "environ",
        {classifier.EXTERNAL_POLICY_ENV: json.dumps(policy_document())},
    )
    assert classifier.main(cli_fixture) == 1
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.classify_foundation_whatif(
            foundation_document(), subscription_id=SUBSCRIPTION, resource_group=GROUP
        )


@pytest.mark.parametrize("flag", ["--deployment-mode", "--external-policy-env"])
def test_cli_rejects_unsafe_mode_or_variable_without_echoing(
    flag: str,
    cli_fixture: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unsupported CLI values get fixed diagnostics rather than raw argparse output."""
    assert classifier.main([*cli_fixture, flag, "SYNTHETIC_SECRET"]) == 1
    captured = capsys.readouterr()
    assert "SYNTHETIC_SECRET" not in captured.out + captured.err


@pytest.mark.parametrize("field", sorted(policy_document()))
def test_every_policy_field_is_required(field: str) -> None:
    """Partial policy objects cannot masquerade as the disabled state."""
    document = policy_document()
    del document[field]
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.parse_external_observation_policy(document)


@pytest.mark.parametrize(
    "resource_type",
    [
        *sorted(classifier.EXPECTED_FOUNDATION_RESOURCE_TYPES),
        "microsoft.authorization/roleassignments",
        "microsoft.authorization/roledefinitions",
        "microsoft.app/containerapps",
        "microsoft.app/jobs",
        "microsoft.cache/redisenterprise",
        "microsoft.cache/redis",
        "microsoft.cognitiveservices/accounts/deployments",
        "microsoft.resources/deployments",
        "microsoft.storage/storageaccounts",
        "Microsoft.CognitiveServices/accounts",
        "*",
    ],
)
def test_policy_cannot_authorize_managed_nested_or_unreviewed_types(
    resource_type: str,
) -> None:
    """Only the reviewed canonical top-level type can be explicitly pinned."""
    document = policy_document()
    document["resource_type"] = resource_type
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.parse_external_observation_policy(document)


@pytest.mark.parametrize("field", ["scope_fingerprint", "resource_id_fingerprint"])
def test_valid_but_wrong_policy_fingerprint_rejects_observation(field: str) -> None:
    """A well-formed digest is insufficient without exact scope/identity matching."""
    document = policy_document()
    document[field] = "f" * 64
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.classify_foundation_whatif(
            foundation_document(),
            subscription_id=SUBSCRIPTION,
            resource_group=GROUP,
            external_policy=classifier.parse_external_observation_policy(document),
        )


@pytest.mark.parametrize("index", range(9))
def test_external_observation_cannot_replace_any_managed_role(index: int) -> None:
    """All nine original graph members remain required and managed."""
    document = foundation_document()
    document["changes"].pop(index)
    with pytest.raises(classifier.WhatIfClassificationError):
        classify(document)
    document = foundation_document()
    managed = document["changes"][index]
    managed["changeType"] = "Ignore"
    parsed = classifier._parse_resource_id(
        managed["resourceId"], subscription_id=SUBSCRIPTION, resource_group=GROUP
    )
    managed["before"] = {"id": managed["resourceId"], "type": parsed.resource_type}
    with pytest.raises(classifier.WhatIfClassificationError):
        classify(document)


@pytest.mark.parametrize(
    "extra",
    [
        None,
        {},
        {"resourceId": EXTERNAL_ID, "changeType": "Ignore", "unexpected": None},
        {
            "resourceId": EXTERNAL_ID + "/deployments/model",
            "changeType": "Ignore",
            "before": {
                "id": EXTERNAL_ID + "/deployments/model",
                "type": EXTERNAL_TYPE + "/deployments",
            },
        },
        {**observation(), "resourceId": EXTERNAL_ID.upper()},
    ],
)
def test_no_entry_is_filtered_before_shape_scope_or_duplicate_checks(
    extra: Any,
) -> None:
    """One valid observation never hides malformed, nested or duplicate entries."""
    document = foundation_document()
    document["changes"].append(extra)
    with pytest.raises(classifier.WhatIfClassificationError):
        classify(document)


@pytest.mark.parametrize("field", ["id", "type"])
def test_before_requires_both_identity_fields(field: str) -> None:
    """A nonempty properties object alone is not identity evidence."""
    document = foundation_document()
    del document["changes"][-1]["before"][field]
    with pytest.raises(classifier.WhatIfClassificationError):
        classify(document)


@pytest.mark.parametrize("field", ["potentialChanges", "diagnostics", "future"])
def test_external_policy_does_not_relax_top_level_signals(field: str) -> None:
    """Observation exceptions do not authorize incomplete or future diagnostics."""
    document = foundation_document()
    document[field] = [{}]
    with pytest.raises(classifier.WhatIfClassificationError):
        classify(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("after", None),
        ("delta", None),
        ("delta", []),
        ("extension", None),
        ("unsupportedReason", None),
        ("unsupportedReason", ""),
        ("identifiers", None),
        ("identifiers", {}),
        ("symbolicName", None),
        ("symbolicName", "syntheticSymbol"),
        ("deploymentId", None),
        (
            "deploymentId",
            EXTERNAL_ID.split("/providers/")[0]
            + "/providers/Microsoft.Resources/deployments/synthetic-deployment",
        ),
    ],
)
def test_every_accepted_optional_observation_field_is_fingerprinted(
    field: str, value: Any
) -> None:
    """Even accepted null/empty metadata is retained, never silently stripped."""
    original = classify()
    document = foundation_document()
    document["changes"][-1][field] = value
    updated = classify(document)
    assert updated.change_fingerprint != original.change_fingerprint
    assert updated.external_observations != original.external_observations


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (1, True),
        ("Value", "value"),
        (1234567890123456789012345678901, 1234567890123456789012345678902),
        (
            Decimal("0.1234567890123456789012345678901"),
            Decimal("0.1234567890123456789012345678902"),
        ),
    ],
)
def test_distinct_property_values_have_distinct_complete_digests(
    first: Any, second: Any
) -> None:
    """Canonical serialization must not round or casefold arbitrary properties."""
    document = foundation_document()
    document["changes"][-1]["before"]["properties"]["value"] = first
    original = classify(document)
    document["changes"][-1]["before"]["properties"]["value"] = second
    updated = classify(document)
    assert updated.external_observations != original.external_observations
    assert updated.change_fingerprint != original.change_fingerprint


def test_policy_drift_between_individually_valid_evidence_is_rejected() -> None:
    """Even a separately well-formed replacement policy cannot inherit approval."""
    document = converged_document()
    new_id = EXTERNAL_ID + "-other"
    document["changes"][-1]["resourceId"] = new_id
    document["changes"][-1]["before"]["id"] = new_id
    document["changes"][-1]["before"]["name"] = "synthetic-external-other"
    policy = policy_document()
    policy["resource_id_fingerprint"] = classifier.resource_identity_fingerprint(
        new_id, subscription_id=SUBSCRIPTION, resource_group=GROUP
    )
    result = classifier.classify_foundation_whatif(
        document,
        subscription_id=SUBSCRIPTION,
        resource_group=GROUP,
        external_policy=classifier.parse_external_observation_policy(policy),
    )
    replacement = classifier.build_foundation_evidence(
        result,
        commit_sha="a" * 40,
        parameter_fingerprint_value="b" * 64,
        deployment_source_fingerprint_value="c" * 64,
        deployment_source_file_count=3,
    )
    assert (
        replacement["external_policy"]["fingerprint"]
        != evidence()["external_policy"]["fingerprint"]
    )
    for compare in (
        classifier.compare_promotion_evidence,
        classifier.compare_convergence_evidence,
    ):
        with pytest.raises(classifier.WhatIfClassificationError):
            compare(evidence(), replacement)


@pytest.mark.parametrize("mode", ["Complete", "incremental", "", True, None])
def test_core_rejects_every_other_deployment_mode(mode: Any) -> None:
    """The programmatic API shares the CLI's exact Incremental contract."""
    with pytest.raises(classifier.WhatIfClassificationError):
        classifier.classify_foundation_whatif(
            foundation_document(),
            subscription_id=SUBSCRIPTION,
            resource_group=GROUP,
            deployment_mode=mode,
            external_policy=classifier.parse_external_observation_policy(
                policy_document()
            ),
        )


def test_policy_json_duplicate_keys_are_rejected_at_every_level() -> None:
    """Duplicate-aware parsing also rejects repeated otherwise-valid policy keys."""
    text = json.dumps(policy_document())
    duplicate = text[:-1] + ',"deployment_mode":"Incremental"}'
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "environ", {classifier.EXTERNAL_POLICY_ENV: duplicate})
        with pytest.raises(classifier.WhatIfClassificationError):
            classifier._external_policy_from_environment(classifier.EXTERNAL_POLICY_ENV)


def test_canonical_object_order_numeric_equivalence_and_input_immutability() -> None:
    """Reordering objects or equivalent JSON numbers changes no approved facts."""
    document = foundation_document()
    snapshot = copy.deepcopy(document)
    first = classify(document)
    reordered = json.loads(json.dumps(document, sort_keys=True))
    reordered["changes"][-1]["before"]["properties"]["capacity"] = Decimal("1.00")
    assert classify(reordered) == first
    assert document == snapshot
