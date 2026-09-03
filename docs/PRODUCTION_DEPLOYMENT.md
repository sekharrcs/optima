---
title: OPTIMA Production Deployment
description: OIDC, Azure preflight, immutable image publication, Container Apps rollout, verification, and rollback
---

## Deployment architecture

The manual `Deploy production` workflow is the only automated production
publisher. It extends the existing Bicep architecture and does not create a
second deployment path.

```text
Exact Git commit
  -> application, Bicep, secret, and Linux AMD64 container validation
  -> protected GitHub hackathon environment
  -> GitHub OIDC login
  -> read-only foundation preflight
  -> foundation what-if and incremental convergence
  -> read-only publication preflight
  -> ACR optima-api:<commit> and optima-ui:<commit>
  -> registry-generated API and UI manifest digests
  -> optional reviewed runtime-access bootstrap
  -> read-only rollout preflight
  -> digest-qualified Container Apps deployment
  -> revision, health, UI-to-API, Entra, and telemetry verification
```

The validation job has `contents: read` only. The deployment job receives
`id-token: write` only after validation succeeds and runs inside the `hackathon`
environment. Production deployments are serialized by the
`optima-production` concurrency group and never run from a pull request or push
trigger.

The API and UI use separate Dockerfiles because their commands and ports differ.
Both install the frozen production dependency closure from the approved
Microsoft package feed, run as the Azure Linux distroless `nonroot` user, and
target `linux/amd64`. Native wheels such as `cryptography`, `pandas`, `Pillow`,
and `pyarrow` are selected inside the AMD64 builder. An ARM workstation must not
publish a native ARM build as an Azure runtime image.

## Selected East US 2 cache profile

The workflow reads one protected canonical production decision from the
`OPTIMA_SEMANTIC_CACHE_ENABLED` environment variable and passes it unchanged to
preflight, both Bicep entry points, Container Apps, and smoke. The selected East
US 2 profile sets that variable to `false`. Disabled mode makes the Bicep entry
points omit Azure Managed Redis, Redis access assignments, endpoint values,
embedding settings, and embedding pricing. The API then omits embedding, Redis,
token renewal, and index bootstrap from its lifespan.

The disabled profile retains model routing, Quality Contract enforcement,
LLM-judge evaluation, escalation, context reduction, exact active-role cost
accounting, Cosmos history, Application Insights, Entra UI authentication,
immutable publication, and paired rollback. Smoke uses a cache-eligible request
and requires typed disabled evidence with no cache step or embedding usage. No
cache hit or cache-savings claim is permitted.

Changing the mode to `true` is a reviewed configuration change to the protected
environment variable and its approval gate, not a workflow-code change. Enabled
mode reactivates every required Redis and embedding input,
provider/SKU/restriction/quota preflight, runtime assignment, index bootstrap,
pricing, and smoke gate, and fails closed when any of them is missing. Neither
mode selects a different SKU or region.

## Protected GitHub environment

Configure the `hackathon` environment before dispatching the workflow:

* Add required reviewers
* Restrict deployment branches to `main`
* Keep environment administrators separate from untrusted pull-request code
* Configure the non-secret variables below at environment scope
* Configure the single secret below at environment scope

The workflow verifies required reviewers and a deployment branch policy, and it
refuses any ref other than `refs/heads/main`. Its unprivileged job repeats the
full application checks, Linux AMD64 builds, runtime smoke, rootfs inspection,
SBOM generation, and final-image scanning for the exact main commit before Azure
login.

### Non-secret variables

| Variable | Required value or evidence |
|----------|----------------------------|
| `AZURE_CLIENT_ID` | Client ID of the dedicated OIDC deployment identity |
| `AZURE_DEPLOYMENT_IDENTITY_RESOURCE_ID` | Full resource ID of that user-assigned identity |
| `AZURE_TENANT_ID` | Reviewed tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Reviewed subscription ID |
| `AZURE_RESOURCE_GROUP` | `rg-optima-hackathon` |
| `AZURE_LOCATION` | `eastus2` |
| `AZURE_CONTAINER_REGISTRY_NAME` | Exact Basic ACR name created by OPTIMA IaC |
| `AZURE_OPENAI_RESOURCE_ID` | Full resource ID of the existing Azure OpenAI account |
| `OPTIMA_GITHUB_ENVIRONMENT` | Supplied by the workflow as `hackathon` |
| `OPTIMA_FOUNDRY_BASE_URL` | HTTPS endpoint ending in `/openai/v1` for the selected account |
| `OPTIMA_FOUNDRY_SMALL_DEPLOYMENT` | Exact SMALL deployment name |
| `OPTIMA_FOUNDRY_SMALL_MODEL` | Live SMALL model name reported by Azure |
| `OPTIMA_FOUNDRY_SMALL_MODEL_VERSION` | Live SMALL model version reported by Azure |
| `OPTIMA_FOUNDRY_STRONG_DEPLOYMENT` | Exact STRONG deployment name |
| `OPTIMA_FOUNDRY_STRONG_MODEL` | Live STRONG model name reported by Azure |
| `OPTIMA_FOUNDRY_STRONG_MODEL_VERSION` | Live STRONG model version reported by Azure |
| `OPTIMA_JUDGE_DEPLOYMENT` | Exact dedicated JUDGE deployment name |
| `OPTIMA_JUDGE_MODEL` | Live JUDGE model name reported by Azure |
| `OPTIMA_JUDGE_MODEL_VERSION` | Live JUDGE model version reported by Azure |
| `OPTIMA_SEMANTIC_CACHE_ENABLED` | Exactly `true` or `false`; selects cache-enabled or cache-disabled production and gates every cache-only variable below |
| `OPTIMA_REDIS_EMBEDDING_DEPLOYMENT` | Required only in cache-enabled mode; absent in the selected profile |
| `OPTIMA_REDIS_EMBEDDING_MODEL` | Required only in cache-enabled mode; absent in the selected profile |
| `OPTIMA_REDIS_EMBEDDING_MODEL_VERSION` | Required only in cache-enabled mode; absent in the selected profile |
| `OPTIMA_REDIS_EMBEDDING_DIMENSION` | Required only in cache-enabled mode; absent in the selected profile |
| `OPTIMA_PRICING_CATALOG_VERSION` | Immutable catalog or pricing-snapshot identifier |
| `OPTIMA_PRICING_BINDING_SHA256` | Canonical digest of source, currency, model/version bindings, and exact rates |
| `OPTIMA_PRICING_SOURCE_URL` | Public HTTPS source used for the reviewed rates |
| `OPTIMA_PRICING_CURRENCY` | One uppercase ISO currency code shared by every rate |
| `OPTIMA_PRICING_SMALL_MODEL` | Exact model name priced by the SMALL rates |
| `OPTIMA_PRICING_SMALL_MODEL_VERSION` | Exact model version priced by the SMALL rates |
| `OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS` | Positive exact Decimal rate |
| `OPTIMA_PRICING_SMALL_OUTPUT_RATE_PER_MILLION_TOKENS` | Positive exact Decimal rate |
| `OPTIMA_PRICING_SMALL_CACHED_INPUT_RATE_PER_MILLION_TOKENS` | Optional non-negative exact Decimal rate |
| `OPTIMA_PRICING_STRONG_MODEL` | Exact model name priced by the STRONG rates |
| `OPTIMA_PRICING_STRONG_MODEL_VERSION` | Exact model version priced by the STRONG rates |
| `OPTIMA_PRICING_STRONG_INPUT_RATE_PER_MILLION_TOKENS` | Positive exact Decimal rate |
| `OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS` | Positive exact Decimal rate |
| `OPTIMA_PRICING_STRONG_CACHED_INPUT_RATE_PER_MILLION_TOKENS` | Optional non-negative exact Decimal rate |
| `OPTIMA_PRICING_JUDGE_MODEL` | Exact model name priced by the JUDGE rates |
| `OPTIMA_PRICING_JUDGE_MODEL_VERSION` | Exact model version priced by the JUDGE rates |
| `OPTIMA_PRICING_JUDGE_INPUT_RATE_PER_MILLION_TOKENS` | Positive exact Decimal rate |
| `OPTIMA_PRICING_JUDGE_OUTPUT_RATE_PER_MILLION_TOKENS` | Positive exact Decimal rate |
| `OPTIMA_PRICING_JUDGE_CACHED_INPUT_RATE_PER_MILLION_TOKENS` | Optional non-negative exact Decimal rate |
| `OPTIMA_PRICING_EMBEDDING_MODEL` | Required only in cache-enabled mode; absent in the selected profile |
| `OPTIMA_PRICING_EMBEDDING_MODEL_VERSION` | Required only in cache-enabled mode; absent in the selected profile |
| `OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS` | Required only in cache-enabled mode; absent in the selected profile |
| `OPTIMA_COST_REVIEWED_ON` | Review date in `YYYY-MM-DD`, no more than 31 days old |
| `OPTIMA_EXPECTED_FIXED_MONTHLY_COST_INR` | Reviewed fixed estimate not exceeding the `5000` infrastructure allocation |
| `OPTIMA_UI_AUTH_CLIENT_ID` | Existing single-tenant Entra application client ID |
| `OPTIMA_UI_AUTH_TENANT_ID` | Same tenant as `AZURE_TENANT_ID` |
| `OPTIMA_UI_AUTH_REDIRECT_URI` | Exact deployed `https://<ui-fqdn>/.auth/login/aad/callback` URI |
| `OPTIMA_RUNTIME_ACCESS_BOOTSTRAPPED` | `true` only after all checked-in runtime assignments were applied |

Do not place connection strings, access keys, tokens, passwords, or client
secrets in repository or environment variables.

The workflow reads the semantic-cache Boolean from the protected
`OPTIMA_SEMANTIC_CACHE_ENABLED` environment variable and rejects any value other
than exactly `true` or `false` before Azure login. Cache-only GitHub variables
must remain absent or empty while the selected value is `false`. Preflight
rejects any stale nonempty value.

### Secret

| Secret | Purpose |
|--------|---------|
| `OPTIMA_UI_AUTH_CLIENT_SECRET` | Confidential-client secret for Container Apps built-in UI authentication |

GitHub masks the secret, and Bicep receives it only through the secure
`uiAuthClientSecret` parameter. The workflow never prints it. Rotate it in Entra
and the GitHub environment together.

## OIDC prerequisites

The dedicated deployment user-assigned managed identity needs this federated
credential:

| Property | Exact value |
|----------|-------------|
| Issuer | `https://token.actions.githubusercontent.com` |
| Subject | `repo:sekharrcs/optima:environment:hackathon` |
| Audience | `api://AzureADTokenExchange` |

Use temporary Contributor on the subscription only when the initial deployment
must create `rg-optima-hackathon`. After bootstrap, scope Contributor to that
resource group, retain subscription Reader for provider, quota, and assignment
inspection, and remove the subscription Contributor assignment before the next phase.
Grant `AcrPush` directly on the exact OPTIMA registry. Preflight rejects Owner,
User Access Administrator, RBAC Administrator, inherited `AcrPush`, and a
subscription Contributor assignment after the resource group exists.

The deployment identity also needs Reader on its exact user-assigned identity
and Reader on the exact external Azure OpenAI account so preflight can inspect
federation, deployments, and role assignments. Grant the managed identity the
Microsoft Graph `Application.Read.All` application role with admin consent so it
can verify the existing UI application and service-principal assignment policy.
These read grants do not permit model inference or application mutation outside
the reviewed resource group.

The reviewed access bootstrap principal additionally needs Role Based Access
Control Administrator on the exact registry to create the two `AcrPull`
assignments. The external Azure OpenAI owner grants Cognitive Services OpenAI
User to `id-optima-api-hackathon` on the exact selected account.

## First deployment procedure

The first deployment can require two manual runs because the exact UI callback
contains the Container Apps environment default domain.

1. Configure the protected environment, OIDC identity, existing model bindings,
   reviewed pricing, UI application, and client secret.
2. Open the Slice 11C pull request, wait for exact-head validation, and have an
   authorized maintainer merge it after review. This slice does not merge itself.
3. Dispatch `Deploy production` from `main` for that exact 40-character main
   commit SHA and set `confirm_deployment` to `DEPLOY`.
4. The workflow runs read-only preflight, Bicep what-if, and foundation
   convergence. This creates the existing Consumption Container Apps environment
   without exposing API or UI applications.
5. Read the exact callback URI from the workflow summary, register it as a Web
   redirect URI on the UI Entra application, require user assignment, and set
   `OPTIMA_UI_AUTH_REDIRECT_URI` to that exact value.
6. Grant Cognitive Services OpenAI User to the API identity. Grant the publisher
  `AcrPush`. Apply ACR and Cosmos runtime access with the separate reviewed
  bootstrap principal; disabled mode creates no Redis assignment. The routine
  workflow never requests RBAC administration.
7. Set `OPTIMA_RUNTIME_ACCESS_BOOTSTRAPPED=true` only after the assignments are
   present. Remove temporary subscription or RBAC-administration permissions.
8. Dispatch the workflow again for the same exact commit. It transfers the exact
   scanned images from the unprivileged job, publishes them with commit tags,
   captures registry digests, and verifies all access. It first deploys the UI
   with internal ingress, verifies the Easy Auth child through ARM, and only then
   performs a second what-if and enables external ingress.
9. Complete the interactive Entra acceptance checks that automation cannot
   impersonate safely: authorized login, unauthorized denial, logout, session
   behavior, and intended-user restriction.

Do not enable `deployContainerApps` manually to bypass a failed gate. Correct the
missing prerequisite and rerun the same immutable commit.

## Preflight procedure

The deployment workflow runs `scripts/azure_preflight.py`. It performs read-only
Azure CLI queries and emits secret-free JSON evidence with redacted subscription
and tenant IDs.

```bash
python scripts/azure_preflight.py --phase foundation --output foundation-preflight.json
python scripts/azure_preflight.py --phase publish --output publish-preflight.json
python scripts/azure_preflight.py --phase artifacts \
  --api-digest sha256:<api-digest> --ui-digest sha256:<ui-digest>
python scripts/azure_preflight.py --phase rollout \
  --api-digest sha256:<api-digest> --ui-digest sha256:<ui-digest>
```

The phases prove these progressively stronger conditions:

* `foundation`: tenant, subscription, OIDC federation, Contributor scope,
  providers, active Azure OpenAI deployments, active-role pricing provenance,
  budget, checked-in IaC representation, explicit disabled mode, and absent
  Redis and embedding configuration. Cache-enabled mode additionally requires
  the East US 2 Redis Enterprise resource type, exact Balanced B0 advertisement,
  applicable restrictions, quota exposure, and allocation-unknown evidence
* `publish`: foundation resources, exact UI callback and assignment policy,
  `AcrPush`, and external Foundry access for the API identity
* `artifacts`: separate API and UI registry manifest digests
* `rollout`: artifacts plus API/UI `AcrPull`, container-scoped Cosmos data
  contribution, and Foundry inference access. Cache-enabled mode also requires
  the Redis `default` policy

The following Redis quota and allocation behavior applies only to cache-enabled
mode. Disabled mode does not query `Microsoft.Cache` and instead proves that the
resource and embedding configuration are absent.

The stable Redis Enterprise API does not expose a documented regional quota
operation, and the Azure Quota API does not advertise Microsoft.Cache support.
When provider metadata exposes no authoritative quota surface, preflight records
`NOT_EXPOSED`; this is neither quota availability nor quota exhaustion and does
not independently block the exact reviewed deployment. If Azure later advertises
an authoritative quota operation, unavailable query evidence blocks, an exact
exhausted quota blocks, and an exact available quota remains only quota evidence.

Provider, SKU, restriction, and quota metadata do not guarantee physical
regional allocation. Preflight always records allocation as
`NOT_PROVABLE_BEFORE_CREATION`. An `AllocationFailed` response during an exact
deployment stops the workflow. No code selects another region, Redis SKU, model,
deployment, service, or authentication mechanism.

## Model and pricing binding

List the existing deployments from the selected Azure OpenAI account. For each
logical role, review the exact deployment name, model name, model version, SKU,
capacity, region availability, context limits, response-format support, and
quota. The selected profile requires distinct SMALL, STRONG, and JUDGE
deployments. Cache-enabled mode additionally requires a distinct embedding
deployment.

Do not derive a deployment name from a model name. Do not create a missing
deployment until the owner has reviewed regional availability, quota, and
estimated variable usage cost.

Live canaries for the pinned hackathon deployments returned
`gpt-5-nano-2025-08-07`, `gpt-5.2-2025-12-11`, and
`gpt-4o-2024-11-20`. Production therefore keeps deployment, model, and version
as separate reviewed values, sends the deployment name for request routing, and
derives the exact expected response identity as `${model}-${version}`. The
provider compares that derived value by strict equality without normalization,
prefix matching, or fallback identities.

This contract records observed behavior for the pinned deployments. It does not
claim that every current or future Azure model uses the same response naming
convention. Any model or version change requires a bounded canary that confirms
the returned response identity before production use.

Capture a public HTTPS pricing source without credentials or a query string,
catalog snapshot/version, currency, and exact
per-million-token rates for the same model and version. Cached-input rates are
optional only when the selected offer has no distinct cached meter. Model usage
is variable and excluded from the fixed infrastructure estimate. Preflight
rejects placeholders, missing rates, non-finite values, negative values, and
zero required rates. It requires each pricing model/version to equal the live
Azure deployment model/version for that role. `pricing_binding_sha256()` in
`scripts/azure_preflight.py` canonicalizes and hashes the complete reviewed
binding. Store that lowercase digest as `OPTIMA_PRICING_BINDING_SHA256`; changing
any source, currency, model/version, or rate invalidates preflight. Runtime sets
`OPTIMA_PRODUCTION_COST_MEASUREMENT_REQUIRED=true`, so incomplete pricing also
fails application startup.

The selected disabled profile hashes SMALL, STRONG, and JUDGE pricing. Enabled
mode adds the embedding model, version, and input rate to the same canonical
binding. Supplying any embedding pricing or identity while the mode is false is
a configuration error, not ignored metadata.

## Immutable artifact rules

Both repositories use the same full source commit as the publication tag:

```text
<registry>/optima-api:<40-character-commit>
<registry>/optima-ui:<40-character-commit>
```

The unprivileged job scans the exact local image objects, stores them in a
short-retention workflow artifact, and the deployment job verifies the archive
checksum and local image IDs before loading them. It does not rebuild. After
each push, the workflow compares local repository digests with ACR metadata and
deploys only:

```text
<registry>/optima-api@sha256:<64-lowercase-hex>
<registry>/optima-ui@sha256:<64-lowercase-hex>
```

The digests must differ. Each Container Apps revision suffix, resource tag,
Application Insights service version, workflow summary, and ARM deployment
records the source commit or workflow run. Never deploy `latest`, a local Docker
image ID, an all-zero digest, or a digest not returned by the target ACR.

Third-party GitHub Actions are pinned to full commit SHAs. The comments beside
each pin record the release tag. Resolve a proposed update with
`git ls-remote --refs <upstream-repository> refs/tags/<tag>`, review the upstream
release and commit, then update the pin and comment together.

## Runtime verification

The workflow waits for both active revisions to report `Healthy`, then verifies
their exact digest-qualified image references. It confirms the API ingress is
internal, the UI contains the deployed HTTPS API URL rather than localhost, and
a one-shot Container Apps job running the UI runtime image completes one bounded
SMALL/JUDGE production run with measured pricing. The UI image is distroless and
has no shell, so the smoke runs the image's own Python entrypoint as a job and is
gated on the `Succeeded` execution status rather than `az containerapp exec`. The
run carries a unique W3C trace ID that the workflow finds in Application Insights
by `operation_Id`. Before starting the job, the workflow reads the deployed API
revision and requires cache mode `false` with zero Redis or embedding-price
environment values.

Use these read-only checks when investigating a rollout:

```bash
az containerapp show --resource-group rg-optima-hackathon \
  --name ca-optima-api-hackathon --query properties.latestRevisionName --output tsv
az containerapp show --resource-group rg-optima-hackathon \
  --name ca-optima-ui-hackathon --query properties.latestRevisionName --output tsv
az containerapp revision list --resource-group rg-optima-hackathon \
  --name ca-optima-api-hackathon --output table
az containerapp revision list --resource-group rg-optima-hackathon \
  --name ca-optima-ui-hackathon --output table
```

An anonymous UI request must redirect to Entra or return `401`/`403`. The API
must not be reachable from the public internet. A run-specific API request made
by the pre-exposure smoke job must appear in Application Insights within the
workflow timeout. Do not print prompts, outputs, connection strings, tokens, or
secret values while inspecting logs or telemetry.

## Safe rollback

The workflow captures both ready revision names, immutable image references, and
the prior API cache mode before mutation. Any rollout or smoke failure makes UI
ingress internal first. If a prior pair exists, the workflow creates a new
single-mode revision from each captured ready revision, waits for both exact
revisions to become healthy, and verifies the image references and restored cache
mode. Manual cancellation after rollout mutation uses the same containment path.
It deliberately leaves
UI ingress internal because revision activation cannot restore or prove the
prior app-level client secret and Easy Auth configuration. A first deployment
failure also leaves the UI internal.

For a manual recovery, use the same captured revision pair with
`az containerapp revision copy --from-revision <revision>` for both applications.
Single-revision mode does not support traffic-weight rollback. Keep the UI
ingress internal until its auth configuration and the
restored UI revision have been reapplied through reviewed Bicep, verified through
ARM, and tested interactively. Keep failed and previous healthy revisions; do
not delete or deactivate them during incident response.

Do not roll back only one application. Do not substitute mutable tags or rebuild
an old commit and assume its registry digest will match.

## Troubleshooting

### OIDC login

Verify issuer, subject, audience, client ID, tenant, subscription, protected
environment name, and `id-token: write` on the deployment job. A repository or
branch subject does not satisfy the required environment subject.

### ACR access

Verify `AcrPush` for the OIDC identity and `AcrPull` for both application
identities on the exact Basic registry. Admin credentials remain disabled.
Distinguish a local image ID from the ACR manifest digest.

### Managed Redis

This troubleshooting path applies only after an explicit reviewed switch to
cache-enabled production. The selected disabled profile does not query
`Microsoft.Cache`, require Redis access, or accept embedding configuration.

Confirm `Microsoft.Cache` registration, regional `redisEnterprise` advertisement,
an exact `Balanced_B0` and `Balanced` subscription SKU entry for `eastus2`, and
the absence of applicable regional, subscription, or quota restrictions. Review
the reported quota status: `NOT_EXPOSED` is an explicit unknown, not available
quota. Unauthorized, transient, malformed, and unclassified queries are blockers.

On 2026-09-01, the verified subscription response advertised `redisEnterprise`
in East US 2 but returned the only exact `Balanced_B0` entry for Australia
Central. It returned no East US 2 match, so preflight remains blocked. Azure
Support must resolve the subscription SKU advertisement, or the user must approve
an architecture change. Do not change SKU or region automatically. A later
`AllocationFailed` response remains a hard stop even after all metadata gates pass.

### Missing model deployment or pricing

Verify the selected account resource ID and endpoint host, then compare each
live deployment model/version with its reviewed variable. Do not rename, create,
or substitute a deployment in the workflow. Refresh the public price source and
all role rates under one catalog version and currency.

### Failed readiness

Inspect the revision provisioning and system logs. Production health cannot pass
until settings validation, telemetry composition, model clients, and Cosmos
composition complete. Cache-enabled mode additionally requires Redis index
bootstrap. Correct the failing dependency; do not switch to in-memory stores,
fake providers, access keys, or local services.

### UI-to-API connectivity

Confirm the API is internal in the shared environment and the UI revision has
the exact HTTPS API FQDN. The value must not contain localhost. Check internal
DNS and ingress before changing application configuration.

## Cost boundary

The prior approximately INR 3,250 fixed estimate included approximately INR
1,117 per month for Balanced B0. Omitting Redis reduces the comparable fixed
estimate to approximately INR 2,133 per month before tax. Log Analytics at the
configured cap is approximately INR 1,650 per 30 days. Container Apps active
time, Cosmos requests and storage, telemetry ingestion, and model calls remain
variable.

Refresh the estimate before every deployment. Preflight requires a review no
more than 31 days old and rejects a fixed estimate above the INR 5,000
infrastructure allocation. The overall budget remains approximately INR 12,500,
with the remainder reserved for model use and benchmarks. A passing fixed-cost
gate is not authorization for sustained public traffic.