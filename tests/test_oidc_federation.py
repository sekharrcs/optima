"""Reject the live legacy federation mismatch without requesting any token."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from scripts import oidc_federation
from scripts.oidc_federation import (
    AUDIENCE,
    CREDENTIAL_NAME,
    ISSUER,
    SUBJECT,
    SUBJECT_PREFIX,
    FederationError,
    validate_federated_credentials,
    validate_github_subject,
)

IDENTITY_ID = (
    "/subscriptions/test/resourceGroups/bootstrap/providers/"
    "Microsoft.ManagedIdentity/userAssignedIdentities/plan"
)


def credential() -> dict[str, Any]:
    return {
        "id": f"{IDENTITY_ID}/federatedIdentityCredentials/{CREDENTIAL_NAME}",
        "name": CREDENTIAL_NAME,
        "issuer": ISSUER,
        "subject": SUBJECT,
        "audiences": [AUDIENCE],
    }


def repository() -> dict[str, Any]:
    return {
        "full_name": "sekharrcs/optima",
        "id": 1333906197,
        "owner": {"login": "sekharrcs", "id": 45002138, "type": "User"},
    }


def settings() -> dict[str, Any]:
    return {
        "use_default": True,
        "use_immutable_subject": False,
        "sub_claim_prefix": SUBJECT_PREFIX,
    }


def test_exact_reviewed_federation_passes() -> None:
    validate_github_subject(repository(), settings())
    validate_federated_credentials([credential()], identity_resource_id=IDENTITY_ID)


@pytest.mark.parametrize(
    "subject",
    [
        "repo:sekharrcs/optima:environment:hackathon",
        SUBJECT.replace("45002138", "45002139"),
        SUBJECT.replace("1333906197", "1333906198"),
        SUBJECT.replace("hackathon", "production"),
        SUBJECT.replace("environment:hackathon", "ref:refs/heads/main"),
        SUBJECT + ":job_workflow_ref:other",
        SUBJECT.upper(),
        SUBJECT + " ",
        "repo:*/optima:environment:hackathon",
    ],
)
def test_subject_requires_exact_match(subject: str) -> None:
    value = credential() | {"subject": subject}
    with pytest.raises(FederationError, match="mismatched"):
        validate_federated_credentials([value], identity_resource_id=IDENTITY_ID)


@pytest.mark.parametrize(
    "update",
    [
        {"issuer": ISSUER + "/"},
        {"issuer": "https://example.com"},
        {"audiences": [AUDIENCE, "extra"]},
        {"audiences": AUDIENCE},
        {"audiences": []},
        {"name": "other"},
        {"id": IDENTITY_ID + "/other"},
        {"claimsMatchingExpression": {"value": "*", "languageVersion": 1}},
    ],
)
def test_other_trust_fields_remain_exact(update: dict[str, Any]) -> None:
    with pytest.raises(FederationError):
        validate_federated_credentials(
            [credential() | update], identity_resource_id=IDENTITY_ID
        )


@pytest.mark.parametrize("values", [None, {}, [], [credential(), credential()]])
def test_credential_inventory_fails_closed(values: Any) -> None:
    with pytest.raises(FederationError):
        validate_federated_credentials(values, identity_resource_id=IDENTITY_ID)


@pytest.mark.parametrize(
    "update",
    [
        {"use_default": False},
        {"use_default": "true"},
        {"use_immutable_subject": None},
        {"sub_claim_prefix": "repo:sekharrcs/optima"},
        {"sub_claim_prefix": SUBJECT_PREFIX + ":job_workflow_ref:other"},
    ],
)
def test_effective_prefix_not_boolean_decides_format(update: dict[str, Any]) -> None:
    with pytest.raises(FederationError):
        validate_github_subject(repository(), settings() | update)


@pytest.mark.parametrize("field", ["id", "full_name", "owner"])
def test_repository_identity_is_required(field: str) -> None:
    value = repository()
    value.pop(field)
    with pytest.raises(FederationError):
        validate_github_subject(value, settings())


@pytest.mark.parametrize(
    "update",
    [
        {"id": 1333906198},
        {"id": "1333906197"},
        {"full_name": "sekharrcs/other"},
        {"owner": {"id": 45002139, "login": "sekharrcs", "type": "User"}},
        {"owner": {"id": 45002138, "login": "other", "type": "User"}},
        {"owner": {"id": 45002138, "login": "sekharrcs", "type": "Organization"}},
    ],
)
def test_changed_repository_metadata_fails_closed(update: dict[str, Any]) -> None:
    with pytest.raises(FederationError):
        validate_github_subject(repository() | update, settings())


class FakeMetadata:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.credentials: dict[str, list[dict[str, Any]]] = {
            binding.name: [
                credential()
                | {
                    "id": (
                        f"{binding.resource_id}/federatedIdentityCredentials/"
                        f"{CREDENTIAL_NAME}"
                    )
                }
            ]
            for binding in oidc_federation.IDENTITIES
        }
        self.identity_overrides: dict[str, Any] = {}

    def json(self, executable: str, *arguments: str) -> Any:
        self.calls.append((executable, *arguments))
        if executable == "gh":
            assert arguments[0] == "api"
            if arguments[1].endswith("/actions/oidc/customization/sub"):
                return settings()
            assert arguments[1] == "repos/sekharrcs/optima"
            return repository()
        assert executable == "az"
        if arguments[:2] == ("identity", "show"):
            binding = next(
                value
                for value in oidc_federation.IDENTITIES
                if value.resource_id == arguments[3]
            )
            return {
                "id": binding.resource_id,
                "clientId": binding.client_id,
                "principalId": binding.principal_id,
                "tenantId": oidc_federation.TENANT_ID,
            } | self.identity_overrides
        assert arguments[:3] == ("identity", "federated-credential", "list")
        return self.credentials[arguments[arguments.index("--identity-name") + 1]]


def test_readiness_checks_both_separate_identities_without_mutations() -> None:
    query = FakeMetadata()
    oidc_federation.check_readiness(query)
    assert len(query.calls) == 6
    assert [call[:3] for call in query.calls] == [
        ("gh", "api", "repos/sekharrcs/optima"),
        ("gh", "api", "repos/sekharrcs/optima/actions/oidc/customization/sub"),
        ("az", "identity", "show"),
        ("az", "identity", "federated-credential"),
        ("az", "identity", "show"),
        ("az", "identity", "federated-credential"),
    ]
    for field in ("resource_id", "client_id", "principal_id"):
        assert (
            len({getattr(binding, field) for binding in oidc_federation.IDENTITIES})
            == 2
        )


@pytest.mark.parametrize("index", [0, 1])
def test_each_identity_must_have_corrected_subject(index: int) -> None:
    query = FakeMetadata()
    binding = oidc_federation.IDENTITIES[index]
    query.credentials[binding.name][0]["subject"] = (
        "repo:sekharrcs/optima:environment:hackathon"
    )
    with pytest.raises(FederationError, match=binding.name):
        oidc_federation.check_readiness(query)
    assert len(query.calls) == 6


def test_current_legacy_configuration_reports_both_mismatches() -> None:
    query = FakeMetadata()
    for values in query.credentials.values():
        values[0]["subject"] = "repo:sekharrcs/optima:environment:hackathon"
    with pytest.raises(FederationError) as caught:
        oidc_federation.check_readiness(query)
    assert all(
        binding.name in str(caught.value) for binding in oidc_federation.IDENTITIES
    )


@pytest.mark.parametrize("field", ["id", "clientId", "principalId", "tenantId"])
def test_readiness_rejects_changed_identity_binding(field: str) -> None:
    query = FakeMetadata()
    query.identity_overrides[field] = "different"
    with pytest.raises(FederationError, match="identity binding"):
        oidc_federation.check_readiness(query)


@pytest.mark.parametrize(
    "response",
    [
        subprocess.CompletedProcess([], 1, "sensitive stdout", "sensitive stderr"),
        subprocess.CompletedProcess([], 0, "sensitive non-JSON stdout", ""),
    ],
)
def test_cli_errors_never_echo_output(
    monkeypatch: pytest.MonkeyPatch,
    response: subprocess.CompletedProcess[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("scripts.oidc_federation.shutil.which", lambda name: name)
    monkeypatch.setattr(
        "scripts.oidc_federation.subprocess.run", lambda *args, **kwargs: response
    )
    with pytest.raises(FederationError) as caught:
        oidc_federation.CliMetadataQuery().json("gh", "api", "repos/sekharrcs/optima")
    assert "sensitive" not in str(caught.value)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert oidc_federation.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "FAILED" in captured.err
    assert "sensitive" not in captured.err


def test_cli_uses_bounded_non_shell_execution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert arguments == ["gh", "api", "repos/sekharrcs/optima"]
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": 90,
            "check": False,
        }
        raise subprocess.TimeoutExpired(arguments, 90, output="sensitive")

    monkeypatch.setattr("scripts.oidc_federation.shutil.which", lambda name: name)
    monkeypatch.setattr("scripts.oidc_federation.subprocess.run", run)
    with pytest.raises(FederationError, match="unavailable"):
        oidc_federation.CliMetadataQuery().json("gh", "api", "repos/sekharrcs/optima")
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert oidc_federation.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unavailable" in captured.err
    assert "sensitive" not in captured.err


def test_main_exit_codes_and_no_authentication_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    query = FakeMetadata()
    monkeypatch.setattr(oidc_federation, "CliMetadataQuery", lambda: query)
    assert oidc_federation.main([]) == 0
    assert "has not been tested" in capsys.readouterr().out
    query.credentials[oidc_federation.IDENTITIES[0].name] = []
    assert oidc_federation.main([]) == 1
    assert "FAILED" in capsys.readouterr().err
