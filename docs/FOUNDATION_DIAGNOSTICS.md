---
title: Foundation Diagnostic Capture
description: Bounded failure evidence for a separately authorized foundation plan and private inspection limits
---

## Scope and observed evidence

No run, retry, deployment, merge, permission change, or cost-date change is authorized here. Use this procedure only for the NEXT separately authorized `foundation-plan` run.
Every nonempty `diagnostics` collection remains fatal, including `Info`, `Warning`,
recognized codes, malformed entries, and entries beyond the display limit.
The classifier and promotion acceptance rules are unchanged; failure evidence never authorizes apply.

Run `34355473718` retained `WHATIF_DIAGNOSTICS`, one diagnostic, nine `changes` entries, and `potentialChanges: null`. Its diagnostic code, level, message, target,
and cause were discarded. Nine entries do not establish nine approved changes.
Counts, digests, tool versions, and another run cannot recover those bytes.
Nested-deployment short-circuiting is a published possible explanation, not an
observed cause of this run. Outer-mode evaluation is one published message form,
not a conclusion supported by the lost diagnostic or by its count.

## Preconditions before any future run

1. Require reviewed merge of the v2 projection AND stdout/stderr containment changes,
   with required checks passing on the reviewed source. Confirm the exact current
   protected `main` SHA; obtain separate explicit authorization for one run at that SHA.
   These instructions do not authorize the merge or dispatch.
2. Confirm no conflicting foundation or production run, pending conflicting approval,
   or active foundation deployment. Preserve the existing concurrency and preflight gates.
3. Require the existing cost review to remain valid at execution time. Keep
   `OPTIMA_COST_REVIEWED_ON` and the approved estimate unchanged; stop if stale or invalid.
   Do not refresh a date, change configuration, or add permissions to pass a gate.
4. Use the normal `hackathon` environment approval and existing plan identity.
   Preserve identity, scope, source, and authorization checks without bypasses.
5. Decide whether bounded published evidence is sufficient BEFORE dispatch. The next
   response may have an unknown code or message. Avoiding another loss of explanatory
   prose may require separate approval of the private route below before any new run.

## Exact bounded capture procedure

1. After authorization, select `Foundation plan and apply`, the authorized current
   `main` SHA, and `operation=foundation-plan`. Leave all FIVE apply inputs empty:
   `confirm_commit_sha`, `confirm_foundation`, `plan_run_id`, `confirm_plan_actor`,
   and `confirm_plan_artifact_digest`. A moved SHA requires renewed authorization.
2. Verify the unchanged profile: `environmentName=hackathon`, `location=eastus2`,
   `resourceGroup=rg-optima-hackathon`, and `deployContainerApps=false`,
   `exposePublicUi=false`, `deployRuntimeAccess=false`, `semanticCacheEnabled=false`.
   Keep the existing foundation template, parameter file, and run provenance bindings.
3. Allow only the workflow's existing single authoritative what-if, with
   `ProviderNoRbac`, `FullResourcePayloads`, and structured non-pretty output unchanged.
   There is no diagnostic retry, reduced validation, debug mode, or additional input.
4. Require both what-if stdout and stderr to stay in narrowly owned runner temporary
   storage with restrictive permissions. Raw files must be removed on success and
   handled failure, without hiding the failing exit status. Do not echo, `tee`, upload,
   or add raw content to step outputs, logs, summaries, or caches. No new permissions
   or encrypted raw artifacts are authorized. Runner loss or forced termination can
   defeat cleanup; this is not a guaranteed deletion or operator-access mechanism.
5. On classifier failure, retain only `foundation-classification-failure.json` in
   `foundation-classification-failure-RUN-ATTEMPT`, substituting that run ID and attempt.
   Retention is seven days. This separate failure artifact is never promotion evidence.
   CLI failure before classification or report-write failure may leave no artifact;
   stop without a raw fallback or another operation. On success, use the separate
   approved-plan procedure; this runbook grants no apply authorization.

## Inspect only the same-run safe artifact

1. Record the authorized SHA, actual run ID, attempt, actor, workflow, and outcome.
   Select the exact failure artifact from that SAME run and attempt, not a similarly
   named artifact from an earlier run. Require unexpired metadata and record its artifact ID.
2. Download that artifact's archive and compare its locally calculated SHA-256 with
   GitHub's artifact metadata `sha256:` digest. Hash the downloaded archive bytes,
   NOT the extracted JSON, a reserialized document, or a newly repacked archive.
   Stop on missing digest, mismatch, unexpected provenance, or expired evidence.
3. Require exactly the expected regular JSON file, without extra entries or symlinks.
   Validate the closed v2 shape before displaying fields; reject duplicate keys or
   unexpected artifact fields. Read only the known-safe fields listed below.
4. Keep the result `FAILED` and `promotable=false`. Review recognized literals and
   reconstructed summaries as limited evidence, not an override. If a field is unknown,
   withheld, invalid, unexpectedly missing/null, or omitted, or explanation is insufficient,
   stop and use the separately authorized private route. Never publish unknown contents.

## Safe output contract and limits

The schema is `optima-foundation-whatif-failure-v2`. Safe top-level fields are
`schema_version`, `classification`, `promotable`, `classifier_error`, `document`,
`fields`, `diagnostic_details`, and `toolchain`. `document` holds `parsed`, `json_type`,
and `unknown_field_count`. `fields` describes only `status`, `error`, `changes`,
`potentialChanges`, and `diagnostics` using `present`, `json_type`, and `array_count`.
`toolchain` holds bounded numeric versions or null for `python`, `azure_cli`,
`azure_cli_core`, `bicep`, and `runner_image`. No raw status or service error is copied.

* `diagnostic_details.entries` contains at most 32 entries in original order.
  `omitted_count` counts the remainder; it is null when no diagnostic array is available.
  An empty projection never proves an empty or accepted original response.
* Each entry contains exactly `index` (zero-based), `json_type`, `code`, `code_state`,
  `level`, `level_state`, `message_state`, `message_form`, `message_summary`, and
  `unknown_field_count`. Non-object entries retain their type; their field count is null.
* States are `recognized`, `missing`, `null`, `invalid`, or `withheld`.
  Missing means absent, null means explicit JSON null, invalid means wrong type, and
  withheld means an unrecognized string. Nonrecognized code/level values are null;
  nonrecognized message forms and summaries are null. Unknown names and values are omitted.
* The ONLY emitted service code is the trusted literal `NestedDeploymentShortCircuited`.
   Code validation is exact literal lookup, not a broad identifier regex: secrets can look alphanumeric. Levels use exact trusted `Info`, `Warning`, or `Error` literals
  from `level`, never inferred `severity` or case-normalized input.
* A message is recognized only with a recognized code AND level and a full match of
  the exact published text, punctuation, and fixed guidance suffix, at most 4096 characters.
  Its deployment path must exactly equal `target`: `/subscriptions/{subscription}`,
  optionally `/resourceGroups/{group}`, then `/providers/Microsoft.Resources/deployments/{name}`.
  The subscription slot is 36 ASCII hex-or-hyphen characters; group/name slots are
  1-90/1-64 ASCII letters, digits, underscores, periods, parentheses, or hyphens.
  Source line and column each allow 1-10 ASCII decimal digits. These are recognition
  bounds, not proof of a real scope or a full ARM resource-name validator.
* The sole form is `nested-deployment-outer-evaluation-v1`. It emits this trusted
  reconstructed constant, never captured target, positions, URL, or raw message:

```text
The nested deployment '[REDACTED]' at line '[REDACTED]' and column '[REDACTED]' could not be expanded because it uses outer-mode evaluation and its template contains expressions that could not be evaluated. This is a reconstructed explanation; analysis is incomplete and review is required.
```

`target`, `additionalInfo`, and unknown fields are never published; unknown fields
contribute only counts. Prefixes, suffix drift, oversized text, and unknown prose
are withheld, not partially scrubbed. These bounds cover projection and matching,
not total raw-response size or parser depth. The catalogue is intentionally incomplete:
the service schema declares open strings, not an exhaustive code/message catalogue.
No guarantee exists that the next response matches. Code alone does not prove outer-mode evaluation.

## Unknown prose and private inspection

GUID/URL removal, truncation, masking, secret scanners, and AI summaries cannot prove
unknown prose safe. The current GitHub-hosted ephemeral job has no established
operator-only ACL, private shell, or retained raw viewer. Repository-read artifact access
is not restricted by environment approval or seven-day retention. Its discarded raw
response is unavailable; do not promise retrieval from deployment history or an operation name.

1. Stop. Obtain separate authorization naming the operator, scope, private access,
   data handling, and cleanup. Ask the service owner for a supported diagnostic
   inspection route, such as Azure Support using existing operation metadata if
   available. Availability and retention are unverified, not guaranteed recovery.
2. If necessary, approve a private, supervised, operator-controlled execution environment
   BEFORE any separately authorized new what-if. Use current identity and existing
   access only, with private temporary storage; no grants or configuration changes.
   If that boundary cannot be established, do not execute.
3. Keep both raw streams and inspection off repository files, artifacts, logging,
   transcripts, synced folders, and AI systems. Human inspection occurs only privately.
   Release only a human-approved redacted fixed summary, not unknown prose or input
   fragments; minimize any separately approved support submission.
4. Delete private temporary data and record only approved safe conclusions, new-run
   provenance, and cleanup confirmation. A new response is new evidence, never recovery
   of the old one. Failure remains nonpromotable; another operation needs separate authorization.

## Pinned source evidence

CLI 2.89.1 uses deployment SDK 1.0.0b1 with REST 2025-04-01. CLI conversion flattens `properties.diagnostics` to `diagnostics`; SDK optional attributes may serialize as null.
The upstream examples establish the one display form, not this repository's historical cause.

* [CLI dependency pin](https://github.com/Azure/azure-cli/blob/d3d81d5007da1688e8dc1e65d852151176fad757/src/azure-cli/requirements.py3.Linux.txt)
* [CLI JSON conversion](https://github.com/Azure/azure-cli/blob/d3d81d5007da1688e8dc1e65d852151176fad757/src/azure-cli-core/azure/cli/core/util.py#L674)
* [SDK diagnostic models](https://github.com/Azure/azure-sdk-for-python/blob/11f7eb8bee01b9d7a9b3fdefec9610f8d2521326/sdk/resources/azure-mgmt-resource-deployments/azure/mgmt/resource/deployments/models/_models_py3.py)
* [SDK API default](https://github.com/Azure/azure-sdk-for-python/blob/11f7eb8bee01b9d7a9b3fdefec9610f8d2521326/sdk/resources/azure-mgmt-resource-deployments/azure/mgmt/resource/deployments/_configuration.py)
* [REST diagnostic schema](https://github.com/Azure/azure-rest-api-specs/blob/8338afaf221a4ca1cc2a134cdc38952bdd5c7245/specification/resources/resource-manager/Microsoft.Resources/deployments/stable/2025-04-01/deployments.json)
* [Published formatter example](https://github.com/Azure/azure-cli/blob/d3d81d5007da1688e8dc1e65d852151176fad757/src/azure-cli/azure/cli/command_modules/resource/tests/latest/test_resource_formatters.py#L577)
* [Published sanitized recording](https://github.com/Azure/azure-cli/blob/d3d81d5007da1688e8dc1e65d852151176fad757/src/azure-cli/azure/cli/command_modules/resource/tests/latest/recordings/test_subscription_level_what_if_with_short_circuiting.yaml#L429)
