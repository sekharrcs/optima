"""Compiled contracts for optional foundation and application dependencies."""

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_OUTPUT_PARAMETERS = (
    "apiIdentityClientId",
    "apiIdentityPrincipalId",
    "apiIdentityResourceId",
    "apiImage",
    "applicationInsightsConnectionString",
    "cosmosContainerName",
    "cosmosDatabaseName",
    "cosmosEndpoint",
    "registryLoginServer",
    "uiIdentityResourceId",
    "uiImage",
)


@pytest.fixture(scope="module")
def compiled_foundation() -> dict[str, Any]:
    """Compile local source only, using the pinned standalone Bicep executable."""
    command = os.environ.get("OPTIMA_BICEP_COMMAND") or shutil.which("bicep")
    if command is None:
        pytest.skip("Set OPTIMA_BICEP_COMMAND to the Bicep 0.46.1 executable")
    version = subprocess.run(
        [command, "--version"], capture_output=True, text=True, check=True
    )
    assert version.stdout.startswith("Bicep CLI version 0.46.1 ")
    result = subprocess.run(
        [command, "build", str(ROOT / "infra/resource-group.bicep"), "--stdout"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stderr == ""
    template: dict[str, Any] = json.loads(result.stdout)
    return template


@pytest.mark.parametrize("parameter", RUNTIME_OUTPUT_PARAMETERS)
def test_disabled_applications_guard_parent_output_lookups(
    compiled_foundation: dict[str, Any], parameter: str
) -> None:
    """A false child resource condition cannot guard its parent parameter inputs."""
    module = compiled_foundation["resources"]["containerApps"]
    assert "condition" not in module
    assert module["properties"]["expressionEvaluationOptions"]["scope"] == "inner"
    expression = module["properties"]["parameters"][parameter]
    assert expression.startswith("[if(parameters('deployContainerApps'), ")
    assert expression.endswith(", createObject('value', null()))]")


def evaluate_arm(
    value: Any, parameters: dict[str, Any], variables: dict[str, Any]
) -> Any:
    """Evaluate only the emitted pure-expression subset, never cloud functions."""
    if isinstance(value, dict):
        return {
            evaluate_arm(key, parameters, variables): evaluate_arm(
                item, parameters, variables
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [evaluate_arm(item, parameters, variables) for item in value]
    if not isinstance(value, str) or not value.startswith("["):
        return value

    expression = value[1:-1]
    token_pattern = re.compile(r"\s*('(?:[^']|'')*'|-?\d+|[A-Za-z_]\w*|[(),.])")
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        match = token_pattern.match(expression, position)
        assert match is not None, f"Unsupported ARM syntax at {expression[position:]}"
        tokens.append(match.group(1))
        position = match.end()
    cursor = 0

    def parse() -> Any:
        nonlocal cursor
        token = tokens[cursor]
        cursor += 1
        if token.startswith("'"):
            return token[1:-1].replace("''", "'")
        if re.fullmatch(r"-?\d+", token):
            return int(token)
        assert tokens[cursor] == "("
        cursor += 1
        arguments = []
        while tokens[cursor] != ")":
            arguments.append(parse())
            if tokens[cursor] != ")":
                assert tokens[cursor] == ","
                cursor += 1
        cursor += 1
        node: Any = token, arguments
        while cursor < len(tokens) and tokens[cursor] == ".":
            cursor += 1
            node = "property", [node, tokens[cursor]]
            cursor += 1
        return node

    def resolve(node: Any) -> Any:
        if not isinstance(node, tuple):
            return node
        name, arguments = node
        if name == "if":
            return resolve(arguments[1] if resolve(arguments[0]) else arguments[2])
        if name == "parameters":
            return parameters[resolve(arguments[0])]
        if name == "variables":
            return evaluate_arm(variables[resolve(arguments[0])], parameters, variables)
        if name == "property":
            return resolve(arguments[0])[resolve(arguments[1])]
        if name == "fail":
            raise ValueError(resolve(arguments[0]))
        operations: dict[str, Callable[..., Any]] = {
            "and": lambda *items: all(items),
            "or": lambda *items: any(items),
            "not": lambda item: not item,
            "true": lambda: True,
            "false": lambda: False,
            "null": lambda: None,
            "equals": lambda left, right: left == right,
            "coalesce": lambda *items: next(
                (item for item in items if item is not None), None
            ),
            "empty": lambda item: item is None or len(item) == 0,
            "trim": lambda item: item.strip(),
            "toLower": lambda item: item.lower(),
            "startsWith": lambda item, prefix: item.lower().startswith(prefix.lower()),
            "greaterOrEquals": lambda left, right: left >= right,
            "lessOrEquals": lambda left, right: left <= right,
            "createArray": lambda *items: list(items),
            "createObject": lambda *items: dict(
                zip(items[::2], items[1::2], strict=True)
            ),
            "concat": lambda *items: sum(items, []),
            "union": lambda *items: {
                key: item for entry in items for key, item in entry.items()
            },
            "take": lambda item, count: item[:count],
            "string": lambda item: str(item),
            "format": lambda pattern, *items: pattern.format(*items),
        }
        assert name in operations, f"Unsupported or cloud ARM function: {name}"
        return operations[name](*(resolve(argument) for argument in arguments))

    parsed = parse()
    assert cursor == len(tokens)
    return resolve(parsed)


def child_parameters(template: dict[str, Any]) -> dict[str, Any]:
    """Use absent nullable values for an offline child-variable safety probe."""
    return {
        name: None
        if specification.get("nullable")
        else specification.get("defaultValue", "")
        for name, specification in template["parameters"].items()
    } | {
        "deployApplications": False,
        "semanticCacheEnabled": False,
        "productionEvaluatorMode": "EXACT_REFERENCE",
        "judgeTimeoutSeconds": 30,
        "tags": {},
    }


@pytest.mark.parametrize("semantic_cache_enabled", [False, True])
@pytest.mark.parametrize("evaluator_mode", ["EXACT_REFERENCE", "LLM_JUDGE"])
def test_disabled_child_variables_accept_absent_runtime_inputs(
    compiled_foundation: dict[str, Any],
    semantic_cache_enabled: bool,
    evaluator_mode: str,
) -> None:
    """Even eager variable evaluation must not need null runtime input values."""
    child = compiled_foundation["resources"]["containerApps"]["properties"]["template"]
    parameters = child_parameters(child) | {
        "semanticCacheEnabled": semantic_cache_enabled,
        "productionEvaluatorMode": evaluator_mode,
    }
    evaluated = {
        name: evaluate_arm(value, parameters, child["variables"])
        for name, value in child["variables"].items()
    }
    assert evaluated["validatedDeployApplications"] is False
    assert evaluated["runtimeInputsAreComplete"] is False
    assert evaluated["semanticCacheEnvironment"] == []
    for name in ("api", "ui", "deploymentSmokeJob"):
        identities = child["resources"][name]["identity"]["userAssignedIdentities"]
        assert evaluate_arm(identities, parameters, child["variables"]) == {}


@pytest.mark.parametrize("parameter", RUNTIME_OUTPUT_PARAMETERS)
def test_nullable_runtime_inputs_remain_required_when_applications_enabled(
    compiled_foundation: dict[str, Any], parameter: str
) -> None:
    """Nullability supports foundation only, not incomplete enabled applications."""
    child = compiled_foundation["resources"]["containerApps"]["properties"]["template"]
    specification = child["parameters"][parameter]
    assert specification["nullable"] is True
    assert specification["type"] == (
        "securestring"
        if parameter == "applicationInsightsConnectionString"
        else "string"
    )
    parameters = child_parameters(child) | dict.fromkeys(
        RUNTIME_OUTPUT_PARAMETERS, "offline-test-input"
    )
    parameters.update(deployApplications=True, semanticCacheEnabled=True)
    parameters[parameter] = None
    with pytest.raises(ValueError, match="Enabled applications require complete"):
        evaluate_arm(
            child["variables"]["validatedDeployApplications"],
            parameters,
            child["variables"],
        )


@pytest.mark.parametrize("semantic_cache_enabled", [False, True])
def test_enabled_parent_branches_preserve_real_module_output_expressions(
    compiled_foundation: dict[str, Any], semantic_cache_enabled: bool
) -> None:
    """Compare enabled wiring with actual output expressions, not dummy resources."""
    parameters = compiled_foundation["resources"]["containerApps"]["properties"][
        "parameters"
    ]
    expected = {
        "apiIdentityClientId": "reference('identities').outputs.apiClientId.value",
        "apiIdentityResourceId": "reference('identities').outputs.apiResourceId.value",
        "apiImage": (
            "format('{0}/optima-api@{1}', "
            "reference('registry').outputs.loginServer.value, "
            "variables('validatedApiImageDigest'))"
        ),
        "applicationInsightsConnectionString": (
            "listOutputsWithSecureValues('monitoring', '2025-04-01').connectionString"
        ),
        "cosmosContainerName": "reference('cosmos').outputs.containerName.value",
        "cosmosDatabaseName": "reference('cosmos').outputs.databaseName.value",
        "cosmosEndpoint": "reference('cosmos').outputs.endpoint.value",
        "registryLoginServer": "reference('registry').outputs.loginServer.value",
        "uiIdentityResourceId": "reference('identities').outputs.uiResourceId.value",
        "uiImage": (
            "format('{0}/optima-ui@{1}', "
            "reference('registry').outputs.loginServer.value, "
            "variables('validatedUiImageDigest'))"
        ),
    }
    for name, expression in expected.items():
        assert parameters[name] == (
            "[if(parameters('deployContainerApps'), "
            f"createObject('value', {expression}), createObject('value', null()))]"
        )
    for name, expression in {
        "apiIdentityPrincipalId": (
            "reference('identities').outputs.apiPrincipalId.value"
        ),
        "redisHost": "reference('redis').outputs.hostName.value",
        "redisIndexName": "'optima-cache-v1'",
    }.items():
        assert parameters[name] == (
            "[if(parameters('deployContainerApps'), "
            "if(variables('validatedSemanticCacheEnabled'), "
            f"createObject('value', {expression}), createObject('value', null())), "
            "createObject('value', null()))]"
        )
        if not semantic_cache_enabled:
            assert evaluate_arm(
                parameters[name],
                {"deployContainerApps": True},
                {"validatedSemanticCacheEnabled": False},
            ) == {"value": None}


@pytest.mark.parametrize("semantic_cache_enabled", [False, True])
def test_disabled_applications_do_not_evaluate_any_parent_output_lookup(
    compiled_foundation: dict[str, Any], semantic_cache_enabled: bool
) -> None:
    """Evaluate actual compiled guards with unresolved module outputs prohibited."""
    parameters = compiled_foundation["resources"]["containerApps"]["properties"][
        "parameters"
    ]
    output_parameters = {
        name: expression
        for name, expression in parameters.items()
        if re.search(
            r"reference\(|listOutputsWithSecureValues\(", json.dumps(expression)
        )
    }
    assert set(output_parameters) == set(RUNTIME_OUTPUT_PARAMETERS) | {"redisHost"}
    for expression in output_parameters.values():
        assert evaluate_arm(
            expression,
            {"deployContainerApps": False},
            {"validatedSemanticCacheEnabled": semantic_cache_enabled},
        ) == {"value": None}


@pytest.mark.parametrize("deploy_applications", [False, True])
def test_disabled_cache_still_rejects_contradictory_child_inputs(
    compiled_foundation: dict[str, Any], deploy_applications: bool
) -> None:
    """Do not relax disabled-cache validation while making runtime inputs optional."""
    child = compiled_foundation["resources"]["containerApps"]["properties"]["template"]
    parameters = child_parameters(child) | {
        "deployApplications": deploy_applications,
        "redisHost": "unexpected-cache-input",
    }
    with pytest.raises(ValueError, match="Disabled semantic cache rejects"):
        evaluate_arm(
            child["variables"]["validatedSemanticCacheEnabled"],
            parameters,
            child["variables"],
        )


@pytest.mark.parametrize("semantic_cache_enabled", [False, True])
def test_enabled_applications_keep_all_four_resources_and_identity_wiring(
    compiled_foundation: dict[str, Any], semantic_cache_enabled: bool
) -> None:
    """Exercise local composition only; these test values are never deployed."""
    child = compiled_foundation["resources"]["containerApps"]["properties"]["template"]
    parameters = child_parameters(child) | dict.fromkeys(
        RUNTIME_OUTPUT_PARAMETERS, "offline-test-input"
    )
    parameters.update(
        deployApplications=True, semanticCacheEnabled=semantic_cache_enabled
    )
    if semantic_cache_enabled:
        parameters.update(
            redisHost="cache.example.test",
            redisIndexName="optima-cache-v1",
            redisEmbeddingDeployment="test-embedding",
            redisEmbeddingModel="test-embedding-model",
            redisEmbeddingDimension=1536,
            pricingEmbeddingInputRatePerMillionTokens="0.02",
        )
    else:
        parameters["apiIdentityPrincipalId"] = None
    cache_environment = evaluate_arm(
        child["variables"]["semanticCacheEnvironment"], parameters, child["variables"]
    )
    if semantic_cache_enabled:
        assert {entry["name"]: entry["value"] for entry in cache_environment} == {
            "OPTIMA_REDIS_HOST": parameters["redisHost"],
            "OPTIMA_REDIS_INDEX_NAME": parameters["redisIndexName"],
            "OPTIMA_REDIS_EMBEDDING_DIMENSION": "1536",
            "OPTIMA_REDIS_EMBEDDING_MODEL": parameters["redisEmbeddingModel"],
            "OPTIMA_REDIS_EMBEDDING_DEPLOYMENT": parameters["redisEmbeddingDeployment"],
            "OPTIMA_REDIS_AUTH_MODE": "MANAGED_IDENTITY",
            "OPTIMA_REDIS_OBJECT_ID": parameters["apiIdentityPrincipalId"],
            "OPTIMA_REDIS_MANAGED_IDENTITY_CLIENT_ID": parameters[
                "apiIdentityClientId"
            ],
            "OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS": "0.02",
        }
    else:
        assert cache_environment == []
    for name in ("api", "ui", "uiAuthentication", "deploymentSmokeJob"):
        resource = child["resources"][name]
        assert (
            evaluate_arm(resource["condition"], parameters, child["variables"]) is True
        )
    for name, parameter in (
        ("api", "apiIdentityResourceId"),
        ("ui", "uiIdentityResourceId"),
        ("deploymentSmokeJob", "uiIdentityResourceId"),
    ):
        resource = child["resources"][name]
        assert evaluate_arm(
            resource["identity"]["userAssignedIdentities"],
            parameters,
            child["variables"],
        ) == {parameters[parameter]: {}}
        assert resource["properties"]["configuration"]["registries"] == [
            {
                "identity": f"[parameters('{parameter}')]",
                "server": "[parameters('registryLoginServer')]",
            }
        ]
    assert child["resources"]["api"]["properties"]["configuration"]["secrets"] == [
        {
            "name": "application-insights-connection-string",
            "value": "[parameters('applicationInsightsConnectionString')]",
        }
    ]


@pytest.mark.parametrize("deploy_applications", [False, True])
@pytest.mark.parametrize("semantic_cache_enabled", [False, True])
def test_environment_disables_platform_log_storage_for_all_execution_modes(
    compiled_foundation: dict[str, Any],
    deploy_applications: bool,
    semantic_cache_enabled: bool,
) -> None:
    """Use the CLI's null destination, not its unsupported literal 'none' option."""
    child = compiled_foundation["resources"]["containerApps"]["properties"]["template"]
    environment = child["resources"]["managedEnvironment"]
    parameters = child_parameters(child) | {
        "deployApplications": deploy_applications,
        "semanticCacheEnabled": semantic_cache_enabled,
    }
    assert "condition" not in environment
    assert environment["apiVersion"] == "2025-07-01"
    assert evaluate_arm(
        environment["properties"]["appLogsConfiguration"],
        parameters,
        child["variables"],
    ) == {"destination": None}


def test_compiled_foundation_retains_exact_nine_resource_graph(
    compiled_foundation: dict[str, Any],
) -> None:
    """Keep foundation resources and conditions without claiming a live ARM preview."""
    parameters = compiled_foundation["parameters"]
    assert parameters["location"]["allowedValues"] == ["eastus2"]
    assert parameters["location"]["defaultValue"] == "eastus2"
    for flag in ("deployContainerApps", "exposePublicUi", "deployRuntimeAccess"):
        assert parameters[flag]["defaultValue"] is False
    resources = compiled_foundation["resources"]
    assert set(resources) == {
        "identities",
        "registry",
        "monitoring",
        "cosmos",
        "redis",
        "runtimeAccess",
        "containerApps",
    }
    assert (
        resources["redis"]["condition"]
        == "[variables('validatedSemanticCacheEnabled')]"
    )
    assert (
        resources["runtimeAccess"]["condition"] == "[parameters('deployRuntimeAccess')]"
    )
    types: list[str] = []
    for name in ("identities", "registry", "monitoring", "cosmos", "containerApps"):
        module = resources[name]
        assert "condition" not in module
        child = module["properties"]["template"]
        nested = child["resources"]
        for resource in nested.values() if isinstance(nested, dict) else nested:
            if "condition" in resource:
                assert name == "containerApps"
                assert (
                    resource["condition"]
                    == "[variables('validatedDeployApplications')]"
                )
                assert (
                    evaluate_arm(
                        resource["condition"],
                        child_parameters(child),
                        child["variables"],
                    )
                    is False
                )
            else:
                types.append(resource["type"])
    assert Counter(types) == Counter(
        {
            "Microsoft.ManagedIdentity/userAssignedIdentities": 2,
            "Microsoft.ContainerRegistry/registries": 1,
            "Microsoft.OperationalInsights/workspaces": 1,
            "Microsoft.Insights/components": 1,
            "Microsoft.DocumentDB/databaseAccounts": 1,
            "Microsoft.DocumentDB/databaseAccounts/sqlDatabases": 1,
            "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers": 1,
            "Microsoft.App/managedEnvironments": 1,
        }
    )
    child = resources["containerApps"]["properties"]["template"]
    environment = child["resources"]["managedEnvironment"]
    assert environment["location"] == "[parameters('location')]"
    assert environment["tags"] == "[parameters('tags')]"
    assert evaluate_arm(
        environment["properties"], child_parameters(child), child["variables"]
    ) == {
        "appLogsConfiguration": {"destination": None},
        "publicNetworkAccess": "Enabled",
        "zoneRedundant": False,
    }


def test_pure_expression_probe_is_lazy_and_rejects_unknown_functions() -> None:
    """Do not let the offline helper mask eager failures or unsupported semantics."""
    assert evaluate_arm("[if(false(), fail('unused'), null())]", {}, {}) is None
    with pytest.raises(ValueError, match="required"):
        evaluate_arm("[if(true(), fail('required'), null())]", {}, {})
    with pytest.raises(AssertionError, match="Unsupported or cloud ARM function"):
        evaluate_arm("[reference('unresolved')]", {}, {})
    with pytest.raises(AttributeError):
        evaluate_arm("[trim(null())]", {}, {})
