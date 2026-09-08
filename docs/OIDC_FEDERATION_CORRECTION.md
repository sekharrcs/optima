---
title: OPTIMA OIDC Federation Correction Proposal
description: Verified subject mismatch, two proposed credential updates, and read-only verification
---

## Review Status

Prepared on 2026-09-08. Configuration changes below are proposals only and were
not executed. Code and draft PR preparation were authorized; merging, trust or
GitHub configuration changes, permission grants, and workflow dispatch were not.
Do not dispatch while code and trust are inconsistent.

## Verified Failure

[Foundation-plan run 34228402819](https://github.com/sekharrcs/optima/actions/runs/34228402819)
used main commit `26e7db1b23cb943cfc4989eee8ae18f5b24251b0`. Validation passed;
`azure/login` failed with `AADSTS700213` at `2026-09-08T12:59:14Z`, before
preflight, what-if, classification, or artifact creation.

The action logged these claim values, without needing to expose a JWT:

| Claim | Exact emitted value |
|-------|---------------------|
| Issuer | `https://token.actions.githubusercontent.com` |
| Subject | `repo:sekharrcs@45002138/optima@1333906197:environment:hackathon` |
| Audience | `api://AzureADTokenExchange` |

Both Azure identities below currently have one credential named
`github-optima-hackathon` with matching issuer and audience, but subject
`repo:sekharrcs/optima:environment:hackathon`.

## Effective GitHub Configuration

Read-only `GET /repos/sekharrcs/optima/actions/oidc/customization/sub` returned:

```json
{
  "use_default": true,
  "use_immutable_subject": false,
  "sub_claim_prefix": "repo:sekharrcs@45002138/optima@1333906197"
}
```

Repository metadata confirmed `full_name=sekharrcs/optima`, repository ID
`1333906197`, owner login `sekharrcs`, owner ID `45002138`, and owner type `User`.
This repository has no owning organization whose OIDC template could apply.
`use_default=true` selects GitHub's generated default, not an active customized
claim-key template. The emitted subject and returned prefix establish the current
ID-bearing format; the immutable Boolean alone does not establish its format.
No claim is made about when or why this repository began using that format.

Current official sources:

- [GitHub immutable subject claims](https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims)
  documents owner/repository IDs in generated default subjects and exact matching
  trust policies. The environment context remains `:environment:hackathon`.
- [GitHub OIDC REST reference](https://docs.github.com/en/rest/actions/oidc)
  defines default/custom template settings and the effective subject prefix.
- [Microsoft workload identity federation](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation)
  requires case-sensitive issuer, subject, and audience matching.
- [Azure CLI credential update](https://learn.microsoft.com/en-us/cli/azure/identity/federated-credential#az-identity-federated-credential-update)
  supports updating these properties on an existing managed-identity credential.

## Affected Identities

Subscription: `cce38a08-26e8-4b74-8fdb-df7a6db795ed`.
Tenant: `d04cc813-b8d5-4eba-aca4-391c3278fd1a`.
Resource group: `rg-optima-bootstrap`.

| Role | Identity name | Client ID | Principal ID |
|------|---------------|-----------|--------------|
| Foundation plan | `id-optima-github-foundation-plan` | `4f75358a-eb79-495a-bca4-f1a39f20169c` | `8afc339b-298d-4d8a-a57e-edb5e5bfd150` |
| Deployment | `id-optima-github-deployer` | `cc0f08d6-53ff-4f21-b0df-25742f1b69a5` | `e78ea309-d34a-4c37-8c44-ee76ed86e628` |

Exact resource IDs (ARM path casing is not significant):

```text
/subscriptions/cce38a08-26e8-4b74-8fdb-df7a6db795ed/resourcegroups/rg-optima-bootstrap/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-github-foundation-plan
/subscriptions/cce38a08-26e8-4b74-8fdb-df7a6db795ed/resourcegroups/rg-optima-bootstrap/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-optima-github-deployer
```

## Proposed Configuration Delta

Apply the same delta separately to the existing `github-optima-hackathon`
credential under each identity:

| Property | Before | After |
|----------|--------|-------|
| Subject | `repo:sekharrcs/optima:environment:hackathon` | `repo:sekharrcs@45002138/optima@1333906197:environment:hackathon` |
| Issuer | `https://token.actions.githubusercontent.com` | Unchanged |
| Audiences | `["api://AzureADTokenExchange"]` | Unchanged |
| Name | `github-optima-hackathon` | Unchanged |
| Credential count per identity | `1` | `1` |

GitHub configuration changes: **none**. Keep the effective JSON above unchanged,
including `use_default=true`. Do not set a custom template, change organization
settings, or attempt to restore name-only subjects.

Preserve both identities, their client/principal IDs, issuer, audience, environment
restriction, main-only protection, normal environment review, and role assignments.
Plan remains subscription Reader plus the target-group what-if-only custom role;
deployment remains subscription Reader plus target-group Contributor. This common
environment subject does not cryptographically isolate jobs; existing workflow
policy and distinct identity/RBAC bindings continue to separate plan and apply.
No role or Graph consent changes are included. The deployment identity's deferred
`Application.Read.All` grant remains a separate prerequisite for future deployment.

### Commands Awaiting Separate Approval

These two commands update existing credentials, not identities or additional
credentials. Do not run them until the exact delta is explicitly approved. Do not
replay the dated bootstrap creation commands in historical tracking records.

```powershell
az identity federated-credential update `
  --subscription cce38a08-26e8-4b74-8fdb-df7a6db795ed `
  --resource-group rg-optima-bootstrap `
  --identity-name id-optima-github-foundation-plan `
  --name github-optima-hackathon `
  --subject 'repo:sekharrcs@45002138/optima@1333906197:environment:hackathon' `
  --issuer 'https://token.actions.githubusercontent.com' `
  --audiences 'api://AzureADTokenExchange' `
  --only-show-errors --output json
```

```powershell
az identity federated-credential update `
  --subscription cce38a08-26e8-4b74-8fdb-df7a6db795ed `
  --resource-group rg-optima-bootstrap `
  --identity-name id-optima-github-deployer `
  --name github-optima-hackathon `
  --subject 'repo:sekharrcs@45002138/optima@1333906197:environment:hackathon' `
  --issuer 'https://token.actions.githubusercontent.com' `
  --audiences 'api://AzureADTokenExchange' `
  --only-show-errors --output json
```

If an update fails or its result is uncertain, stop and read back that credential;
do not blindly resend, add a second credential, or weaken matching.

## Verification and Coordination

1. Review the code and proposal together. Keep all dispatches paused until the
   corrected preflight contract is on reviewed main and both trust changes are
   separately approved and verified. Neither merge nor updates are performed here.
2. Before any approved update, repeat the following GET/list commands and compare
   both identity/client/principal/tenant bindings and complete credential inventories
   with the before-state above. Stop on any unexpected change.
3. After each approved update, repeat its readback and verify only the subject
   changed, the other properties are unchanged, and the identity still has one
   exact credential with no claims-matching expression. Recheck existing RBAC and
   environment protections as part of the normal deployment-readiness review.
4. From the reviewed code, run `python scripts/oidc_federation.py`. Require exit 0
   immediately before requesting a dispatch. Exit 1 means stop. This operator
   gate is not an automatic enforcement hook on GitHub's dispatch endpoint.
5. Obtain separate authorization for a fresh foundation-plan run and normal
   environment approval. Do not retry the failed run or infer that this proposal
   authorizes a dispatch. A metadata check cannot prove authentication.

Read-only commands (existing authenticated `gh`/`az` sessions required):

```powershell
gh api repos/sekharrcs/optima
gh api repos/sekharrcs/optima/actions/oidc/customization/sub
az identity show --subscription cce38a08-26e8-4b74-8fdb-df7a6db795ed --resource-group rg-optima-bootstrap --name id-optima-github-foundation-plan --output json
az identity federated-credential list --subscription cce38a08-26e8-4b74-8fdb-df7a6db795ed --resource-group rg-optima-bootstrap --identity-name id-optima-github-foundation-plan --output json
az identity show --subscription cce38a08-26e8-4b74-8fdb-df7a6db795ed --resource-group rg-optima-bootstrap --name id-optima-github-deployer --output json
az identity federated-credential list --subscription cce38a08-26e8-4b74-8fdb-df7a6db795ed --resource-group rg-optima-bootstrap --identity-name id-optima-github-deployer --output json
python scripts/oidc_federation.py
```

The checker uses only metadata GET/show/list operations and bounded subprocesses.
It checks repository/owner names and IDs, effective default prefix, both reviewed
identity bindings, sole credential resource IDs/names, and exact trust fields.
It refuses custom/unknown prefixes, legacy or wildcard subjects, extra credentials,
and broadened audiences. CLI failures are sanitized. No JWT is requested or logged.

On 2026-09-08 this new command ran against live unchanged configuration and returned
exit 1: `Federation mismatch: id-optima-github-foundation-plan, id-optima-github-deployer`.
Corrected after-state fixtures pass. This demonstrates predispatch detection of
the actual mismatch without changing Azure to make the check pass.

Live authentication still must prove GitHub assertion issuance in the protected
environment, Entra token exchange and propagation, Azure subscription selection,
ARM session-principal binding, and post-login role/Graph preflight. A successful
future plan login proves only the plan identity; deployment authentication remains
separate and must not be exercised by dispatching production just for a test.

## Repository Assumption Audit

The old subject constructor in `scripts/azure_preflight.py` and the shared fixture
in `tests/test_azure_preflight.py` were corrected. Current OIDC guidance in
`docs/AZURE_INFRASTRUCTURE.md` and `docs/PRODUCTION_DEPLOYMENT.md` was aligned.
The shared contract and operator check live in `scripts/oidc_federation.py`, with
focused tests in `tests/test_oidc_federation.py`. No subject override exists in the
foundation or production `azure/login` steps, so no workflow change is required.

No tracked bootstrap script or Bicep federated-credential writer was found.
Ignored dated foundation-readiness and administrative-setup records contain the
old creation instruction; they remain historical evidence, superseded by this
update-only proposal. No dependencies, runtime product behavior, or infrastructure
deployment parameters were changed.