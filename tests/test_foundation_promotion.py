"""Behavior tests for the offline foundation promotion provenance gate."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.foundation_promotion import (
    FOUNDATION_WORKFLOW_PATH,
    PromotionValidationError,
    main,
    validate_foundation_promotion,
)

REPOSITORY = "sekharrcs/optima"
HEAD_SHA = "a" * 40
PLAN_RUN_ID = 1001
APPLY_RUN_ID = 1002
ACTOR = "plan-reviewer"
DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def _actor(login: str, identifier: int = 77) -> dict[str, Any]:
    return {"id": identifier, "login": login}


def _repository() -> dict[str, Any]:
    return {
        "id": 501,
        "name": "optima",
        "full_name": REPOSITORY,
        "private": False,
        "owner": _actor("sekharrcs", 1),
    }


def _run(
    *,
    run_id: int,
    run_number: int,
    operation: str,
    created_at: str,
    status: str,
    conclusion: str | None,
    actor: str = ACTOR,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "run_number": run_number,
        "run_attempt": 1,
        "name": "Foundation plan and apply",
        "path": FOUNDATION_WORKFLOW_PATH,
        "display_title": f"Foundation {operation} · {HEAD_SHA}",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": HEAD_SHA,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
        "actor": _actor(actor),
        "triggering_actor": _actor(actor),
        "repository": _repository(),
        "head_repository": _repository(),
    }


def _documents() -> dict[str, Any]:
    source_run = _run(
        run_id=PLAN_RUN_ID,
        run_number=41,
        operation="foundation-plan",
        created_at="2026-09-07T11:00:00Z",
        status="completed",
        conclusion="success",
    )
    current_run = _run(
        run_id=APPLY_RUN_ID,
        run_number=42,
        operation="foundation-apply",
        created_at="2026-09-07T11:30:00Z",
        status="in_progress",
        conclusion=None,
        actor="apply-reviewer",
    )
    jobs = {
        "total_count": 3,
        "jobs": [
            {
                "id": 2001,
                "name": "Validate exact foundation source",
                "status": "completed",
                "conclusion": "success",
                "head_sha": HEAD_SHA,
                "run_id": PLAN_RUN_ID,
                "run_attempt": 1,
            },
            {
                "id": 2002,
                "name": "Read-only foundation plan",
                "status": "completed",
                "conclusion": "success",
                "head_sha": HEAD_SHA,
                "run_id": PLAN_RUN_ID,
                "run_attempt": 1,
            },
            {
                "id": 2003,
                "name": "Promoted foundation apply",
                "status": "completed",
                "conclusion": "skipped",
                "head_sha": HEAD_SHA,
                "run_id": PLAN_RUN_ID,
                "run_attempt": 1,
            },
        ],
    }
    artifacts = {
        "total_count": 1,
        "artifacts": [
            {
                "id": 3001,
                "name": f"foundation-plan-evidence-{HEAD_SHA}",
                "size_in_bytes": 8192,
                "expired": False,
                "digest": DIGEST,
                "created_at": "2026-09-07T11:10:00Z",
                "expires_at": "2026-10-07T11:10:00Z",
                "workflow_run": {
                    "id": PLAN_RUN_ID,
                    "repository_id": 501,
                    "head_repository_id": 501,
                    "head_branch": "main",
                    "head_sha": HEAD_SHA,
                },
            }
        ],
    }
    return {
        "source_run": source_run,
        "source_jobs": jobs,
        "source_artifacts": artifacts,
        "recent_workflow_runs": {
            "total_count": 2,
            "workflow_runs": [current_run, copy.deepcopy(source_run)],
        },
    }


def _validate(documents: dict[str, Any], **overrides: Any) -> Any:
    arguments = {
        **documents,
        "expected_repository": REPOSITORY,
        "expected_workflow_path": FOUNDATION_WORKFLOW_PATH,
        "expected_branch": "main",
        "expected_head_sha": HEAD_SHA,
        "expected_plan_run_id": PLAN_RUN_ID,
        "current_apply_run_id": APPLY_RUN_ID,
        "expected_plan_actor": ACTOR,
        "confirmed_artifact_digest": DIGEST,
        "current_time": NOW,
        "maximum_age_seconds": 86400,
    }
    arguments.update(overrides)
    return validate_foundation_promotion(**arguments)


def test_valid_promotion_returns_only_bound_artifact_outputs() -> None:
    outputs = _validate(_documents())

    assert outputs.artifact_id == 3001
    assert outputs.artifact_digest == DIGEST
    assert outputs.source_run_attempt == 1


@pytest.mark.parametrize("conclusion", ["failure", "success"])
def test_diagnostic_artifact_never_qualifies_for_promotion(conclusion: str) -> None:
    """Reject diagnostics even with forged successful source-run metadata."""
    documents = _documents()
    documents["source_run"]["conclusion"] = conclusion
    documents["source_jobs"]["jobs"][1]["conclusion"] = conclusion
    documents["source_artifacts"]["artifacts"][0]["name"] = (
        f"foundation-classification-failure-{PLAN_RUN_ID}-1"
    )
    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", ".github/workflows/deploy-production.yml"),
        ("head_branch", "feature/attacker"),
        ("event", "push"),
        ("display_title", f"Foundation foundation-apply · {HEAD_SHA}"),
        ("display_title", "Foundation plan"),
        ("head_sha", "c" * 40),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("run_attempt", 0),
    ],
)
def test_wrong_source_run_provenance_is_rejected(field: str, value: Any) -> None:
    documents = _documents()
    documents["source_run"][field] = value

    with pytest.raises(PromotionValidationError):
        _validate(documents)


def test_wrong_source_repository_is_rejected() -> None:
    documents = _documents()
    documents["source_run"]["repository"]["full_name"] = "attacker/optima"

    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize("field", ["actor", "triggering_actor"])
def test_wrong_source_actor_is_rejected(field: str) -> None:
    documents = _documents()
    documents["source_run"][field] = _actor("attacker")

    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize(
    ("created_at", "maximum_age"),
    [
        ("2026-09-07T12:00:01Z", 86400),
        ("2026-09-07T10:00:00Z", 3600),
        ("2026-09-06T11:59:59Z", 86400),
    ],
)
def test_future_expired_or_over_24_hour_source_is_rejected(
    created_at: str, maximum_age: int
) -> None:
    documents = _documents()
    documents["source_run"]["created_at"] = created_at
    documents["recent_workflow_runs"]["workflow_runs"][1]["created_at"] = created_at

    with pytest.raises(PromotionValidationError):
        _validate(documents, maximum_age_seconds=maximum_age)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("conclusion", "failure"),
        ("status", "in_progress"),
        ("head_sha", "c" * 40),
        ("run_attempt", 2),
    ],
)
def test_wrong_plan_job_outcome_head_or_attempt_is_rejected(
    field: str, value: Any
) -> None:
    documents = _documents()
    documents["source_jobs"]["jobs"][1][field] = value

    with pytest.raises(PromotionValidationError):
        _validate(documents)


def test_successful_apply_job_in_source_run_is_rejected() -> None:
    documents = _documents()
    documents["source_jobs"]["jobs"][2]["conclusion"] = "success"

    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize("run_number", [43, 41.5])
def test_superseded_or_malformed_intervening_run_is_rejected(
    run_number: int | float,
) -> None:
    documents = _documents()
    intervening = _run(
        run_id=1003,
        run_number=43,
        operation="foundation-plan",
        created_at="2026-09-07T11:45:00Z",
        status="completed",
        conclusion="success",
    )
    intervening["run_number"] = run_number
    documents["recent_workflow_runs"]["workflow_runs"].insert(0, intervening)
    documents["recent_workflow_runs"]["total_count"] = 3

    with pytest.raises(PromotionValidationError):
        _validate(documents)


def test_run_between_source_plan_and_current_apply_is_rejected() -> None:
    documents = _documents()
    documents["recent_workflow_runs"]["workflow_runs"][0]["run_number"] = 43
    intervening = _run(
        run_id=1003,
        run_number=42,
        operation="foundation-plan",
        created_at="2026-09-07T11:15:00Z",
        status="completed",
        conclusion="failure",
    )
    documents["recent_workflow_runs"]["workflow_runs"].insert(1, intervening)
    documents["recent_workflow_runs"]["total_count"] = 3

    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize("mode", ["missing", "wrong_id", "retry"])
def test_wrong_missing_or_retried_current_apply_is_rejected(mode: str) -> None:
    documents = _documents()
    current = documents["recent_workflow_runs"]["workflow_runs"][0]
    if mode == "missing":
        documents["recent_workflow_runs"]["workflow_runs"].pop(0)
        documents["recent_workflow_runs"]["total_count"] = 1
    elif mode == "wrong_id":
        current["id"] = 9999
    else:
        current["run_attempt"] = 2

    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "fabricated-evidence"),
        ("expired", True),
        ("expires_at", "2026-09-07T12:00:00Z"),
        ("digest", "sha256:" + "c" * 64),
        ("id", "3001"),
        ("size_in_bytes", 0),
        ("size_in_bytes", 1024 * 1024 + 1),
    ],
)
def test_wrong_expired_mismatched_or_oversized_artifact_is_rejected(
    field: str, value: Any
) -> None:
    documents = _documents()
    documents["source_artifacts"]["artifacts"][0][field] = value

    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize("mode", ["missing", "multiple", "extra"])
def test_missing_multiple_or_extra_artifacts_are_rejected(mode: str) -> None:
    documents = _documents()
    artifacts = documents["source_artifacts"]["artifacts"]
    if mode == "missing":
        artifacts.clear()
        documents["source_artifacts"]["total_count"] = 0
    else:
        extra = copy.deepcopy(artifacts[0])
        extra["id"] = 3002
        if mode == "extra":
            extra["name"] = "unreviewed-file"
        artifacts.append(extra)
        documents["source_artifacts"]["total_count"] = 2

    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 9999),
        ("repository_id", 9999),
        ("head_repository_id", 9999),
        ("head_branch", "attacker"),
        ("head_sha", "c" * 40),
    ],
)
def test_fabricated_artifact_run_metadata_is_rejected(field: str, value: Any) -> None:
    documents = _documents()
    workflow_run = documents["source_artifacts"]["artifacts"][0]["workflow_run"]
    workflow_run[field] = value

    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize(
    "location",
    ["source_run", "source_jobs", "source_artifacts", "recent_workflow_runs"],
)
def test_unknown_closed_schema_fields_are_rejected(location: str) -> None:
    documents = _documents()
    documents[location]["unexpected"] = True

    with pytest.raises(PromotionValidationError):
        _validate(documents)


@pytest.mark.parametrize("location", ["actor", "job", "artifact", "artifact_run"])
def test_unknown_nested_security_fields_are_rejected(location: str) -> None:
    documents = _documents()
    if location == "actor":
        target = documents["source_run"]["actor"]
    elif location == "job":
        target = documents["source_jobs"]["jobs"][1]
    elif location == "artifact":
        target = documents["source_artifacts"]["artifacts"][0]
    else:
        target = documents["source_artifacts"]["artifacts"][0]["workflow_run"]
    target["unexpected"] = True

    with pytest.raises(PromotionValidationError):
        _validate(documents)


def test_missing_security_field_is_rejected() -> None:
    documents = _documents()
    del documents["source_run"]["triggering_actor"]

    with pytest.raises(PromotionValidationError):
        _validate(documents)


def _write_cli_documents(tmp_path: Path, documents: dict[str, Any]) -> list[str]:
    arguments: list[str] = []
    for key, option in (
        ("source_run", "--source-run-json"),
        ("source_jobs", "--source-jobs-json"),
        ("source_artifacts", "--source-artifacts-json"),
        ("recent_workflow_runs", "--recent-runs-json"),
    ):
        path = tmp_path / f"{key}.json"
        path.write_text(json.dumps(documents[key]), encoding="utf-8")
        arguments.extend((option, str(path)))
    return arguments


def _cli_arguments(tmp_path: Path, documents: dict[str, Any]) -> list[str]:
    return [
        *_write_cli_documents(tmp_path, documents),
        "--expected-repository",
        REPOSITORY,
        "--expected-workflow-path",
        FOUNDATION_WORKFLOW_PATH,
        "--expected-branch",
        "main",
        "--expected-head-sha",
        HEAD_SHA,
        "--expected-plan-run-id",
        str(PLAN_RUN_ID),
        "--current-apply-run-id",
        str(APPLY_RUN_ID),
        "--expected-plan-actor",
        ACTOR,
        "--confirmed-artifact-digest",
        DIGEST,
        "--current-time",
        "2026-09-07T12:00:00Z",
        "--maximum-age-seconds",
        "86400",
        "--github-output",
        str(tmp_path / "github-output.txt"),
    ]


def test_cli_writes_only_strict_numeric_and_digest_outputs(tmp_path: Path) -> None:
    output = tmp_path / "github-output.txt"

    assert main(_cli_arguments(tmp_path, _documents())) == 0
    assert output.read_text(encoding="utf-8") == (
        f"artifact_id=3001\nartifact_digest={DIGEST}\nsource_run_attempt=1\n"
    )


@pytest.mark.parametrize(
    "invalid_json",
    [
        "{",
        '{"id": 1001, "id": 1002}',
        '{"value": NaN}',
        '{"value": Infinity}',
    ],
)
def test_cli_rejects_malformed_duplicate_or_nonfinite_json_without_outputs(
    tmp_path: Path, invalid_json: str
) -> None:
    arguments = _cli_arguments(tmp_path, _documents())
    source_path = tmp_path / "source_run.json"
    source_path.write_text(invalid_json, encoding="utf-8")

    assert main(arguments) == 1
    assert not (tmp_path / "github-output.txt").exists()


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("expected_repository", "attacker/optima"),
        ("expected_workflow_path", ".github/workflows/other.yml"),
        ("expected_branch", "feature/attacker"),
        ("expected_head_sha", "c" * 40),
        ("expected_plan_run_id", 9999),
        ("current_apply_run_id", 9999),
        ("expected_plan_actor", "attacker"),
        ("confirmed_artifact_digest", "sha256:" + "c" * 64),
    ],
)
def test_wrong_trusted_confirmation_is_rejected(argument: str, value: Any) -> None:
    with pytest.raises(PromotionValidationError):
        _validate(_documents(), **{argument: value})
