"""Validate GitHub Actions foundation-plan provenance without network access."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

EXIT_SUCCESS = 0
EXIT_FAILURE = 1

FOUNDATION_WORKFLOW_PATH = ".github/workflows/foundation.yml"
FOUNDATION_BRANCH = "main"
PLAN_JOB_NAME = "Read-only foundation plan"
APPLY_JOB_NAME = "Promoted foundation apply"
MAXIMUM_PLAN_AGE_SECONDS = 24 * 60 * 60
MAXIMUM_ARTIFACT_SIZE_BYTES = 1024 * 1024

_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_ACTOR = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z"
)

_ACTOR_FIELDS = frozenset(
    {
        "avatar_url",
        "events_url",
        "followers_url",
        "following_url",
        "gists_url",
        "gravatar_id",
        "html_url",
        "id",
        "login",
        "node_id",
        "organizations_url",
        "received_events_url",
        "repos_url",
        "site_admin",
        "starred_url",
        "subscriptions_url",
        "type",
        "url",
        "user_view_type",
    }
)
_REPOSITORY_FIELDS = frozenset(
    {
        "allow_auto_merge",
        "allow_forking",
        "allow_merge_commit",
        "allow_rebase_merge",
        "allow_squash_merge",
        "allow_update_branch",
        "anonymous_access_enabled",
        "archive_url",
        "archived",
        "assignees_url",
        "blobs_url",
        "branches_url",
        "clone_url",
        "code_of_conduct",
        "collaborators_url",
        "comments_url",
        "commits_url",
        "compare_url",
        "contents_url",
        "contributors_url",
        "created_at",
        "custom_properties",
        "default_branch",
        "delete_branch_on_merge",
        "deployments_url",
        "description",
        "disabled",
        "downloads_url",
        "events_url",
        "fork",
        "forks",
        "forks_count",
        "forks_url",
        "full_name",
        "git_commits_url",
        "git_refs_url",
        "git_tags_url",
        "git_url",
        "has_discussions",
        "has_downloads",
        "has_issues",
        "has_pages",
        "has_projects",
        "has_wiki",
        "homepage",
        "hooks_url",
        "html_url",
        "id",
        "is_template",
        "issue_comment_url",
        "issue_events_url",
        "issues_url",
        "keys_url",
        "labels_url",
        "language",
        "languages_url",
        "license",
        "merges_url",
        "milestones_url",
        "mirror_url",
        "name",
        "network_count",
        "node_id",
        "notifications_url",
        "open_issues",
        "open_issues_count",
        "organization",
        "owner",
        "permissions",
        "private",
        "pulls_url",
        "pushed_at",
        "releases_url",
        "security_and_analysis",
        "size",
        "ssh_url",
        "stargazers_count",
        "stargazers_url",
        "statuses_url",
        "subscribers_count",
        "subscribers_url",
        "subscription_url",
        "svn_url",
        "tags_url",
        "teams_url",
        "temp_clone_token",
        "topics",
        "trees_url",
        "updated_at",
        "url",
        "use_squash_pr_title_as_default",
        "visibility",
        "watchers",
        "watchers_count",
        "web_commit_signoff_required",
    }
)
_RUN_FIELDS = frozenset(
    {
        "actor",
        "artifacts_url",
        "cancel_url",
        "check_suite_id",
        "check_suite_node_id",
        "check_suite_url",
        "conclusion",
        "created_at",
        "display_title",
        "event",
        "head_branch",
        "head_commit",
        "head_repository",
        "head_sha",
        "html_url",
        "id",
        "jobs_url",
        "logs_url",
        "name",
        "node_id",
        "path",
        "previous_attempt_url",
        "pull_requests",
        "referenced_workflows",
        "repository",
        "rerun_url",
        "run_attempt",
        "run_number",
        "run_started_at",
        "status",
        "triggering_actor",
        "updated_at",
        "url",
        "workflow_id",
        "workflow_url",
    }
)
_JOB_FIELDS = frozenset(
    {
        "check_run_url",
        "completed_at",
        "conclusion",
        "created_at",
        "head_branch",
        "head_sha",
        "html_url",
        "id",
        "labels",
        "name",
        "node_id",
        "run_attempt",
        "run_id",
        "run_url",
        "runner_group_id",
        "runner_group_name",
        "runner_id",
        "runner_name",
        "started_at",
        "status",
        "steps",
        "url",
        "workflow_name",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "archive_download_url",
        "created_at",
        "digest",
        "expired",
        "expires_at",
        "id",
        "name",
        "node_id",
        "size_in_bytes",
        "updated_at",
        "url",
        "workflow_run",
    }
)
_ARTIFACT_RUN_FIELDS = frozenset(
    {"head_branch", "head_repository_id", "head_sha", "id", "repository_id"}
)


class PromotionValidationError(RuntimeError):
    """Plan-run metadata did not prove an authorized promotion."""


@dataclass(frozen=True)
class PromotionOutputs:
    """Strict values safe to append to the GitHub output command file."""

    artifact_id: int
    artifact_digest: str
    source_run_attempt: int


@dataclass(frozen=True)
class RunIdentity:
    """Security-relevant facts from one workflow-run response."""

    run_id: int
    run_number: int
    run_attempt: int
    repository_id: int
    repository: str
    workflow_path: str
    branch: str
    head_sha: str
    event: str
    display_title: str
    status: str
    conclusion: str | None
    created_at: datetime
    actor: str
    triggering_actor: str
    head_repository_id: int | None


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant {value}")


def load_strict_json(path: Path) -> Any:
    """Read strict JSON, rejecting duplicate keys and non-finite numbers."""
    try:
        content = path.read_text(encoding="utf-8")
        return json.loads(
            content,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
            parse_float=Decimal,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise PromotionValidationError(f"{path.name} is not strict JSON") from error


def _closed_object(
    value: Any,
    *,
    required: set[str] | frozenset[str],
    allowed: set[str] | frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PromotionValidationError(f"{label} must be a JSON object")
    keys = set(value)
    if not required <= keys or keys - allowed:
        raise PromotionValidationError(f"{label} does not match its closed schema")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PromotionValidationError(f"{label} must be a positive integer")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PromotionValidationError(f"{label} must be a canonical string")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    text = _required_string(value, label)
    if _UTC_TIMESTAMP.fullmatch(text) is None:
        raise PromotionValidationError(f"{label} must be a UTC RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise PromotionValidationError(
            f"{label} must be a UTC RFC 3339 timestamp"
        ) from error
    return parsed.astimezone(UTC)


def _actor_login(value: Any, label: str) -> str:
    actor = _closed_object(
        value,
        required={"id", "login"},
        allowed=_ACTOR_FIELDS,
        label=label,
    )
    _positive_integer(actor["id"], f"{label}.id")
    login = _required_string(actor["login"], f"{label}.login")
    if _ACTOR.fullmatch(login) is None:
        raise PromotionValidationError(f"{label}.login is malformed")
    return login


def _repository_identity(value: Any, label: str) -> tuple[int, str]:
    repository = _closed_object(
        value,
        required={"full_name", "id", "name", "owner", "private"},
        allowed=_REPOSITORY_FIELDS,
        label=label,
    )
    repository_id = _positive_integer(repository["id"], f"{label}.id")
    full_name = _required_string(repository["full_name"], f"{label}.full_name")
    name = _required_string(repository["name"], f"{label}.name")
    if _REPOSITORY.fullmatch(full_name) is None or full_name.rsplit("/", 1)[1] != name:
        raise PromotionValidationError(f"{label} identity is malformed")
    if not isinstance(repository["private"], bool):
        raise PromotionValidationError(f"{label}.private must be a boolean")
    _actor_login(repository["owner"], f"{label}.owner")
    return repository_id, full_name


def _run_identity(value: Any, label: str) -> RunIdentity:
    required = {
        "actor",
        "conclusion",
        "created_at",
        "display_title",
        "event",
        "head_branch",
        "head_sha",
        "id",
        "path",
        "repository",
        "run_attempt",
        "run_number",
        "status",
        "triggering_actor",
    }
    run = _closed_object(value, required=required, allowed=_RUN_FIELDS, label=label)
    repository_id, repository = _repository_identity(
        run["repository"], f"{label}.repository"
    )
    head_repository_id: int | None = None
    if run.get("head_repository") is not None:
        head_repository_id, head_repository = _repository_identity(
            run["head_repository"], f"{label}.head_repository"
        )
        if head_repository != repository:
            raise PromotionValidationError(
                f"{label}.head_repository does not match the source repository"
            )
    conclusion = run["conclusion"]
    if conclusion is not None and not isinstance(conclusion, str):
        raise PromotionValidationError(f"{label}.conclusion is malformed")
    return RunIdentity(
        run_id=_positive_integer(run["id"], f"{label}.id"),
        run_number=_positive_integer(run["run_number"], f"{label}.run_number"),
        run_attempt=_positive_integer(run["run_attempt"], f"{label}.run_attempt"),
        repository_id=repository_id,
        repository=repository,
        workflow_path=_required_string(run["path"], f"{label}.path"),
        branch=_required_string(run["head_branch"], f"{label}.head_branch"),
        head_sha=_required_string(run["head_sha"], f"{label}.head_sha"),
        event=_required_string(run["event"], f"{label}.event"),
        display_title=_required_string(run["display_title"], f"{label}.display_title"),
        status=_required_string(run["status"], f"{label}.status"),
        conclusion=conclusion,
        created_at=_timestamp(run["created_at"], f"{label}.created_at"),
        actor=_actor_login(run["actor"], f"{label}.actor"),
        triggering_actor=_actor_login(
            run["triggering_actor"], f"{label}.triggering_actor"
        ),
        head_repository_id=head_repository_id,
    )


def _validate_source_run(
    document: Any,
    *,
    expected_repository: str,
    expected_workflow_path: str,
    expected_branch: str,
    expected_head_sha: str,
    expected_plan_run_id: int,
    expected_plan_actor: str,
    current_time: datetime,
    maximum_age_seconds: int,
) -> RunIdentity:
    source = _run_identity(document, "source run")
    expected_title = f"Foundation foundation-plan · {expected_head_sha}"
    if (
        source.run_id != expected_plan_run_id
        or source.repository != expected_repository
        or source.workflow_path != expected_workflow_path
        or source.branch != expected_branch
        or source.head_sha != expected_head_sha
        or source.event != "workflow_dispatch"
        or source.display_title != expected_title
        or source.status != "completed"
        or source.conclusion != "success"
        or source.actor != expected_plan_actor
        or source.triggering_actor != expected_plan_actor
    ):
        raise PromotionValidationError(
            "source run does not match the confirmed foundation-plan provenance"
        )
    age_seconds = (current_time - source.created_at).total_seconds()
    if age_seconds < 0:
        raise PromotionValidationError("source run is dated in the future")
    if age_seconds > maximum_age_seconds:
        raise PromotionValidationError("source run is too old for promotion")
    return source


def _validate_source_jobs(
    document: Any, *, source: RunIdentity, expected_head_sha: str
) -> None:
    response = _closed_object(
        document,
        required={"jobs", "total_count"},
        allowed={"jobs", "total_count"},
        label="source jobs response",
    )
    jobs = response["jobs"]
    total_count = response["total_count"]
    if (
        not isinstance(jobs, list)
        or not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count != len(jobs)
        or not 1 <= total_count <= 100
    ):
        raise PromotionValidationError("source jobs response is incomplete")
    plan_jobs: list[dict[str, Any]] = []
    for index, value in enumerate(jobs):
        job = _closed_object(
            value,
            required={"conclusion", "id", "name", "status"},
            allowed=_JOB_FIELDS,
            label=f"source job {index}",
        )
        _positive_integer(job["id"], f"source job {index}.id")
        name = _required_string(job["name"], f"source job {index}.name")
        status = _required_string(job["status"], f"source job {index}.status")
        conclusion = job["conclusion"]
        if conclusion is not None and not isinstance(conclusion, str):
            raise PromotionValidationError(
                f"source job {index}.conclusion is malformed"
            )
        if "head_sha" in job and job["head_sha"] != expected_head_sha:
            raise PromotionValidationError("source job head SHA does not match")
        if "run_attempt" in job:
            attempt = _positive_integer(
                job["run_attempt"], f"source job {index}.run_attempt"
            )
            if attempt != source.run_attempt:
                raise PromotionValidationError("source job run attempt does not match")
        if "run_id" in job and job["run_id"] != source.run_id:
            raise PromotionValidationError("source job run ID does not match")
        if name == PLAN_JOB_NAME:
            plan_jobs.append(job)
        if name == APPLY_JOB_NAME and status == "completed" and conclusion == "success":
            raise PromotionValidationError("source run contains a successful apply job")
    if len(plan_jobs) != 1:
        raise PromotionValidationError("source run must contain exactly one plan job")
    plan_job = plan_jobs[0]
    if plan_job["status"] != "completed" or plan_job["conclusion"] != "success":
        raise PromotionValidationError("source plan job did not complete successfully")


def _validate_recent_runs(
    document: Any,
    *,
    source: RunIdentity,
    current_apply_run_id: int,
    expected_repository: str,
    expected_workflow_path: str,
    expected_branch: str,
    expected_head_sha: str,
) -> None:
    response = _closed_object(
        document,
        required={"total_count", "workflow_runs"},
        allowed={"total_count", "workflow_runs"},
        label="recent workflow runs response",
    )
    values = response["workflow_runs"]
    total_count = response["total_count"]
    if (
        not isinstance(values, list)
        or not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < len(values)
        or not 2 <= len(values) <= 100
    ):
        raise PromotionValidationError("recent workflow runs response is incomplete")
    runs = [
        _run_identity(value, f"recent workflow run {index}")
        for index, value in enumerate(values)
    ]
    ordering = [(run.run_number, run.run_attempt) for run in runs]
    if ordering != sorted(ordering, reverse=True):
        raise PromotionValidationError("recent workflow runs are not newest first")
    if len({run.run_id for run in runs}) != len(runs):
        raise PromotionValidationError("recent workflow runs contain duplicate IDs")

    source_matches = [run for run in runs if run.run_id == source.run_id]
    current_matches = [run for run in runs if run.run_id == current_apply_run_id]
    if len(source_matches) != 1 or source_matches[0] != source:
        raise PromotionValidationError("source run is absent or changed in recent runs")
    if len(current_matches) != 1:
        raise PromotionValidationError("current apply run is absent from recent runs")
    current = current_matches[0]
    if (
        current.run_id == source.run_id
        or current.run_number <= source.run_number
        or current.run_attempt != 1
        or current.repository != expected_repository
        or current.workflow_path != expected_workflow_path
        or current.branch != expected_branch
        or current.head_sha != expected_head_sha
        or current.event != "workflow_dispatch"
        or current.display_title != f"Foundation foundation-apply · {expected_head_sha}"
        or current.status not in {"queued", "in_progress"}
        or current.conclusion is not None
    ):
        raise PromotionValidationError("current apply run metadata is invalid")
    if any(
        run.run_number > source.run_number and run.run_id != current_apply_run_id
        for run in runs
    ):
        raise PromotionValidationError(
            "source plan was superseded by an intervening workflow run"
        )


def _validate_artifact(
    document: Any,
    *,
    source: RunIdentity,
    expected_head_sha: str,
    confirmed_artifact_digest: str,
    current_time: datetime,
) -> PromotionOutputs:
    response = _closed_object(
        document,
        required={"artifacts", "total_count"},
        allowed={"artifacts", "total_count"},
        label="source artifacts response",
    )
    artifacts = response["artifacts"]
    if (
        not isinstance(artifacts, list)
        or response["total_count"] != 1
        or len(artifacts) != 1
    ):
        raise PromotionValidationError(
            "source run must contain exactly one plan evidence artifact"
        )
    artifact = _closed_object(
        artifacts[0],
        required={
            "created_at",
            "digest",
            "expired",
            "expires_at",
            "id",
            "name",
            "size_in_bytes",
            "workflow_run",
        },
        allowed=_ARTIFACT_FIELDS,
        label="source artifact",
    )
    artifact_id = _positive_integer(artifact["id"], "source artifact.id")
    size = _positive_integer(artifact["size_in_bytes"], "source artifact.size_in_bytes")
    if size > MAXIMUM_ARTIFACT_SIZE_BYTES:
        raise PromotionValidationError("source artifact exceeds the size limit")
    if artifact["name"] != f"foundation-plan-evidence-{expected_head_sha}":
        raise PromotionValidationError("source artifact name does not match")
    if artifact["expired"] is not False:
        raise PromotionValidationError("source artifact is expired")
    created_at = _timestamp(artifact["created_at"], "source artifact.created_at")
    expires_at = _timestamp(artifact["expires_at"], "source artifact.expires_at")
    if created_at < source.created_at or created_at > current_time:
        raise PromotionValidationError("source artifact creation time is invalid")
    if expires_at <= current_time:
        raise PromotionValidationError("source artifact expiration time is invalid")
    digest = _required_string(artifact["digest"], "source artifact.digest")
    if _DIGEST.fullmatch(digest) is None or digest != confirmed_artifact_digest:
        raise PromotionValidationError("source artifact digest does not match")

    artifact_run = _closed_object(
        artifact["workflow_run"],
        required={"head_branch", "head_sha", "id", "repository_id"},
        allowed=_ARTIFACT_RUN_FIELDS,
        label="source artifact.workflow_run",
    )
    if (
        artifact_run["id"] != source.run_id
        or artifact_run["repository_id"] != source.repository_id
        or artifact_run["head_branch"] != source.branch
        or artifact_run["head_sha"] != expected_head_sha
    ):
        raise PromotionValidationError("source artifact run metadata does not match")
    if "head_repository_id" in artifact_run and artifact_run[
        "head_repository_id"
    ] not in {
        source.repository_id,
        source.head_repository_id,
    }:
        raise PromotionValidationError(
            "source artifact head repository metadata does not match"
        )
    return PromotionOutputs(
        artifact_id=artifact_id,
        artifact_digest=digest,
        source_run_attempt=source.run_attempt,
    )


def validate_foundation_promotion(
    *,
    source_run: Any,
    source_jobs: Any,
    source_artifacts: Any,
    recent_workflow_runs: Any,
    expected_repository: str,
    expected_workflow_path: str,
    expected_branch: str,
    expected_head_sha: str,
    expected_plan_run_id: int,
    current_apply_run_id: int,
    expected_plan_actor: str,
    confirmed_artifact_digest: str,
    current_time: datetime,
    maximum_age_seconds: int,
) -> PromotionOutputs:
    """Validate pre-downloaded REST metadata and return download-safe outputs."""
    if _REPOSITORY.fullmatch(expected_repository) is None:
        raise PromotionValidationError("expected repository is malformed")
    if expected_workflow_path != FOUNDATION_WORKFLOW_PATH:
        raise PromotionValidationError("expected workflow path is not approved")
    if expected_branch != FOUNDATION_BRANCH:
        raise PromotionValidationError("expected branch is not approved")
    if _SHA.fullmatch(expected_head_sha) is None:
        raise PromotionValidationError("expected head SHA is malformed")
    _positive_integer(expected_plan_run_id, "expected plan run ID")
    _positive_integer(current_apply_run_id, "current apply run ID")
    if _ACTOR.fullmatch(expected_plan_actor) is None:
        raise PromotionValidationError("expected plan actor is malformed")
    if _DIGEST.fullmatch(confirmed_artifact_digest) is None:
        raise PromotionValidationError("confirmed artifact digest is malformed")
    if current_time.tzinfo is None or current_time.utcoffset() != UTC.utcoffset(None):
        raise PromotionValidationError("current time must be UTC")
    if not 1 <= maximum_age_seconds <= MAXIMUM_PLAN_AGE_SECONDS:
        raise PromotionValidationError(
            "maximum age must be between 1 and 86400 seconds"
        )

    source = _validate_source_run(
        source_run,
        expected_repository=expected_repository,
        expected_workflow_path=expected_workflow_path,
        expected_branch=expected_branch,
        expected_head_sha=expected_head_sha,
        expected_plan_run_id=expected_plan_run_id,
        expected_plan_actor=expected_plan_actor,
        current_time=current_time,
        maximum_age_seconds=maximum_age_seconds,
    )
    _validate_source_jobs(
        source_jobs, source=source, expected_head_sha=expected_head_sha
    )
    _validate_recent_runs(
        recent_workflow_runs,
        source=source,
        current_apply_run_id=current_apply_run_id,
        expected_repository=expected_repository,
        expected_workflow_path=expected_workflow_path,
        expected_branch=expected_branch,
        expected_head_sha=expected_head_sha,
    )
    return _validate_artifact(
        source_artifacts,
        source=source,
        expected_head_sha=expected_head_sha,
        confirmed_artifact_digest=confirmed_artifact_digest,
        current_time=current_time,
    )


def _positive_decimal_argument(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError("must be a positive decimal integer")
    return int(value)


def create_parser() -> argparse.ArgumentParser:
    """Create the offline foundation promotion parser."""
    parser = argparse.ArgumentParser(
        description="Validate pre-downloaded GitHub foundation plan metadata."
    )
    parser.add_argument("--source-run-json", type=Path, required=True)
    parser.add_argument("--source-jobs-json", type=Path, required=True)
    parser.add_argument("--source-artifacts-json", type=Path, required=True)
    parser.add_argument("--recent-runs-json", type=Path, required=True)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument("--expected-workflow-path", required=True)
    parser.add_argument("--expected-branch", required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument(
        "--expected-plan-run-id", type=_positive_decimal_argument, required=True
    )
    parser.add_argument(
        "--current-apply-run-id", type=_positive_decimal_argument, required=True
    )
    parser.add_argument("--expected-plan-actor", required=True)
    parser.add_argument("--confirmed-artifact-digest", required=True)
    parser.add_argument("--current-time", required=True)
    parser.add_argument(
        "--maximum-age-seconds", type=_positive_decimal_argument, required=True
    )
    parser.add_argument("--github-output", type=Path, required=True)
    return parser


def _write_outputs(path: Path, outputs: PromotionOutputs) -> None:
    content = (
        f"artifact_id={outputs.artifact_id}\n"
        f"artifact_digest={outputs.artifact_digest}\n"
        f"source_run_attempt={outputs.source_run_attempt}\n"
    )
    try:
        with path.open("a", encoding="utf-8", newline="\n") as output_file:
            output_file.write(content)
    except OSError as error:
        raise PromotionValidationError("cannot write the GitHub output file") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Validate promotion metadata and emit only strict download identifiers."""
    arguments = create_parser().parse_args(argv)
    try:
        current_time = _timestamp(arguments.current_time, "current time")
        outputs = validate_foundation_promotion(
            source_run=load_strict_json(arguments.source_run_json),
            source_jobs=load_strict_json(arguments.source_jobs_json),
            source_artifacts=load_strict_json(arguments.source_artifacts_json),
            recent_workflow_runs=load_strict_json(arguments.recent_runs_json),
            expected_repository=arguments.expected_repository,
            expected_workflow_path=arguments.expected_workflow_path,
            expected_branch=arguments.expected_branch,
            expected_head_sha=arguments.expected_head_sha,
            expected_plan_run_id=arguments.expected_plan_run_id,
            current_apply_run_id=arguments.current_apply_run_id,
            expected_plan_actor=arguments.expected_plan_actor,
            confirmed_artifact_digest=arguments.confirmed_artifact_digest,
            current_time=current_time,
            maximum_age_seconds=arguments.maximum_age_seconds,
        )
        _write_outputs(arguments.github_output, outputs)
    except PromotionValidationError as error:
        print(f"FOUNDATION PROMOTION FAILED: {error}", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
