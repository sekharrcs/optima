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
> environments, or GitHub secrets.

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

## Repository findings

The current application already defines the Azure data-plane contracts. The
infrastructure preserves them rather than introducing replacement services.

| Integration          | Repository contract                                                                 | Startup behavior                                  |
|----------------------|--------------------------------------------------------------------------------------|---------------------------------------------------|
| FastAPI API          | Factory `optima.api.production:create_production_app`, port `8000`, health at `/api/v1/health` | Lifespan composes Azure resources before readiness |
| Streamlit UI         | `streamlit run src/ui/app.py`, port `8501`, `OPTIMA_API_BASE_URL`                    | Starts without an available API                   |
| Foundry/APIM         | HTTPS Azure OpenAI v1 root ending `/openai/v1`; SMALL, STRONG, JUDGE, and embedding names | First model, judge, or embedding request performs I/O |
| Cosmos DB            | NoSQL API, database and container supplied by settings, partition key `/id`          | Client is lazy; database and container must exist |
| Azure Managed Redis  | TLS port `10000`, RESP2, RediSearch HASH index, `FLOAT32`/COSINE, read-only lookup    | Lifespan validates or creates the index idempotently |
| Application Insights | Workspace-based connection string, local OpenTelemetry providers, explicit close     | Failure-isolated initialization during app build  |

The default FastAPI export remains intentionally unconfigured for library and
local health use. The production factory validates all Azure settings, composes
Foundry generator and optional JUDGE resources, mode-selected evaluation,
centralized pricing, context reduction, Redis, Cosmos, and observability, then
owns cleanup through FastAPI lifespan. Separate API and UI image definitions now
exist. `deployContainerApps` remains `false` until Slice 11C publishes immutable
images and completes live access and regional preflight.

## Azure topology

```text
GitHub Actions (later slice)
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
                +-- Azure Managed Redis Balanced B0
                +-- Log Analytics workspace
                +-- workspace-based Application Insights
                +-- API user-assigned managed identity
                +-- UI user-assigned managed identity
                +-- optional OPTIMA runtime access assignments

OPTIMA API --managed identity--> Foundry or APIM endpoint (external input)
OPTIMA API --managed identity--> Cosmos DB
OPTIMA API --managed identity--> Azure Managed Redis
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
| Azure Managed Redis            | Balanced B0, HA disabled, TLS 1.2, public endpoint, access keys disabled               | Host semantic-cache evidence                 |
| Redis database                 | Port `10000`, Enterprise clustering, NoEviction, RediSearch, no disk persistence       | Support filtered `FT.SEARCH` vector lookup   |
| Log Analytics workspace        | PerGB2018, 30-day immediate purge, 0.25 GB/day emergency cap                          | Store Application Insights telemetry         |
| Application Insights           | Workspace-based, public ingestion, 30 days, local ingestion auth retained             | Preserve Slice 10D traces and metrics        |
| API managed identity           | User-assigned                                                                          | Stable pre-assignable API runtime identity   |
| UI managed identity            | User-assigned                                                                          | Pull only the UI image from ACR               |

The API and UI remain separate deployment units because the UI is an HTTP
client of the API, they use different ports and processes, and only the API
needs model, Cosmos, Redis, and telemetry configuration. The API has internal
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
| `OPTIMA_REDIS_HOST`                                 | Derived Managed Redis hostname                 | No     | Not applicable                 | IaC                |
| `OPTIMA_REDIS_INDEX_NAME`                           | Literal `optima-cache-v1`                      | No     | Not applicable                 | IaC/data bootstrap |
| `OPTIMA_REDIS_EMBEDDING_DIMENSION`                  | Reviewed embedding parameter                   | No     | Not applicable                 | AI/cache slice     |
| `OPTIMA_REDIS_EMBEDDING_MODEL`                      | Reviewed embedding parameter                   | No     | Not applicable                 | AI/cache slice     |
| `OPTIMA_REDIS_EMBEDDING_DEPLOYMENT`                 | Reviewed embedding parameter                   | No     | Not applicable                 | AI/cache slice     |
| `OPTIMA_REDIS_AUTH_MODE`                            | Literal `MANAGED_IDENTITY`                     | No     | Replaces access key            | IaC                |
| `OPTIMA_REDIS_OBJECT_ID`                            | API identity principal/object ID               | No     | Redis AUTH username            | IaC                |
| `OPTIMA_REDIS_MANAGED_IDENTITY_CLIENT_ID`           | API identity client ID                         | No     | Selects user-assigned identity | IaC                |
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
| API managed identity | Managed Redis database        | Stable `default` access-policy assignment           | Managed Redis `default` database | Authenticate cache operations          |
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
| `Microsoft.Cache`               | Azure Managed Redis cluster and database            |
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
| Managed Redis             | Balanced B0, HA off, 0.5 GB             | ₹1.5304 per cache hour                                   | About ₹1,117 for 730 hours                  | HA replica, B1+, persistence, geo-replication |
| Cosmos DB                 | Serverless NoSQL                        | ₹23.9125/million RUs; ₹23.9125/GB-month data storage    | No idle throughput charge                  | Provisioned or autoscale RU/s                  |
| Cosmos backup             | Periodic local redundancy               | Backup beyond included allowance at LRS storage meter    | Storage-driven and expected to be small    | Continuous PITR and geo-redundant backup      |
| Container Apps            | Consumption, two 0.5-vCPU/1-GiB apps   | Active vCPU/GiB seconds and requests; zero at scale zero | Low for intermittent use; high if sustained| Dedicated profiles and minimum replicas       |
| Log Analytics/App Insights| PerGB2018, 30 days, 25% trace sampling  | ₹219.995/GB analyzed; workspace cap 0.25 GB/day          | Normal use should remain below the cap     | Commitment tier and duplicate diagnostics    |
| Managed identities        | Two user-assigned identities            | No separate runtime meter                                | No direct charge                           | Secret-bearing service credentials            |
| Container Apps environment| Consumption-only, no VNet               | No dedicated workload-profile floor                      | No fixed compute floor                     | Dedicated environment and network stack       |

The predictable ACR and Redis floor is approximately ₹1,600/month. At the full
workspace cap, ordinary Log Analytics ingestion is approximately ₹1,650 for 30
days, although cap overshoot can be billed. That leaves roughly ₹1,750 of the
₹5,000 infrastructure target for Container Apps, Cosmos requests/storage, and
small variable charges. Intermittent scale-to-zero usage should remain below
that remainder.

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

1. API and UI Linux AMD64 images exist at the configured immutable manifest digests.
2. The API image exposes a production entry point that composes all required
   providers, evaluator, cost catalog, store, cache, and lifecycle owners.
3. The API lifespan flushes and closes observability, Foundry transports,
   Cosmos resources, Redis renewal/client resources, and embedding resources.
4. OPTIMA runtime access was bootstrapped with `deployRuntimeAccess=true`, then
  returned to `false`; external Foundry access was applied and verified.
5. Redis bootstrap can inspect or create `optima-cache-v1` with the reviewed profile.
6. Placeholder Foundry and embedding parameters have been replaced.
7. The API health endpoint and UI startup have passed container smoke tests.
8. Placeholder UI Entra client and tenant IDs have been replaced, ID-token
  issuance is enabled, and the exact `/.auth/login/aad/callback` URI is registered.
9. Both final images have zero unresolved applicable high or critical scanner
  findings and contain their generated CycloneDX SBOMs.

## Future deployment flow

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

No workflow is implemented in Slice 11A or Slice 11B.

## Risks and deployment blockers

* Foundry resource, SMALL, STRONG, JUDGE, and embedding deployment names, quotas,
  and model pricing are unresolved.
* Reference-free `LLM_JUDGE` composition is implemented but not deployed. The
  first live deployment still requires reviewed judge identity, pricing, access,
  and JSON response-format validation.
* The production price catalog is intentionally empty until reviewed deployment
  rates are supplied, so monetary cost remains unavailable rather than fabricated.
* The stable Redis access policy grants the API identity full Redis data-plane
  access; custom module-compatible ACLs are unavailable and remain a residual
  risk.
* RediSearch bootstrap requires authenticated live validation on first startup.
* B0 Redis availability in East US 2 and subscription quota must be confirmed
  before deployment.
* The UI Entra app registration and user-assignment policy are external tenant
  prerequisites. Placeholder IDs block Container Apps deployment.
* The shared backend history endpoints remain internal and have no per-user
  ownership model. The public UI exposes only session-local history.
* Final API and UI image builds and scans remain mandatory before deployment.
* Scale-to-zero introduces API/UI cold starts and may affect demo latency.
* The Log Analytics cap is an emergency brake, not a precise billing limit.
