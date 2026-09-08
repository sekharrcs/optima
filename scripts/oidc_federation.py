"""Exact GitHub-to-Azure federation contract for OPTIMA's reviewed repository."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

REPOSITORY = "sekharrcs/optima"
REPOSITORY_ID = 1333906197
OWNER_ID = 45002138
ENVIRONMENT = "hackathon"
SUBJECT_PREFIX = f"repo:sekharrcs@{OWNER_ID}/optima@{REPOSITORY_ID}"
SUBJECT = f"{SUBJECT_PREFIX}:environment:{ENVIRONMENT}"
ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "api://AzureADTokenExchange"
CREDENTIAL_NAME = "github-optima-hackathon"
SUBSCRIPTION_ID = "cce38a08-26e8-4b74-8fdb-df7a6db795ed"
TENANT_ID = "d04cc813-b8d5-4eba-aca4-391c3278fd1a"
BOOTSTRAP_GROUP = "rg-optima-bootstrap"


@dataclass(frozen=True)
class IdentityBinding:
    """Reviewed identity metadata, never credentials or access tokens."""

    name: str
    client_id: str
    principal_id: str

    @property
    def resource_id(self) -> str:
        return (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{BOOTSTRAP_GROUP}/"
            f"providers/Microsoft.ManagedIdentity/userAssignedIdentities/{self.name}"
        )


IDENTITIES = (
    IdentityBinding(
        "id-optima-github-foundation-plan",
        "4f75358a-eb79-495a-bca4-f1a39f20169c",
        "8afc339b-298d-4d8a-a57e-edb5e5bfd150",
    ),
    IdentityBinding(
        "id-optima-github-deployer",
        "cc0f08d6-53ff-4f21-b0df-25742f1b69a5",
        "e78ea309-d34a-4c37-8c44-ee76ed86e628",
    ),
)


class FederationError(ValueError):
    """A reviewed federation binding could not be established."""


def validate_github_subject(repository: Any, settings: Any) -> None:
    """Compare GitHub's effective default prefix with the reviewed identity."""
    if not isinstance(repository, dict) or not isinstance(settings, dict):
        raise FederationError("GitHub repository or OIDC metadata is malformed")
    owner = repository.get("owner")
    if (
        repository.get("full_name") != REPOSITORY
        or type(repository.get("id")) is not int
        or repository["id"] != REPOSITORY_ID
        or not isinstance(owner, dict)
        or owner.get("login") != "sekharrcs"
        or owner.get("type") != "User"
        or type(owner.get("id")) is not int
        or owner["id"] != OWNER_ID
    ):
        raise FederationError(
            "GitHub repository identity differs from reviewed binding"
        )
    if (
        settings.get("use_default") is not True
        or type(settings.get("use_immutable_subject")) is not bool
        or settings.get("sub_claim_prefix") != SUBJECT_PREFIX
    ):
        raise FederationError(
            "GitHub effective OIDC subject differs from reviewed binding"
        )


def validate_federated_credentials(
    credentials: Any, *, identity_resource_id: str
) -> None:
    """Require the sole exact credential on the selected managed identity."""
    if not isinstance(credentials, list) or len(credentials) != 1:
        raise FederationError("Expected exactly one GitHub environment credential")
    credential = credentials[0]
    expected_id = (
        f"{identity_resource_id}/federatedIdentityCredentials/{CREDENTIAL_NAME}"
    )
    if (
        not isinstance(credential, dict)
        or not isinstance(credential.get("id"), str)
        or credential["id"].casefold() != expected_id.casefold()
        or credential.get("name") != CREDENTIAL_NAME
        or credential.get("issuer") != ISSUER
        or credential.get("subject") != SUBJECT
        or credential.get("audiences") != [AUDIENCE]
        or credential.get("claimsMatchingExpression") is not None
    ):
        raise FederationError("GitHub environment federated credential is mismatched")


class MetadataQuery(Protocol):
    """Injectable metadata-only CLI boundary."""

    def json(self, executable: str, *arguments: str) -> Any:
        """Return JSON from a read-only metadata query."""


class CliMetadataQuery:
    """Use existing CLI sessions; never log in or request an OIDC assertion."""

    def json(self, executable: str, *arguments: str) -> Any:
        path = shutil.which(executable)
        if path is None:
            raise FederationError(f"{executable} is required for federation readiness")
        try:
            result = subprocess.run(
                [path, *arguments],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise FederationError(f"{executable} metadata query unavailable") from error
        if result.returncode:
            raise FederationError(f"{executable} metadata query failed")
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise FederationError(
                f"{executable} metadata response is malformed"
            ) from error


def check_readiness(query: MetadataQuery) -> None:
    """Read GitHub metadata and both exact Azure bindings before dispatch."""
    repository = query.json("gh", "api", f"repos/{REPOSITORY}")
    settings = query.json(
        "gh", "api", f"repos/{REPOSITORY}/actions/oidc/customization/sub"
    )
    validate_github_subject(repository, settings)
    mismatches: list[str] = []
    for binding in IDENTITIES:
        identity = query.json(
            "az",
            "identity",
            "show",
            "--ids",
            binding.resource_id,
            "--only-show-errors",
            "--output",
            "json",
        )
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("id"), str)
            or identity["id"].casefold() != binding.resource_id.casefold()
            or identity.get("clientId") != binding.client_id
            or identity.get("principalId") != binding.principal_id
            or identity.get("tenantId") != TENANT_ID
        ):
            raise FederationError(f"Reviewed identity binding differs: {binding.name}")
        credentials = query.json(
            "az",
            "identity",
            "federated-credential",
            "list",
            "--subscription",
            SUBSCRIPTION_ID,
            "--resource-group",
            BOOTSTRAP_GROUP,
            "--identity-name",
            binding.name,
            "--only-show-errors",
            "--output",
            "json",
        )
        try:
            validate_federated_credentials(
                credentials, identity_resource_id=binding.resource_id
            )
        except FederationError:
            mismatches.append(binding.name)
    if mismatches:
        raise FederationError(f"Federation mismatch: {', '.join(mismatches)}")


def create_parser() -> argparse.ArgumentParser:
    """Describe the metadata-only readiness command."""
    return argparse.ArgumentParser(
        description="Check GitHub/Azure federation metadata; no dispatch or login."
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Fail closed before dispatch; live token exchange remains a separate test."""
    create_parser().parse_args(argv)
    try:
        check_readiness(CliMetadataQuery())
    except FederationError as error:
        print(f"Federation readiness FAILED: {error}", file=sys.stderr)
        return 1
    print(f"Federation metadata matches both reviewed identities: {SUBJECT}")
    print("Live GitHub OIDC token exchange has not been tested by this check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
