"""Build one canonical Azure parameter artifact for production foundation use."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from scripts.whatif_classification import read_regular_file
else:
    from whatif_classification import read_regular_file

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
DEPLOYMENT_PARAMETERS_SCHEMA = (
    "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
)
MAX_PARAMETER_FILE_BYTES = 2 * 1024 * 1024
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_RUN_NUMBER = re.compile(r"[1-9][0-9]{0,19}")
_PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_REQUIRED_ENVIRONMENT_PARAMETERS = {
    "foundryBaseUrl": "OPTIMA_FOUNDRY_BASE_URL",
    "foundrySmallDeployment": "OPTIMA_FOUNDRY_SMALL_DEPLOYMENT",
    "foundrySmallModel": "OPTIMA_FOUNDRY_SMALL_MODEL",
    "foundrySmallModelVersion": "OPTIMA_FOUNDRY_SMALL_MODEL_VERSION",
    "foundryStrongDeployment": "OPTIMA_FOUNDRY_STRONG_DEPLOYMENT",
    "foundryStrongModel": "OPTIMA_FOUNDRY_STRONG_MODEL",
    "foundryStrongModelVersion": "OPTIMA_FOUNDRY_STRONG_MODEL_VERSION",
    "judgeDeployment": "OPTIMA_JUDGE_DEPLOYMENT",
    "judgeModel": "OPTIMA_JUDGE_MODEL",
    "judgeModelVersion": "OPTIMA_JUDGE_MODEL_VERSION",
    "location": "AZURE_LOCATION",
    "pricingCatalogVersion": "OPTIMA_PRICING_CATALOG_VERSION",
    "pricingCurrency": "OPTIMA_PRICING_CURRENCY",
    "pricingJudgeInputRatePerMillionTokens": (
        "OPTIMA_PRICING_JUDGE_INPUT_RATE_PER_MILLION_TOKENS"
    ),
    "pricingJudgeOutputRatePerMillionTokens": (
        "OPTIMA_PRICING_JUDGE_OUTPUT_RATE_PER_MILLION_TOKENS"
    ),
    "pricingSmallInputRatePerMillionTokens": (
        "OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS"
    ),
    "pricingSmallOutputRatePerMillionTokens": (
        "OPTIMA_PRICING_SMALL_OUTPUT_RATE_PER_MILLION_TOKENS"
    ),
    "pricingStrongInputRatePerMillionTokens": (
        "OPTIMA_PRICING_STRONG_INPUT_RATE_PER_MILLION_TOKENS"
    ),
    "pricingStrongOutputRatePerMillionTokens": (
        "OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS"
    ),
    "uiAuthClientId": "OPTIMA_UI_AUTH_CLIENT_ID",
    "uiAuthTenantId": "OPTIMA_UI_AUTH_TENANT_ID",
}
_OPTIONAL_ENVIRONMENT_PARAMETERS = {
    "pricingJudgeCachedInputRatePerMillionTokens": (
        "OPTIMA_PRICING_JUDGE_CACHED_INPUT_RATE_PER_MILLION_TOKENS"
    ),
    "pricingSmallCachedInputRatePerMillionTokens": (
        "OPTIMA_PRICING_SMALL_CACHED_INPUT_RATE_PER_MILLION_TOKENS"
    ),
    "pricingStrongCachedInputRatePerMillionTokens": (
        "OPTIMA_PRICING_STRONG_CACHED_INPUT_RATE_PER_MILLION_TOKENS"
    ),
}
_CACHE_ENVIRONMENT_PARAMETERS = {
    "pricingEmbeddingInputRatePerMillionTokens": (
        "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS"
    ),
    "redisEmbeddingDeployment": "OPTIMA_REDIS_EMBEDDING_DEPLOYMENT",
    "redisEmbeddingModel": "OPTIMA_REDIS_EMBEDDING_MODEL",
}
# Cache Bicep bindings that only ever appear in the enabled profile. When the
# cache is disabled their canonical representation is absence; a compiled base
# that supplies any of them with a nonempty value is rejected.
_CACHE_PARAMETER_NAMES = frozenset(_CACHE_ENVIRONMENT_PARAMETERS) | {
    "redisEmbeddingDimension"
}
_CACHE_ENVIRONMENT_VARIABLES = frozenset(_CACHE_ENVIRONMENT_PARAMETERS.values()) | {
    "OPTIMA_REDIS_EMBEDDING_DIMENSION"
}
# Compiled bindings that OPTIMA passes through unchanged: they are declared by
# the runtime template and carried by hackathon.runtime.bicepparam but are not
# rebound from the protected environment. The closed contract admits exactly
# these names in addition to the rebound overrides and the cache bindings.
_ALLOWED_PASSTHROUGH_PARAMETERS = frozenset(
    {
        "apiImageDigest",
        "applicationInsightsSamplingRatio",
        "foundryTokenScope",
        "judgeTimeoutSeconds",
        "uiImageDigest",
    }
)


class ProductionParameterError(RuntimeError):
    """The effective production parameter artifact cannot be trusted."""


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value or value != value.strip():
        raise ProductionParameterError(f"Required production value {name} is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ProductionParameterError(f"Required production value {name} is invalid")
    return value


def _load_compiled_document(path: Path) -> dict[str, Any]:
    try:
        content = read_regular_file(
            path,
            maximum_bytes=MAX_PARAMETER_FILE_BYTES,
        )
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ProductionParameterError(
            "Compiled runtime parameters are unavailable or malformed"
        ) from error
    if (
        not isinstance(document, dict)
        or set(document) != {"$schema", "contentVersion", "parameters"}
        or document["$schema"] != DEPLOYMENT_PARAMETERS_SCHEMA
        or document["contentVersion"] != "1.0.0.0"
        or not isinstance(document["parameters"], dict)
    ):
        raise ProductionParameterError(
            "Compiled runtime parameters have an unsupported schema"
        )
    for name, binding in document["parameters"].items():
        if (
            not isinstance(name, str)
            or _PARAMETER_NAME.fullmatch(name) is None
            or not isinstance(binding, dict)
            or set(binding) != {"value"}
            or isinstance(binding["value"], (dict, list))
        ):
            raise ProductionParameterError(
                "Compiled runtime parameter binding is malformed"
            )
    if "uiAuthClientSecret" in document["parameters"]:
        raise ProductionParameterError(
            "The foundation parameter artifact must not contain the UI secret"
        )
    return document


def build_effective_document(
    compiled: Mapping[str, Any], environment: Mapping[str, str]
) -> dict[str, Any]:
    """Return the canonical runtime parameter document for foundation mutation."""
    parameters = compiled.get("parameters")
    if not isinstance(parameters, dict):
        raise ProductionParameterError("Compiled runtime parameters are malformed")
    values = {
        name: dict(binding)
        for name, binding in parameters.items()
        if isinstance(name, str) and isinstance(binding, dict)
    }
    if len(values) != len(parameters):
        raise ProductionParameterError("Compiled runtime parameters are malformed")

    commit_sha = _required(environment, "GITHUB_SHA")
    run_id = _required(environment, "GITHUB_RUN_ID")
    run_attempt = _required(environment, "GITHUB_RUN_ATTEMPT")
    if _COMMIT_SHA.fullmatch(commit_sha) is None:
        raise ProductionParameterError("GITHUB_SHA must be a full lowercase commit SHA")
    if (
        _RUN_NUMBER.fullmatch(run_id) is None
        or _RUN_NUMBER.fullmatch(run_attempt) is None
    ):
        raise ProductionParameterError("GitHub run identity is malformed")
    cache_text = _required(environment, "OPTIMA_SEMANTIC_CACHE_ENABLED")
    if cache_text not in {"true", "false"}:
        raise ProductionParameterError(
            "OPTIMA_SEMANTIC_CACHE_ENABLED must be exactly true or false"
        )
    cache_enabled = cache_text == "true"

    overrides: dict[str, Any] = {
        "deploymentCommitSha": commit_sha,
        "deploymentWorkflowRunId": f"{run_id}-{run_attempt}",
        "deployContainerApps": False,
        "deployRuntimeAccess": False,
        "environmentName": "hackathon",
        "exposePublicUi": False,
        "productionEvaluatorMode": "LLM_JUDGE",
        "semanticCacheEnabled": cache_enabled,
    }
    overrides.update(
        {
            parameter: _required(environment, variable)
            for parameter, variable in _REQUIRED_ENVIRONMENT_PARAMETERS.items()
        }
    )
    for parameter, variable in _OPTIONAL_ENVIRONMENT_PARAMETERS.items():
        raw = environment.get(variable, "")
        overrides[parameter] = _required(environment, variable) if raw else None

    if cache_enabled:
        overrides.update(
            {
                parameter: _required(environment, variable)
                for parameter, variable in _CACHE_ENVIRONMENT_PARAMETERS.items()
            }
        )
        dimension = _required(environment, "OPTIMA_REDIS_EMBEDDING_DIMENSION")
        try:
            overrides["redisEmbeddingDimension"] = int(dimension)
        except ValueError as error:
            raise ProductionParameterError(
                "OPTIMA_REDIS_EMBEDDING_DIMENSION must be an integer"
            ) from error

    # Closed name contract: the compiled base may carry only the rebound
    # overrides, the reviewed pass-through bindings, and the cache bindings.
    # Any other compiled parameter -- including unexpectedParameter -- fails.
    allowed_names = (
        set(overrides) | _ALLOWED_PASSTHROUGH_PARAMETERS | _CACHE_PARAMETER_NAMES
    )
    unexpected = sorted(name for name in values if name not in allowed_names)
    if unexpected:
        raise ProductionParameterError(
            "Compiled runtime parameters contain unexpected bindings: "
            + ", ".join(unexpected)
        )

    if not cache_enabled:
        # A disabled artifact rejects any enabled cache value from either the
        # protected environment or the compiled base, and never emits a cache
        # binding. Absence or an explicit null is the canonical disabled state.
        if any(environment.get(name, "") for name in _CACHE_ENVIRONMENT_VARIABLES):
            raise ProductionParameterError(
                "Disabled semantic cache cannot supply Redis or embedding parameters"
            )
        for name in _CACHE_PARAMETER_NAMES:
            binding = values.get(name)
            binding_value = binding.get("value") if isinstance(binding, dict) else None
            if binding_value not in (None, ""):
                raise ProductionParameterError(
                    "Disabled semantic cache cannot supply Redis or embedding "
                    "parameters"
                )
            values.pop(name, None)

    missing = sorted(
        name
        for name in overrides
        if name not in values and name not in _CACHE_PARAMETER_NAMES
    )
    if missing:
        raise ProductionParameterError(
            "Compiled runtime parameters are missing required bindings: "
            + ", ".join(missing)
        )
    for name, value in overrides.items():
        values[name] = {"value": value}
    return {
        "$schema": DEPLOYMENT_PARAMETERS_SCHEMA,
        "contentVersion": "1.0.0.0",
        "parameters": values,
    }


def canonical_parameter_bytes(document: Mapping[str, Any]) -> bytes:
    """Serialize one effective parameter document in canonical UTF-8 JSON."""
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _fsync_directory(directory: Path) -> None:
    """Apply a best-effort durability barrier to a directory entry."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_new_file(path: Path, content: bytes) -> None:
    """Publish content atomically and exclusively to a new destination.

    The bytes are written to a unique 0600 temporary file in the destination
    directory, flushed and fsynced, verified, then published with a no-replace
    hard link. An interruption therefore never leaves a partial or overwritten
    destination: only a temporary file can exist, and it is always removed. The
    destination directory is fsynced where the platform supports it.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing artifact {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory, prefix=".optima-parameters-"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary_path.read_bytes() != content:
            raise ProductionParameterError(
                "Effective production parameter artifact changed before publication"
            )
        os.link(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    temporary_path.unlink(missing_ok=True)
    _fsync_directory(directory)


def create_parser() -> argparse.ArgumentParser:
    """Create the effective production parameter CLI parser."""
    parser = argparse.ArgumentParser(
        description="Build one canonical production foundation parameter artifact."
    )
    parser.add_argument("--compiled-base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build the artifact and print its captured SHA-256 digest."""
    arguments = create_parser().parse_args(argv)
    try:
        compiled = _load_compiled_document(arguments.compiled_base)
        document = build_effective_document(compiled, os.environ)
        content = canonical_parameter_bytes(document)
        _write_new_file(arguments.output, content)
        verified = read_regular_file(
            arguments.output,
            maximum_bytes=MAX_PARAMETER_FILE_BYTES,
        )
        if verified != content:
            raise ProductionParameterError(
                "Effective production parameter artifact changed after creation"
            )
        print(hashlib.sha256(content).hexdigest())
    except (OSError, ProductionParameterError, ValueError) as error:
        print(f"PRODUCTION PARAMETERS FAILED: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
