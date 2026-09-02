---
title: OPTIMA Azure Infrastructure and Runtime Readiness
description: Slice 11A and 11B Azure topology, runtime composition, deployment contracts, cost controls, and prerequisites
---

# OPTIMA Azure Infrastructure Foundation

## Slice boundary

> [!IMPORTANT]
> Slices 11A and 11B define infrastructure and deployment readiness only. They do not
> deploy, update, or delete Azure resources. It defines optional runtime access
> assignments, but it does not create federated credentials, GitHub
> environments, or GitHub secrets. Slice 11B-S adds credential-free, read-only
> pull-request verification; it has no Azure identity or deployment capability.

The reviewed target metadata is:

| Property          | Value                                      |
|-------------------|--------------------------------------------|
| Subscription      | Visual Studio Enterprise Subscription      |
| Subscription ID   | `cce38a08-26e8-4b74-8fdb-df7a6db795ed`   |
| Tenant ID         | `d04cc813-b8d5-4eba-aca4-391c3278fd1a`   |
| Tenant domain     | `sekhar183live.onmicrosoft.com`            |
| Application region | East US 2 (`eastus2`)                     |
| Environment       | `hackathon`                                |
| GitHub repository | `sekharrcs/optima`                         |

The subscription is treated as a fresh OPTIMA target, not as an empty
subscription. Templates own only the deterministic OPTIMA resource group and
resources declared beneath it. Unrelated subscription resources are outside the
deployment boundary.

## Selected temporary production profile

The East US 2 hackathon profile explicitly sets `semanticCacheEnabled=false`.
It omits Azure Managed Redis, the Redis database and access-policy assignment,
all Redis endpoint settings, embedding identity and pricing, token renewal, and
RediSearch bootstrap. The API still runs Quality Contract enforcement, SMALL and
STRONG routing, LLM-judge evaluation, escalation, context reduction, active-role
cost accounting, Cosmos run history, and Application Insights. The UI remains
Entra protected and renders typed disabled cache evidence without cache-hit or
cache-savings claims.

This profile is temporary. Cache-enabled production remains represented by the
same Bicep and application contracts and retains every exact Managed Redis,
embedding, pricing, access, index, and preflight gate. The East US 2
`Balanced_B0` advertisement remains blocked and receives no automatic region or
SKU fallback.

## Repository findings

The current application already defines the Azure data-plane contracts. The
infrastructure preserves them rather than introducing replacement services.

| Integration          | Repository contract                                                                 | Startup behavior                                  |
|----------------------|--------------------------------------------------------------------------------------|---------------------------------------------------|
| FastAPI API          | Factory `optima.api.production:create_production_app`, port `8000`, health at `/api/v1/health` | Lifespan composes Azure resources before readiness |
| Streamlit UI         | `streamlit run src/ui/app.py`, port `8501`, `OPTIMA_API_BASE_URL`                    | Starts without an available API                   |
| Foundry/APIM         | HTTPS Azure OpenAI v1 root ending `/openai/v1`; SMALL, STRONG, and JUDGE names; embedding only when cache is enabled | First active model, judge, or embedding request performs I/O |
| Cosmos DB            | NoSQL API, database and container supplied by settings, partition key `/id`          | Client is lazy; database and container must exist |
| Azure Managed Redis  | Cache-enabled only: TLS port `10000`, RESP2, RediSearch HASH index, `FLOAT32`/COSINE | Enabled lifespan validates or creates the index idempotently |
| Application Insights | Workspace-based connection string, local OpenTelemetry providers, explicit close     | Failure-isolated initialization during app build  |

The default FastAPI export remains intentionally unconfigured for library and
local health use. The production factory validates all Azure settings, composes
Foundry generator and optional JUDGE resources, mode-selected evaluation,
centralized active-role pricing, context reduction, Cosmos, and observability.
It adds Redis and embedding only when explicitly enabled, then owns cleanup
through FastAPI lifespan. Separate API and UI image definitions now
exist. `deployContainerApps` remains `false` until Slice 11C publishes immutable
images and completes live access and regional preflight.

## Azure topology

```text
GitHub Actions deployment (Slice 11C)
        |
        | OIDC federation
        v
Azure Resource Manager
        |
        +-- subscription bootstrap: infra/main.bicep
        |       |
        |       +-- rg-optima-hackathon
        |
        +-- routine deployment: infra/resource-group.bicep
                |
                +-- ACR Basic
                +-- Container Apps Consumption environment
                |       +-- internal OPTIMA API
                |       +-- Entra-protected public OPTIMA Streamlit UI
                +-- Cosmos DB for NoSQL serverless
                +-- Azure Managed Redis Balanced B0 (cache-enabled only)
                +-- Log Analytics workspace
                +-- workspace-based Application Insights
                +-- API user-assigned managed identity
                +-- UI user-assigned managed identity
                +-- optional OPTIMA runtime access assignments

OPTIMA API --managed identity--> Foundry or APIM endpoint (external input)
OPTIMA API --managed identity--> Cosmos DB
OPTIMA API --managed identity--> Azure Managed Redis (cache-enabled only)
OPTIMA UI  --HTTPS-------------> internal OPTIMA API
```

### Resource inventory

| Resource                       | Selected configuration                                                                 | Purpose                                      |
|--------------------------------|----------------------------------------------------------------------------------------|----------------------------------------------|
| Resource group                 | `rg-optima-hackathon` in East US 2                                                     | Own all application resources                |
| Azure Container Registry       | Basic, public endpoint, admin disabled, no dedicated data endpoint                     | Store API and UI images                      |
| Container Apps environment     | Consumption-only, non-zone-redundant, no VNet, platform logs disabled                 | Host separate API and UI apps                |
| API Container App              | Internal ingress, `0.5` vCPU/`1.0Gi`, min `0`, max `3`, HTTP health probes            | Execute OPTIMA and own Azure integrations    |
| UI Container App               | External HTTPS ingress, tenant-restricted Entra auth, `0.5` vCPU/`1.0Gi`, min `0`, max `2` | Serve the Streamlit experience               |
| Cosmos DB account              | NoSQL, serverless, one region, Session consistency, local auth disabled               | Persist immutable run history                |
| Cosmos database/container      | `optima` / `runs`, partition key `/id`, consistent default index                      | Satisfy the existing storage adapter         |
| Azure Managed Redis            | Omitted in the selected profile; enabled mode uses Balanced B0, HA off, TLS 1.2, and disabled access keys | Host semantic-cache evidence when enabled |
| Redis database                 | Omitted in the selected profile; enabled mode uses port `10000`, Enterprise clustering, NoEviction, and RediSearch | Support filtered `FT.SEARCH` when enabled |
| Log Analytics workspace        | PerGB2018, 30-day immediate purge, 0.25 GB/day emergency cap                          | Store Application Insights telemetry         |
| Application Insights           | Workspace-based, public ingestion, 30 days, local ingestion auth retained             | Preserve Slice 10D traces and metrics        |
| API managed identity           | User-assigned                                                                          | Stable pre-assignable API runtime identity   |
| UI managed identity            | User-assigned                                                                          | Pull only the UI image from ACR               |

The API and UI remain separate deployment units because the UI is an HTTP
client of the API, they use different ports and processes, and only the API
needs model, Cosmos, and telemetry configuration. Redis is conditional on the
explicit cache mode. The API has internal
Container Apps ingress because the current API has no caller authentication.
The UI is the only public application endpoint. Container Apps built-in
authentication redirects every anonymous browser request to the configured
single-tenant Microsoft Entra provider before Streamlit receives it.

## Deliberate omissions

### API Management and Foundry resources

The provider accepts either a direct Foundry endpoint or an APIM-hosted Azure
OpenAI v1 endpoint. APIM is not required by the current adapter and would add a
billable gateway before model and benchmark requirements are finalized. Slice
11A therefore treats the endpoint and deployment names as external parameters.
Model resources and APIM belong to a reviewed AI/gateway slice.

### Key Vault

No static Azure service credential remains in the proposed runtime:

* Foundry uses the API managed identity
* Cosmos local authentication is disabled
* Redis access-key authentication is disabled
* ACR admin authentication is disabled

Application Insights still uses its generated connection string because the
installed exporter does not accept a managed-identity credential. Microsoft
documents the contained instrumentation key as a resource identifier, not a
security token or key. The value is a secure Bicep module output and input,
stored as a Container Apps secret, referenced by the API environment, and never
returned by the root template. Adding Key Vault for this generated destination
value would add lifecycle and access complexity without improving the current
threat boundary.

### Networking stack

This hackathon environment intentionally uses public Azure service endpoints
with identity-based authorization. It has no VNet integration, private
endpoints, private DNS, NAT Gateway, Firewall, Front Door, Application Gateway,
or zone redundancy. This avoids fixed networking charges and deployment delay.
Public endpoints do not imply anonymous data access: Cosmos and Redis local keys
are disabled, and runtime access is identity-scoped.

## Data service contracts

### Cosmos DB for NoSQL

Cosmos DB uses serverless throughput because hackathon traffic is intermittent
and the adapter performs bounded point operations plus short recent-history
queries. No RU/s value is provisioned. Serverless bills consumed request units
and storage, has no minimum throughput charge, and is limited to one region.

The database is `optima`. The `runs` container uses `/id` as a version 2 hash
partition key. Its consistent index includes all paths except `_etag`, which is
sufficient for the adapter's single-property `ORDER BY c.sort_key DESC` query.
No composite index or analytical store is enabled. Session consistency matches
the single-region, user-facing history workflow without paying for stronger
cross-region semantics.

The database and container are infrastructure-owned and must exist before the
first history operation. The application never creates them.

### Azure Managed Redis

This section defines the preserved cache-enabled profile. The selected temporary
profile does not deploy or connect to this service.

Balanced B0 is the smallest Azure Managed Redis SKU and provides 0.5 GB,
RediSearch, vector search, Microsoft Entra authentication, and the required TLS
endpoint. High availability is disabled because the semantic cache is an
optimization: lookup failure already falls back to normal model execution.
Persistence, replicas, geo-replication, and clustering for scale are omitted.

RediSearch requires `EnterpriseCluster` and `NoEviction`. The module enables the
RediSearch capability, but ARM does not create the application-level index. The
production API lifespan now owns an authenticated, idempotent bootstrap for
`optima-cache-v1` with this schema:

```text
FT.CREATE optima-cache-v1 ON HASH PREFIX 1 optima:semantic-cache: SCHEMA
  schema_version TAG
  embedding_profile TAG
  task_type TAG
  complexity TAG
  embedding VECTOR FLAT 6 TYPE FLOAT32 DIM <reviewed-dimension> DISTANCE_METRIC COSINE
```

The bootstrap uses `FT._LIST` and `FT.INFO`. It creates the index only when the
index and companion contract hash are both absent. An existing index is a
read-only no-op only when its name, HASH prefix, exact TAG fields, vector type,
dimension, algorithm, metric, cache schema version, semantic-input policy, and
embedding-profile identity match. Any mismatch fails startup without dropping
the index or deleting cache data.

A bounded Redis lock coordinates concurrent replica startup. Followers wait for
the creator's contract and then perform the same full validation. A stale
contractless index fails closed once no active creator owns the lock.

## Configuration mapping

### UI settings

| Application setting   | Azure source                    | Secret | Identity alternative | Owner          |
|-----------------------|---------------------------------|--------|----------------------|----------------|
| `OPTIMA_API_BASE_URL` | Internal API Container App FQDN | No     | Not applicable       | Container Apps |
| `OPTIMA_API_TIMEOUT_SECONDS` | Literal `315`                | No     | Not applicable       | IaC            |
| `OPTIMA_DEPLOYMENT_ENVIRONMENT` | Literal `hackathon`       | No     | Not applicable       | IaC            |
| `OPTIMA_REQUIRE_REFERENCE_OUTPUT` | Derived from evaluator mode | No   | Not applicable       | IaC            |
| `OPTIMA_UI_PRODUCTION_MODE` | Literal `true`              | No     | Not applicable       | IaC            |

The UI receives no Foundry, Cosmos, Redis, or Application Insights setting.

### API service settings

| Application setting                                 | Azure source                                   | Secret | Identity alternative           | Owner              |
|-----------------------------------------------------|------------------------------------------------|--------|--------------------------------|--------------------|
| `OPTIMA_DEPLOYMENT_ENVIRONMENT`                     | Literal `hackathon`                             | No     | Not applicable                 | IaC                |
| `OPTIMA_PRODUCTION_EVALUATOR_MODE`                  | Reviewed `EXACT_REFERENCE` or `LLM_JUDGE` parameter | No | Not applicable                 | AI/model slice     |
| `OPTIMA_PRODUCTION_REQUIRE_REFERENCE_OUTPUT`        | Derived from evaluator mode                    | No     | Not applicable                 | IaC                |
| `OPTIMA_SEMANTIC_CACHE_ENABLED`                     | Explicit literal `false` in the selected profile | No   | Not applicable                 | IaC                |
| `OPTIMA_EXECUTION_CONCURRENCY_LIMIT`                | Literal `4`                                    | No     | Not applicable                 | IaC                |
| `OPTIMA_EXECUTION_TIMEOUT_SECONDS`                  | Literal `300`                                  | No     | Not applicable                 | IaC                |
| `OPTIMA_FOUNDRY_BASE_URL`                           | Reviewed Foundry/APIM parameter                | No     | Not applicable                 | AI/gateway slice   |
| `OPTIMA_FOUNDRY_SMALL_DEPLOYMENT`                   | Reviewed model deployment parameter            | No     | Not applicable                 | AI/model slice     |
| `OPTIMA_FOUNDRY_STRONG_DEPLOYMENT`                  | Reviewed model deployment parameter            | No     | Not applicable                 | AI/model slice     |
| `OPTIMA_JUDGE_DEPLOYMENT`                           | Reviewed judge deployment parameter            | No     | Not applicable                 | AI/model slice     |
| `OPTIMA_JUDGE_MODEL`                                | Reviewed judge model identity                  | No     | Not applicable                 | AI/model slice     |
| `OPTIMA_JUDGE_TIMEOUT_SECONDS`                      | Bounded judge timeout parameter                | No     | Not applicable                 | AI/model slice     |
| `OPTIMA_FOUNDRY_AUTH_MODE`                          | Literal `MANAGED_IDENTITY`                     | No     | Selected mode                  | IaC                |
| `OPTIMA_FOUNDRY_TOKEN_SCOPE`                        | Reviewed direct Foundry or APIM token scope    | No     | Token audience                 | AI/gateway slice   |
| `OPTIMA_FOUNDRY_MANAGED_IDENTITY_CLIENT_ID`         | API identity client ID                         | No     | Selects user-assigned identity | IaC                |
| `OPTIMA_COSMOS_ENDPOINT`                            | Cosmos `documentEndpoint`                      | No     | Not applicable                 | IaC                |
| `OPTIMA_COSMOS_DATABASE_NAME`                       | Literal `optima`                               | No     | Not applicable                 | IaC                |
| `OPTIMA_COSMOS_CONTAINER_NAME`                      | Literal `runs`                                 | No     | Not applicable                 | IaC                |
| `OPTIMA_COSMOS_AUTH_MODE`                           | Literal `MANAGED_IDENTITY`                     | No     | Replaces account key           | IaC                |
| `OPTIMA_COSMOS_MANAGED_IDENTITY_CLIENT_ID`          | API identity client ID                         | No     | Selects user-assigned identity | IaC                |
| `OPTIMA_COSMOS_TIMEOUT_SECONDS`                     | Literal `10`                                   | No     | Not applicable                 | IaC                |
| `OPTIMA_REDIS_HOST`                                 | Cache-enabled only: derived Managed Redis hostname | No  | Not applicable                 | IaC                |
| `OPTIMA_REDIS_INDEX_NAME`                           | Cache-enabled only: literal `optima-cache-v1`  | No     | Not applicable                 | IaC/data bootstrap |
| `OPTIMA_REDIS_EMBEDDING_DIMENSION`                  | Cache-enabled only: reviewed embedding parameter | No   | Not applicable                 | AI/cache slice     |
| `OPTIMA_REDIS_EMBEDDING_MODEL`                      | Cache-enabled only: reviewed embedding parameter | No   | Not applicable                 | AI/cache slice     |
| `OPTIMA_REDIS_EMBEDDING_DEPLOYMENT`                 | Cache-enabled only: reviewed embedding parameter | No   | Not applicable                 | AI/cache slice     |
| `OPTIMA_REDIS_AUTH_MODE`                            | Cache-enabled only: literal `MANAGED_IDENTITY` | No     | Replaces access key            | IaC                |
| `OPTIMA_REDIS_OBJECT_ID`                            | Cache-enabled only: API identity object ID     | No     | Redis AUTH username            | IaC                |
| `OPTIMA_REDIS_MANAGED_IDENTITY_CLIENT_ID`           | Cache-enabled only: API identity client ID     | No     | Selects user-assigned identity | IaC                |
| `OPTIMA_APPLICATION_INSIGHTS_ENABLED`               | Literal `true`                                 | No     | Not applicable                 | IaC                |
| `OPTIMA_APPLICATION_INSIGHTS_CONNECTION_STRING`     | Container Apps secret reference                | Yes    | None in current exporter       | IaC                |
| `OPTIMA_APPLICATION_INSIGHTS_SERVICE_NAME`          | Literal `optima-api`                           | No     | Not applicable                 | IaC                |
| `OPTIMA_APPLICATION_INSIGHTS_DEPLOYMENT_ENVIRONMENT`| Literal `hackathon`                             | No     | Not applicable                 | IaC                |
| `OPTIMA_APPLICATION_INSIGHTS_SAMPLING_RATIO`        | Environment parameter, default `0.25`          | No     | Not applicable                 | IaC/operations     |

Live Metrics, performance counters, and offline storage are explicitly `false`
to preserve the Slice 10D isolation and cost contract. Existing module flags,
quality thresholds, planner thresholds, Redis timeouts, Redis connection bounds,
Cosmos retry limits, and history list limits retain their typed application
defaults unless benchmark calibration supplies an explicit override.

No account key, Redis access key, Foundry API key, password, subscription
credential, or tenant credential is present in a parameter file or template
output. The generated Application Insights destination uses secure module
transport and a Container Apps secret reference and is not exposed as a root
output.

## Runtime identity model

`infra/modules/runtime-access.bicep` defines the OPTIMA-owned runtime grants.
They are conditional because creating the ACR assignments requires access
administration that the routine Contributor deployment identity does not have.
Set `deployRuntimeAccess=true` only for a reviewed access-bootstrap deployment.

| Identity             | Resource                      | Required access                                    | Scope                            | Reason                                 |
|----------------------|-------------------------------|----------------------------------------------------|----------------------------------|----------------------------------------|
| API managed identity | ACR                           | `AcrPull` (`7f951dda-4ed3-4680-a7ca-43fe172d538d`) | OPTIMA registry                  | Pull the API image                     |
| API managed identity | Cosmos DB                     | Cosmos DB Built-in Data Contributor (`...0002`)    | `/dbs/optima/colls/runs`         | Create/read immutable runs and query   |
| API managed identity | Managed Redis database, cache-enabled only | Stable `default` access-policy assignment | Managed Redis `default` database | Authenticate cache operations |
| API managed identity | Foundry Azure OpenAI resource | Cognitive Services OpenAI User                     | Exact Foundry/OpenAI resource    | Invoke model and embedding deployments |
| UI managed identity  | ACR                           | `AcrPull` (`7f951dda-4ed3-4680-a7ca-43fe172d538d`) | OPTIMA registry                  | Pull the UI image                      |
| UI managed identity  | Cosmos, Redis, Foundry        | None                                               | None                             | UI calls only the internal API         |

The Cosmos role definition ID is
`00000000-0000-0000-0000-000000000002`; its role-definition resource is under
the Cosmos account. The assignment should use the most granular relative scope
`/dbs/optima/colls/runs`.

Managed Redis does not use an Azure RBAC data role. It uses
`Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments`. The stable
`2025-07-01` API supports only the `default` policy, which grants full Redis
data-plane access. Fine-grained access strings require a preview API and are
not supported on instances with modules such as RediSearch enabled. The API
identity is therefore isolated from the UI but is not command-scoped within
Redis. Revisit this residual risk when Azure Managed Redis supports custom ACLs
with the required search capability.

The API managed identity performs index inspection and creation during startup.
The reviewed stable Redis `default` policy remains broader than the desired
command-level access because custom ACLs are incompatible with RediSearch.

## GitHub OIDC design

The preferred deployment identity is one dedicated user-assigned managed
identity, not a secret-bearing Entra application registration.

| Property           | Proposed value                                |
|--------------------|-----------------------------------------------|
| Repository         | `sekharrcs/optima`                            |
| GitHub environment | `hackathon`                                   |
| Issuer             | `https://token.actions.githubusercontent.com` |
| Subject            | `repo:sekharrcs/optima:environment:hackathon` |
| Audience           | `api://AzureADTokenExchange`                  |
| Azure identity     | Dedicated deployment user-assigned identity   |
| Authentication     | Workload identity federation / OIDC           |

The later GitHub environment should hold subscription ID, tenant ID, and client
ID as non-secret variables. OIDC creates no client secret.

### Deployment RBAC

| Phase                           | Role                     | Scope                     | Why                                    |
|---------------------------------|--------------------------|---------------------------|----------------------------------------|
| One-time subscription bootstrap | Contributor              | Target subscription       | Create resource group and resources    |
| Routine infrastructure deploy   | Contributor              | `rg-optima-hackathon`     | Deploy OPTIMA resources                |
| Runtime access bootstrap        | Contributor + RBAC admin | Resource group + registry | Create finite runtime assignments      |
| Image build and publish         | `AcrPush`                | OPTIMA registry           | Push immutable API and UI images       |

`AcrPush` has role ID `8311e382-0749-4cb8-b61a-304f252e45ec`.
Subscription Contributor is temporary. First deploy resources with
`deployRuntimeAccess=false`. Then a reviewed bootstrap principal with
Contributor on the resource group and Role Based Access Control Administrator
on the exact registry can rerun `infra/resource-group.bicep` with
`deployRuntimeAccess=true`. Cosmos and Redis assignments are native child
resources covered by Contributor; only the two ACR assignments require Azure
RBAC administration.

Do not grant Owner, User Access Administrator, or Role Based Access Control
Administrator to the routine GitHub deployment identity. After bootstrap,
return `deployRuntimeAccess` to `false`. The Foundry resource owner must grant
Cognitive Services OpenAI User on the exact external resource. The image-build
identity owner must grant `AcrPush` on the exact registry. These external grants
cannot be derived safely from the endpoint-only OPTIMA parameters.

## Resource providers

Confirm these namespaces are registered before deployment. Slice 11A does not
register them.

| Namespace                       | Template resource                                   |
|---------------------------------|-----------------------------------------------------|
| `Microsoft.Resources`           | Resource group and deployments                      |
| `Microsoft.ManagedIdentity`     | API and UI user-assigned managed identities         |
| `Microsoft.ContainerRegistry`   | Azure Container Registry                            |
| `Microsoft.App`                 | Container Apps environment and apps                 |
| `Microsoft.DocumentDB`          | Cosmos DB account, database, and container          |
| `Microsoft.Cache`               | Cache-enabled only: Azure Managed Redis cluster and database |
| `Microsoft.OperationalInsights` | Log Analytics workspace                             |
| `Microsoft.Insights`            | Application Insights component                      |

`Microsoft.Authorization` is needed when the reviewed runtime-access bootstrap
enables the two ACR role assignments. `Microsoft.CognitiveServices` is needed
only if a later slice deploys Foundry/OpenAI resources. `Microsoft.KeyVault` is
not required by this architecture.

## Cost assessment

The figures below are a 2026-08-26 reference snapshot in INR. Slice 11C must
refresh applicable East US 2 meters before deployment. They are not a quote. Enterprise
agreement discounts, taxes, free grants, changed meters, data transfer, and
actual usage can change the bill. Recheck pricing before deployment.

| Resource                  | Selected tier/configuration             | Billing behavior and reference driver                    | Cost posture                                | Expensive alternative avoided                 |
|---------------------------|-----------------------------------------|----------------------------------------------------------|---------------------------------------------|-----------------------------------------------|
| ACR                       | Basic, 10 GiB included                  | ₹15.9353/day; extra storage ₹9.565/GB-month              | About ₹478-₹494 for a 30-31 day month       | Standard/Premium and geo-replication          |
| Managed Redis             | Omitted in selected profile; enabled contract remains Balanced B0, HA off | Prior reference was ₹1.5304 per cache hour | Saves about ₹1,117 for 730 hours while disabled | HA replica, B1+, persistence, geo-replication |
| Cosmos DB                 | Serverless NoSQL                        | ₹23.9125/million RUs; ₹23.9125/GB-month data storage    | No idle throughput charge                  | Provisioned or autoscale RU/s                  |
| Cosmos backup             | Periodic local redundancy               | Backup beyond included allowance at LRS storage meter    | Storage-driven and expected to be small    | Continuous PITR and geo-redundant backup      |
| Container Apps            | Consumption, two 0.5-vCPU/1-GiB apps   | Active vCPU/GiB seconds and requests; zero at scale zero | Low for intermittent use; high if sustained| Dedicated profiles and minimum replicas       |
| Log Analytics/App Insights| PerGB2018, 30 days, 25% trace sampling  | ₹219.995/GB analyzed; workspace cap 0.25 GB/day          | Normal use should remain below the cap     | Commitment tier and duplicate diagnostics    |
| Managed identities        | Two user-assigned identities            | No separate runtime meter                                | No direct charge                           | Secret-bearing service credentials            |
| Container Apps environment| Consumption-only, no VNet               | No dedicated workload-profile floor                      | No fixed compute floor                     | Dedicated environment and network stack       |

The prior approximately ₹3,250 fixed estimate included about ₹1,117 per month
for Balanced B0. Omitting Redis produces a comparable estimate of approximately
₹2,133 per month before tax. At the full workspace cap, ordinary Log Analytics
ingestion is approximately ₹1,650 for 30 days, although cap overshoot can be
billed. Intermittent scale-to-zero usage should remain within the ₹5,000 fixed
infrastructure target, but Container Apps and telemetry remain variable risks.

The architecture is designed to fit the ₹5,000 infrastructure target, but it is
not safe under sustained Container Apps load or sustained telemetry at the cap.
Container Apps active duration and telemetry ingestion are the main surprise
cost risks. The overall ₹12,500 budget leaves the remainder for model calls and
Slice 12 benchmarks; Foundry model usage is intentionally excluded from the
infrastructure estimate.

## Deployment safety

Names are stable across reruns. Global names append
`uniqueString(subscription().subscriptionId, environmentName)`, which produces
a deterministic opaque suffix without exposing the subscription ID. No timestamp
or random value participates in naming.

The templates use incremental deployment semantics when invoked normally. They
do not own unrelated subscription resources, define deletion scripts, use
deployment stacks, emit credentials, or create wildcard role assignments. The
optional runtime assignments use deterministic names and exact resource scopes.
Resource deletion must be a separate reviewed action.

Container Apps deployment is gated by `deployContainerApps=false`. Before
setting it to `true`, all of these conditions must hold:

1. The exact commit passed the Slice 11B-S read-only Linux AMD64 workflow,
  including API and UI runtime smoke tests, non-root and native checks, rootfs
  inspection, separate final-image SBOM generation, vulnerability policy, and
  secret scanning.
2. Slice 11C published the API and UI images to ACR, and both exist at the
  configured immutable registry manifest digests. Local Docker image IDs are not
  registry manifest digests.
3. The API image exposes a production entry point that composes all active
  providers, evaluator, cost catalog, store, and lifecycle owners.
4. The API lifespan flushes and closes observability, Foundry transports, and
  Cosmos resources. It also closes Redis and embedding resources when enabled.
5. OPTIMA runtime access was bootstrapped with `deployRuntimeAccess=true`, then
  returned to `false`; external Foundry access was applied and verified. Disabled
  mode creates no Redis assignment.
6. Cache-enabled mode proves Redis bootstrap can inspect or create
  `optima-cache-v1` with the reviewed profile.
7. Placeholder Foundry parameters have been replaced. Cache-enabled mode also
  replaces embedding parameters.
8. The production API health endpoint and UI startup pass with live configuration.
9. Placeholder UI Entra client and tenant IDs have been replaced, a client secret
  is supplied through the secure `uiAuthClientSecret` parameter, and the exact
  `/.auth/login/aad/callback` URI is registered.
10. Live Entra checks confirm authorized and unauthorized behavior, Streamlit
   access, API internality, acceptable session and logout behavior, secret
   non-disclosure, and explicit user restriction or application assignment.

Exact-head workflow evidence is valid only for the commit recorded by that run.
No hosted result, ACR publication, Azure deployment, production authentication,
or live authorization result is claimed here.

## Production deployment flow

The Slice 11B-S workflow is a separate pre-merge path. It runs untrusted
pull-request code with `contents: read`, no persisted checkout credential, no
secret or Azure identity, and no deployment command. Its Docker socket reaches
only the ephemeral hosted runner. Slice 11C adds a distinct manually dispatched,
protected `hackathon` environment path. See the
[production deployment runbook](PRODUCTION_DEPLOYMENT.md) for exact configuration,
preflight, verification, and rollback procedures.

```text
GitHub Actions
      |
      v
OIDC federated deployment identity
      |
      v
Azure Resource Manager
      |
      +-- first bootstrap: subscription-scope main.bicep
  |      +-- create the managed environment without public apps
  |      +-- report exact UI callback and stop until it is registered
      |
      +-- routine: resource-group.bicep
      |
      +-- reviewed access bootstrap: deployRuntimeAccess=true
      |
      v
Build and push immutable API/UI images
      |
      v
Enable Container Apps after access and runtime gates pass
```

The deployment workflow validates application code, Bicep, the Slice 11C diff,
and one exact Linux AMD64 image pair before Azure login. It transfers those
scanned image objects to the OIDC job without rebuilding. Read-only preflight
proves OIDC, active model/version bindings, reviewed pricing, cost posture, Entra
configuration, ACR permissions, runtime assignments, registry manifests, and the
explicit cache mode. Disabled mode proves Redis resources and embedding values
are absent. Enabled mode additionally proves regional Redis Enterprise
advertisement, exact subscription SKU advertisement, applicable restrictions,
and Redis quota-exposure status. Redis Enterprise currently exposes no documented
regional quota operation. `NOT_EXPOSED` is recorded as unknown rather than
available and does not independently block when no other gate proves
ineligibility. Metadata never proves physical allocation; an `AllocationFailed`
response from an exact deployment remains authoritative and blocks without a
region or SKU fallback. API and UI deploy from separate registry digests
produced from one source commit. The first app deployment keeps the UI internal
until the Easy Auth child is verified; failed rollouts close UI ingress and
reactivate the captured prior revision pair.

A successful Slice 11B-S run may make PR #20 merge-ready for its exact head. It
does not authorize merge and cannot declare Slice 11C deployed or successful.

## Risks and deployment blockers

* The 2026-09-01 subscription SKU response contained no exact East US 2
  `Balanced_B0` entry, although provider metadata advertised `redisEnterprise`
  in the region. Cache-enabled production remains blocked. The selected disabled
  profile does not bypass this gate because it cannot provision or contact Redis.
* Redis Enterprise exposes no documented authoritative regional quota API.
  Preflight reports this as unknown and relies on explicit restrictions plus the
  exact provider allocation response; it never calls the unsupported
  `Microsoft.Cache/locations/{location}/usages` route when provider metadata does
  not advertise it.
* Foundry resource, SMALL, STRONG, and JUDGE deployment names, quotas, and model
  pricing are unresolved. Embedding inputs remain required before restoring cache.
* Reference-free `LLM_JUDGE` composition is implemented but not deployed. The
  first live deployment still requires reviewed judge identity, pricing, access,
  and JSON response-format validation.
* The production price catalog is intentionally empty until reviewed deployment
  rates are supplied, so monetary cost remains unavailable rather than fabricated.
* Cache-enabled mode retains the stable Redis access-policy and RediSearch
  bootstrap risks. They are unreachable in the selected disabled profile.
* The UI Entra app registration and user-assignment policy are external tenant
  prerequisites. Placeholder IDs block Container Apps deployment.
* The shared backend history endpoints remain internal and have no per-user
  ownership model. The public UI exposes only session-local history.
* A successful hosted exact-head Slice 11B-S final-image build and scan remains
  mandatory before deployment; no hosted result is recorded yet.
* Scale-to-zero introduces API/UI cold starts and may affect demo latency.
* The Log Analytics cap is an emergency brake, not a precise billing limit.
