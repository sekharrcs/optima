"""Offline workflow command bindings and synthetic external-policy promotion."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from test_external_observation_policy import (
    GROUP,
    SUBSCRIPTION,
    converged_document,
    foundation_document,
    policy_document,
)
from test_foundation_workflow import ROOT, _job, _load_workflow, _steps
from test_foundation_workflow import capture_bash as capture_bash

POLICY_ENV = "OPTIMA_FOUNDATION_EXTERNAL_OBSERVATION_POLICY"
PLAN_PATH = "$RUNNER_TEMP/plan-evidence/foundation-plan-evidence.json"
PARAMETER_STEP = "Write the effective foundation parameter set"
CLASSIFY_STEP = "Classify the foundation what-if and emit sanitized evidence"
FRESH_STEP = "Run a fresh authoritative foundation what-if and reclassify"
PROMOTE_STEP = "Refuse promotion unless the fresh plan matches the approved plan"
DEPLOY_STEP = "Deploy only the Azure foundation"
RECONCILE_STEP = "Reconcile the exact foundation deployment"
CONVERGE_STEP = "Verify foundation convergence and stop"


def _step(job: str, name: str) -> dict[str, Any]:
    return next(step for step in _steps(job) if step.get("name") == name)


def _arguments(commands: str, prefix: str) -> list[str]:
    matches = [
        shlex.split(line)
        for line in commands.replace("\\\n", "").splitlines()
        if line.strip().startswith(prefix)
    ]
    assert len(matches) == 1
    return matches[0]


def test_policy_is_captured_once_per_protected_job_and_never_rewritten() -> None:
    workflow = _load_workflow()
    assert POLICY_ENV not in workflow.get("env", {})
    assert POLICY_ENV not in json.dumps(_job("validate"))
    for job_name in ("foundation-plan", "foundation-apply"):
        job = _job(job_name)
        assert job["environment"] == "hackathon"
        assert job["env"][POLICY_ENV] == "${{ vars." + POLICY_ENV + " }}"
        assert json.dumps(job).count("${{ vars." + POLICY_ENV + " }}") == 1
        for step in job["steps"]:
            assert POLICY_ENV not in step.get("env", {})
            commands = step.get("run", "")
            assert "GITHUB_ENV" not in commands
            for line in commands.splitlines():
                if POLICY_ENV in line:
                    assert line.strip() == f"--external-policy-env {POLICY_ENV} \\"


@pytest.mark.parametrize(
    ("job", "name", "whatif", "output"),
    [
        ("foundation-plan", CLASSIFY_STEP, "foundation-whatif", "foundation-plan"),
        ("foundation-apply", FRESH_STEP, "foundation-whatif", "foundation-apply"),
        (
            "foundation-apply",
            CONVERGE_STEP,
            "foundation-converged-whatif",
            "foundation-converged",
        ),
    ],
)
def test_every_classifier_binds_mode_policy_and_exact_paths(
    job: str, name: str, whatif: str, output: str
) -> None:
    step = _step(job, name)
    arguments = _arguments(
        step["run"], "python scripts/whatif_classification.py classify"
    )
    assert arguments[:3] == ["python", "scripts/whatif_classification.py", "classify"]
    options = arguments[3:]
    expected = {
        "--deployment-mode": "Incremental",
        "--external-policy-env": POLICY_ENV,
        "--whatif": f"$RUNNER_TEMP/{whatif}.json",
        "--subscription-id": "$AZURE_SUBSCRIPTION_ID",
        "--resource-group": "$AZURE_RESOURCE_GROUP",
        "--commit-sha": "$GITHUB_SHA",
        "--parameters-file": "$RUNNER_TEMP/foundation-parameters.txt",
        "--output": f"$RUNNER_TEMP/{output}-evidence.json",
    }
    if job == "foundation-plan":
        expected["--failure-diagnostics"] = (
            "$RUNNER_TEMP/foundation-classification-failure.json"
        )
    assert len(options) == 2 * len(expected)
    assert dict(zip(options[::2], options[1::2], strict=True)) == expected
    assert step["run"].startswith("set -euo pipefail\n")
    assert "continue-on-error" not in step


@pytest.mark.parametrize(
    ("job", "name", "operation", "deployment", "provenance"),
    [
        (
            "foundation-plan",
            "Run exactly one authoritative foundation what-if",
            "what-if",
            "optima-foundation-promotion-whatif",
            "$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT",
        ),
        (
            "foundation-apply",
            FRESH_STEP,
            "what-if",
            "optima-foundation-promotion-whatif",
            "$PLAN_RUN_ID-$SOURCE_RUN_ATTEMPT",
        ),
        (
            "foundation-apply",
            DEPLOY_STEP,
            "create",
            "$deployment_name",
            "$PLAN_RUN_ID-$SOURCE_RUN_ATTEMPT",
        ),
        (
            "foundation-apply",
            CONVERGE_STEP,
            "what-if",
            "optima-foundation-converged-whatif",
            "$PLAN_RUN_ID-$SOURCE_RUN_ATTEMPT",
        ),
    ],
)
def test_every_arm_call_binds_incremental_full_payload_and_plan_parameters(
    job: str, name: str, operation: str, deployment: str, provenance: str
) -> None:
    step = _step(job, name)
    arguments = _arguments(step["run"], f"az deployment group {operation}")
    assert arguments[:4] == ["az", "deployment", "group", operation]
    for option, value in (
        ("--mode", "Incremental"),
        ("--name", deployment),
        ("--resource-group", "$AZURE_RESOURCE_GROUP"),
        ("--template-file", "infra/resource-group.bicep"),
    ):
        assert arguments.count(option) == 1
        assert arguments[arguments.index(option) + 1] == value
    first_parameters = arguments.index("--parameters")
    assert arguments.count("--parameters") == 2
    assert arguments[first_parameters : first_parameters + 5] == [
        "--parameters",
        "infra/environments/hackathon.foundation.bicepparam",
        "--parameters",
        "deploymentCommitSha=$GITHUB_SHA",
        f"deploymentWorkflowRunId={provenance}",
    ]
    assert "--exclude-change-types" not in step["run"]
    assert "--query" not in arguments
    assert "|" not in arguments
    if operation == "what-if":
        assert (
            arguments[arguments.index("--result-format") + 1] == "FullResourcePayloads"
        )
        assert arguments[arguments.index("--validation-level") + 1] == "ProviderNoRbac"
        assert "--no-pretty-print" in arguments
    assert "--only-show-errors" in arguments
    if job == "foundation-apply":
        assert step["env"] == {
            "PLAN_RUN_ID": "${{ inputs.plan_run_id }}",
            "SOURCE_RUN_ATTEMPT": "${{ steps.provenance.outputs.source_run_attempt }}",
        }


def test_promotion_and_convergence_use_authenticated_original_plan_before_success() -> (
    None
):
    steps = _steps("foundation-apply")
    promote = _step("foundation-apply", PROMOTE_STEP)
    converge = _step("foundation-apply", CONVERGE_STEP)
    assert _arguments(promote["run"], "python scripts/whatif_classification.py") == [
        "python",
        "scripts/whatif_classification.py",
        "promote-check",
        "--plan",
        PLAN_PATH,
        "--apply",
        "$RUNNER_TEMP/foundation-apply-evidence.json",
    ]
    commands = converge["run"]
    assert _arguments(
        commands, "python scripts/whatif_classification.py convergence-check"
    ) == [
        "python",
        "scripts/whatif_classification.py",
        "convergence-check",
        "--plan",
        PLAN_PATH,
        "--converged",
        "$RUNNER_TEMP/foundation-converged-evidence.json",
    ]
    assert steps.index(_step("foundation-apply", FRESH_STEP)) < steps.index(promote)
    assert steps.index(promote) < steps.index(_step("foundation-apply", DEPLOY_STEP))
    assert steps.index(_step("foundation-apply", RECONCILE_STEP)) < steps.index(
        converge
    )
    assert (
        commands.index("whatif_classification.py classify")
        < commands.index("whatif_classification.py convergence-check")
        < commands.index("printf '## OPTIMA foundation apply")
    )
    assert "||" not in commands
    assert "set +e" not in commands
    assert "continue-on-error" not in converge
    assert "if" not in converge
    assert (
        _step("foundation-plan", PARAMETER_STEP)["run"]
        == _step("foundation-apply", PARAMETER_STEP)["run"]
    )
    for job, expected_classifications, expected_arm_calls in (
        ("foundation-plan", 1, 1),
        ("foundation-apply", 2, 3),
    ):
        combined = "\n".join(step.get("run", "") for step in _steps(job))
        assert (
            combined.count("whatif_classification.py classify")
            == expected_classifications
        )
        assert combined.count("--mode Incremental") == expected_arm_calls
        assert "--exclude-change-types" not in combined


def _run_policy_block(
    bash: str, commands: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    fake_commands = textwrap.dedent(
        r"""
        az() {
          printf 'az %s\n' "$*" >> "$TEST_COMMAND_LOG"
          case "$*" in
            'group show '* | 'deployment group list '*) return 0 ;;
            'deployment group what-if '*) printf '%s' "$TEST_WHATIF_JSON" ;;
            'deployment group create '*) printf 'created\n' > "$TEST_CREATED" ;;
            'deployment group show '*) printf 'Succeeded\n' ;;
            *) return 97 ;;
          esac
        }
        git() {
          case "$*" in
            'rev-parse HEAD' | 'rev-parse origin/main') printf '%s\n' "$GITHUB_SHA" ;;
                        'fetch --no-tags --prune --depth=1 origin '*)
                            test "$#" -eq 6 || return 98
                            test "$6" = +refs/heads/main:refs/remotes/origin/main ;;
            *) return 98 ;;
          esac
        }
        python() {
          test "$1" = scripts/whatif_classification.py || return 99
          printf 'classifier %s\n' "$2" >> "$TEST_COMMAND_LOG"
                    "$TEST_PYTHON" -I -B -c "$TEST_CLASSIFIER_LAUNCHER" \
                        "$TEST_ROOT" "$TEST_SRC" "$@"
        }
        """
    )
    inherited = os.environ.copy()
    for name in (POLICY_ENV, "BASH_ENV", "ENV"):
        inherited.pop(name, None)
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail"],
        input=(fake_commands + commands).encode("utf-8"),
        cwd=ROOT,
        env=inherited | environment,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "unset",
        "empty",
        "approved",
        "policy-disabled",
        "policy-enabled",
        "external-drift",
        "managed-not-converged",
    ],
)
def test_actual_workflow_policy_gates_with_synthetic_providers(
    tmp_path: Path, capture_bash: str, scenario: str
) -> None:
    plan_enabled = scenario not in {"unset", "empty", "policy-enabled"}
    apply_enabled = scenario not in {"unset", "empty", "policy-disabled"}
    plan_document = foundation_document()
    fresh_document = foundation_document()
    final_document = converged_document()
    if not plan_enabled:
        plan_document["changes"].pop()
    if not apply_enabled:
        fresh_document["changes"].pop()
        final_document["changes"].pop()
    if scenario == "external-drift":
        final_document["changes"][-1]["before"]["properties"]["state"] = "Changed"
    if scenario == "managed-not-converged":
        final_document["changes"][0]["changeType"] = "Create"
    summary = tmp_path / "apply-summary"
    created = tmp_path / "created"
    log = tmp_path / "commands"
    environment = {
        "RUNNER_TEMP": tmp_path.as_posix(),
        "GITHUB_STEP_SUMMARY": (tmp_path / "plan-summary").as_posix(),
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ID": "456",
        "GITHUB_RUN_ATTEMPT": "1",
        "PLAN_RUN_ID": "123",
        "SOURCE_RUN_ATTEMPT": "2",
        "AZURE_LOCATION": "eastus2",
        "AZURE_RESOURCE_GROUP": GROUP,
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "DEPLOYMENT_NAME": "optima-foundation-456-1",
        "DEPLOY_OUTCOME": "success",
        "TEST_ROOT": ROOT.as_posix(),
        "TEST_SRC": (ROOT / "src").as_posix(),
        "TEST_PYTHON": Path(sys.executable).as_posix(),
        "TEST_CLASSIFIER_LAUNCHER": (
            "import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; "
            "from scripts.whatif_classification import main; "
            "raise SystemExit(main(sys.argv[4:]))"
        ),
        "TEST_COMMAND_LOG": log.as_posix(),
        "TEST_CREATED": created.as_posix(),
    }
    if scenario != "unset":
        environment[POLICY_ENV] = json.dumps(policy_document()) if plan_enabled else ""
    (tmp_path / "foundation-whatif.json").write_text(
        json.dumps(plan_document), encoding="utf-8"
    )
    plan_commands = (
        _step("foundation-plan", PARAMETER_STEP)["run"]
        + _step("foundation-plan", CLASSIFY_STEP)["run"]
    )
    result = _run_policy_block(capture_bash, plan_commands, environment)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stderr == b""
    assert result.stdout.startswith(b"Foundation ")
    for value in (SUBSCRIPTION, "synthetic-external", "PreserveCase", "Changed"):
        assert value.encode() not in result.stdout
    assert not (tmp_path / "foundation-whatif.json").exists()
    plan_path = tmp_path / "plan-evidence" / "foundation-plan-evidence.json"
    plan_path.parent.mkdir()
    (tmp_path / "foundation-plan-evidence.json").rename(plan_path)
    original_plan = plan_path.read_bytes()
    if scenario != "unset":
        environment[POLICY_ENV] = json.dumps(policy_document()) if apply_enabled else ""
    environment["GITHUB_STEP_SUMMARY"] = summary.as_posix()
    environment["TEST_WHATIF_JSON"] = json.dumps(fresh_document)
    expected_failure = (
        PROMOTE_STEP
        if scenario.startswith("policy-")
        else CONVERGE_STEP
        if scenario in {"external-drift", "managed-not-converged"}
        else None
    )
    for name in (
        PARAMETER_STEP,
        FRESH_STEP,
        PROMOTE_STEP,
        DEPLOY_STEP,
        RECONCILE_STEP,
        CONVERGE_STEP,
    ):
        if name == CONVERGE_STEP:
            environment["TEST_WHATIF_JSON"] = json.dumps(final_document)
        result = _run_policy_block(
            capture_bash, _step("foundation-apply", name)["run"], environment
        )
        for value in (SUBSCRIPTION, "synthetic-external", "PreserveCase", "Changed"):
            assert value.encode() not in result.stdout + result.stderr
        if name == expected_failure:
            assert result.returncode == 1
            assert result.stderr.startswith(b"WHATIF CLASSIFICATION FAILED:")
            assert not summary.exists()
            break
        assert result.returncode == 0, result.stderr.decode()
        assert result.stderr == b""
    assert plan_path.read_bytes() == original_plan
    commands = log.read_text(encoding="utf-8").splitlines()
    create_calls = [
        command
        for command in commands
        if command.startswith("az deployment group create ")
    ]
    if scenario.startswith("policy-"):
        assert not created.exists()
        assert create_calls == []
        assert commands[-1] == "classifier promote-check"
        assert "classifier convergence-check" not in commands
    else:
        assert created.read_text().strip() == "created"
        assert len(create_calls) == 1
        assert commands[-1] == "classifier convergence-check"
        final_evidence = json.loads(
            (tmp_path / "foundation-converged-evidence.json").read_text()
        )
        assert (
            final_evidence["schema_version"] == "optima-foundation-whatif-evidence-v2"
        )
        assert len(final_evidence["changes"]["resources"]) == 9
        assert len(final_evidence["external_observations"]) == int(apply_enabled)
        if expected_failure is None:
            assert final_evidence["changes"]["counts"] == {"Create": 0, "NoChange": 9}
            assert final_evidence["external_policy"]["definition"] == (
                policy_document() if apply_enabled else None
            )
            assert "Provisioning state: `Succeeded`" in summary.read_text()
    for value in (SUBSCRIPTION, "synthetic-external", "PreserveCase", "Changed"):
        assert value.encode() not in result.stdout + result.stderr
