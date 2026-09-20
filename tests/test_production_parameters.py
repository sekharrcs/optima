"""Tests for the canonical production foundation parameter artifact."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import production_parameters

COMMIT_SHA = "a" * 40


def environment(*, cache_enabled: bool = False) -> dict[str, str]:
    """Return complete synthetic protected production values."""
    values = {
        "AZURE_LOCATION": "eastus2",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "123456789",
        "GITHUB_SHA": COMMIT_SHA,
        "OPTIMA_FOUNDRY_BASE_URL": ("https://aoai-optima.openai.azure.com/openai/v1"),
        "OPTIMA_FOUNDRY_SMALL_DEPLOYMENT": "optima-small",
        "OPTIMA_FOUNDRY_SMALL_MODEL": "gpt-small",
        "OPTIMA_FOUNDRY_SMALL_MODEL_VERSION": "2026-01-01",
        "OPTIMA_FOUNDRY_STRONG_DEPLOYMENT": "optima-strong",
        "OPTIMA_FOUNDRY_STRONG_MODEL": "gpt-strong",
        "OPTIMA_FOUNDRY_STRONG_MODEL_VERSION": "2026-01-02",
        "OPTIMA_JUDGE_DEPLOYMENT": "optima-judge",
        "OPTIMA_JUDGE_MODEL": "gpt-judge",
        "OPTIMA_JUDGE_MODEL_VERSION": "2026-01-03",
        "OPTIMA_PRICING_CATALOG_VERSION": "catalog-1",
        "OPTIMA_PRICING_CURRENCY": "USD",
        "OPTIMA_PRICING_JUDGE_INPUT_RATE_PER_MILLION_TOKENS": "2.5",
        "OPTIMA_PRICING_JUDGE_OUTPUT_RATE_PER_MILLION_TOKENS": "10",
        "OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS": "0.05",
        "OPTIMA_PRICING_SMALL_OUTPUT_RATE_PER_MILLION_TOKENS": "0.4",
        "OPTIMA_PRICING_STRONG_INPUT_RATE_PER_MILLION_TOKENS": "1.75",
        "OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS": "14",
        "OPTIMA_SEMANTIC_CACHE_ENABLED": str(cache_enabled).lower(),
        "OPTIMA_UI_AUTH_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
        "OPTIMA_UI_AUTH_TENANT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }
    if cache_enabled:
        values.update(
            {
                "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS": "0.02",
                "OPTIMA_REDIS_EMBEDDING_DEPLOYMENT": "optima-embedding",
                "OPTIMA_REDIS_EMBEDDING_DIMENSION": "1536",
                "OPTIMA_REDIS_EMBEDDING_MODEL": "text-embedding",
            }
        )
    return values


def compiled_document() -> dict[str, object]:
    """Return a closed compiled runtime parameter skeleton."""
    names = (
        set(production_parameters._REQUIRED_ENVIRONMENT_PARAMETERS)
        | set(production_parameters._OPTIONAL_ENVIRONMENT_PARAMETERS)
        | {
            "deploymentCommitSha",
            "deploymentWorkflowRunId",
            "deployContainerApps",
            "deployRuntimeAccess",
            "environmentName",
            "exposePublicUi",
            "productionEvaluatorMode",
            "semanticCacheEnabled",
        }
    )
    return {
        "$schema": production_parameters.DEPLOYMENT_PARAMETERS_SCHEMA,
        "contentVersion": "1.0.0.0",
        "parameters": {name: {"value": None} for name in names},
    }


def values(document: dict[str, object]) -> dict[str, object]:
    """Project parameter values from a generated document."""
    parameters = document["parameters"]
    assert isinstance(parameters, dict)
    return {
        name: binding["value"]
        for name, binding in parameters.items()
        if isinstance(binding, dict)
    }


def test_disabled_profile_builds_one_canonical_secret_free_document() -> None:
    """Build the exact non-mutating cache-disabled foundation profile."""
    document = production_parameters.build_effective_document(
        compiled_document(), environment()
    )
    bindings = values(document)
    serialized = production_parameters.canonical_parameter_bytes(document)

    assert json.loads(serialized) == document
    assert serialized.endswith(b"\n")
    assert bindings["deploymentCommitSha"] == COMMIT_SHA
    assert bindings["deploymentWorkflowRunId"] == "123456789-1"
    assert bindings["semanticCacheEnabled"] is False
    assert bindings["deployContainerApps"] is False
    assert bindings["deployRuntimeAccess"] is False
    assert bindings["exposePublicUi"] is False
    assert "uiAuthClientSecret" not in bindings
    assert "redisEmbeddingDeployment" not in bindings


def test_enabled_profile_adds_only_complete_typed_cache_parameters() -> None:
    """The enabled profile adds the exact Redis and embedding Bicep inputs."""
    document = production_parameters.build_effective_document(
        compiled_document(), environment(cache_enabled=True)
    )
    bindings = values(document)

    assert bindings["semanticCacheEnabled"] is True
    assert bindings["redisEmbeddingDeployment"] == "optima-embedding"
    assert bindings["redisEmbeddingModel"] == "text-embedding"
    assert bindings["redisEmbeddingDimension"] == 1536
    assert bindings["pricingEmbeddingInputRatePerMillionTokens"] == "0.02"


def test_disabled_profile_rejects_cache_only_value() -> None:
    """Cache-only values cannot hide in a disabled artifact."""
    configured = environment()
    configured["OPTIMA_REDIS_EMBEDDING_MODEL"] = "unexpected"

    with pytest.raises(
        production_parameters.ProductionParameterError,
        match="cannot supply Redis or embedding",
    ):
        production_parameters.build_effective_document(compiled_document(), configured)


API_DIGEST = "sha256:" + ("a" * 64)
UI_DIGEST = "sha256:" + ("b" * 64)


def rollout_compiled_document() -> dict[str, object]:
    """Return a compiled base that also declares the image-digest passthroughs."""
    document = compiled_document()
    parameters = document["parameters"]
    assert isinstance(parameters, dict)
    parameters["apiImageDigest"] = {
        "value": production_parameters._PLACEHOLDER_IMAGE_DIGEST
    }
    parameters["uiImageDigest"] = {
        "value": production_parameters._PLACEHOLDER_IMAGE_DIGEST
    }
    return document


def test_internal_rollout_builds_deployable_container_apps_document() -> None:
    """The internal rollout artifact deploys Container Apps with an internal UI."""
    document = production_parameters.build_effective_document(
        rollout_compiled_document(),
        environment(),
        mode=production_parameters.MODE_INTERNAL_ROLLOUT,
        api_image_digest=API_DIGEST,
        ui_image_digest=UI_DIGEST,
    )
    bindings = values(document)

    assert bindings["deployContainerApps"] is True
    assert bindings["exposePublicUi"] is False
    assert bindings["deployRuntimeAccess"] is False
    assert bindings["apiImageDigest"] == API_DIGEST
    assert bindings["uiImageDigest"] == UI_DIGEST
    assert "uiAuthClientSecret" not in bindings


def test_public_rollout_exposes_the_ui() -> None:
    """The public rollout artifact differs only by exposing the UI ingress."""
    document = production_parameters.build_effective_document(
        rollout_compiled_document(),
        environment(),
        mode=production_parameters.MODE_PUBLIC_ROLLOUT,
        api_image_digest=API_DIGEST,
        ui_image_digest=UI_DIGEST,
    )
    bindings = values(document)

    assert bindings["deployContainerApps"] is True
    assert bindings["exposePublicUi"] is True


def test_rollout_requires_both_image_digests() -> None:
    """A rollout artifact cannot be built without both immutable digests."""
    with pytest.raises(
        production_parameters.ProductionParameterError, match="image digest"
    ):
        production_parameters.build_effective_document(
            rollout_compiled_document(),
            environment(),
            mode=production_parameters.MODE_INTERNAL_ROLLOUT,
            api_image_digest=API_DIGEST,
            ui_image_digest=None,
        )


def test_rollout_rejects_placeholder_image_digest() -> None:
    """A placeholder digest is never a deployable rollout binding."""
    with pytest.raises(
        production_parameters.ProductionParameterError, match="image digest"
    ):
        production_parameters.build_effective_document(
            rollout_compiled_document(),
            environment(),
            mode=production_parameters.MODE_PUBLIC_ROLLOUT,
            api_image_digest=production_parameters._PLACEHOLDER_IMAGE_DIGEST,
            ui_image_digest=UI_DIGEST,
        )


def test_rollout_rejects_identical_image_digests() -> None:
    """The API and UI must resolve to distinct manifests."""
    with pytest.raises(
        production_parameters.ProductionParameterError, match="must be distinct"
    ):
        production_parameters.build_effective_document(
            rollout_compiled_document(),
            environment(),
            mode=production_parameters.MODE_INTERNAL_ROLLOUT,
            api_image_digest=API_DIGEST,
            ui_image_digest=API_DIGEST,
        )


def test_foundation_mode_rejects_image_digests() -> None:
    """The foundation artifact must never bind runtime image digests."""
    with pytest.raises(
        production_parameters.ProductionParameterError, match="must not bind image"
    ):
        production_parameters.build_effective_document(
            compiled_document(),
            environment(),
            mode=production_parameters.MODE_FOUNDATION,
            api_image_digest=API_DIGEST,
            ui_image_digest=UI_DIGEST,
        )


def test_unexpected_compiled_parameter_is_rejected() -> None:
    """An unknown compiled binding must not survive generation."""
    compiled = compiled_document()
    parameters = compiled["parameters"]
    assert isinstance(parameters, dict)
    parameters["unexpectedParameter"] = {"value": "smuggled"}

    with pytest.raises(
        production_parameters.ProductionParameterError,
        match="unexpected",
    ):
        production_parameters.build_effective_document(compiled, environment())


def test_disabled_profile_rejects_compiled_cache_binding() -> None:
    """A disabled artifact cannot carry a nonempty compiled Redis model."""
    compiled = compiled_document()
    parameters = compiled["parameters"]
    assert isinstance(parameters, dict)
    parameters["redisEmbeddingModel"] = {"value": "text-embedding"}

    with pytest.raises(
        production_parameters.ProductionParameterError,
        match="Redis or embedding",
    ):
        production_parameters.build_effective_document(compiled, environment())


def test_disabled_profile_rejects_compiled_enabled_dimension() -> None:
    """A disabled artifact cannot carry an enabled compiled embedding dimension."""
    compiled = compiled_document()
    parameters = compiled["parameters"]
    assert isinstance(parameters, dict)
    parameters["redisEmbeddingDimension"] = {"value": 1536}

    with pytest.raises(
        production_parameters.ProductionParameterError,
        match="Redis or embedding",
    ):
        production_parameters.build_effective_document(compiled, environment())


def test_disabled_profile_accepts_canonical_disabled_cache_binding() -> None:
    """A null compiled cache binding is the canonical disabled representation."""
    compiled = compiled_document()
    parameters = compiled["parameters"]
    assert isinstance(parameters, dict)
    parameters["redisEmbeddingModel"] = {"value": None}
    parameters["redisEmbeddingDimension"] = {"value": None}

    document = production_parameters.build_effective_document(compiled, environment())
    bindings = values(document)

    assert "redisEmbeddingModel" not in bindings
    assert "redisEmbeddingDimension" not in bindings


def test_interrupted_publication_leaves_no_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted write must never publish a partial destination file."""
    output = tmp_path / "effective.json"

    def failing_fsync(descriptor: int) -> None:
        raise OSError("publication interrupted before completion")

    monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(OSError):
        production_parameters._write_new_file(output, b"x" * 128)

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_publication_refuses_existing_destination(tmp_path: Path) -> None:
    """Atomic publication preserves fail-if-destination-exists semantics."""
    output = tmp_path / "effective.json"
    output.write_bytes(b"existing")

    with pytest.raises(OSError):
        production_parameters._write_new_file(output, b"replacement")

    assert output.read_bytes() == b"existing"
    assert list(tmp_path.iterdir()) == [output]


def test_cli_writes_exclusive_regular_artifact_and_reports_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI creates one regular artifact and refuses replacement."""
    compiled = tmp_path / "compiled.json"
    output = tmp_path / "effective.json"
    compiled.write_text(json.dumps(compiled_document()), encoding="utf-8")
    monkeypatch.setattr(os, "environ", environment())

    assert (
        production_parameters.main(
            ["--compiled-base", str(compiled), "--output", str(output)]
        )
        == 0
    )
    digest = capsys.readouterr().out.strip()
    assert digest == hashlib.sha256(output.read_bytes()).hexdigest()
    assert output.is_file()

    assert (
        production_parameters.main(
            ["--compiled-base", str(compiled), "--output", str(output)]
        )
        == 1
    )
    assert "PRODUCTION PARAMETERS FAILED" in capsys.readouterr().err
