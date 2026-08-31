---
title: OPTIMA Azure Infrastructure
description: Validation, deployment entry points, and safety boundaries for OPTIMA Azure resources
---

# OPTIMA Azure Infrastructure

> [!IMPORTANT]
> Slices 11A and 11B did not perform an Azure deployment. Use the manual protected
> workflow and [production deployment runbook](../docs/PRODUCTION_DEPLOYMENT.md)
> for Slice 11C. Do not bypass its preflight, what-if, access, or immutable-image
> gates with ad hoc deployment commands.

## Entry points

* `main.bicep` is the one-time subscription-scope bootstrap. It creates
  `rg-optima-hackathon` and delegates to the resource-group composition.
* `resource-group.bicep` is the routine deployment entry point after the resource
  group exists and the GitHub deployment identity is scoped to that group.
* `environments/hackathon.bicepparam` targets the subscription bootstrap.
* `environments/hackathon.runtime.bicepparam` targets routine resource-group
  deployments.

Both parameter files contain non-deployable model and image placeholders. Both
keep `deployContainerApps=false` and `deployRuntimeAccess=false`. These values
are deliberate deployment gates.

The Container Apps environment is part of foundation convergence even while
`deployContainerApps=false`. The API, UI, and UI authentication resources remain
conditional. This allows the first reviewed run to discover the environment
default domain and stop before public exposure while the exact Entra callback is
registered.

Application resources target East US 2 (`eastus2`). The existing bootstrap
resource group and deployment identity may remain in East US because they are
outside the application resource group template. API and UI image parameters
are separate manifest digests. Slice 11C must replace the all-zero digest
placeholders only after the corresponding ACR manifests exist.

## Modules

| Module                     | Responsibility                                   |
|----------------------------|--------------------------------------------------|
| `identities.bicep`         | API and UI user-assigned managed identities      |
| `container-registry.bicep` | Basic ACR without admin credentials               |
| `monitoring.bicep`         | Log Analytics and Application Insights           |
| `cosmos.bicep`             | Serverless NoSQL account, database, and container|
| `managed-redis.bicep`      | B0 Redis and RediSearch-capable database          |
| `runtime-access.bicep`     | Conditional ACR, Cosmos, and Redis runtime grants |
| `container-apps.bicep`     | Gated API/UI Container Apps definitions           |

The runtime-access module uses deterministic names and exact scopes. It grants
`AcrPull` to the API and UI identities, Cosmos data contribution to the API on
`optima/runs`, and the stable Redis `default` policy to the API. It is disabled
for routine deployments because its ACR assignments require a reviewed
principal with Role Based Access Control Administrator on the exact registry.
Foundry access, image-publisher `AcrPush`, deployment scripts, federated
credentials, and provider registration remain outside this template graph.

The Application Insights connection string is ordinary destination
configuration, not a security token. It is passed directly to the API app and
is not exposed as a root output.

## Offline validation

With Bicep CLI installed, compile every entry point and parameter file:

```powershell
bicep build infra/main.bicep --stdout | Out-Null
bicep build infra/resource-group.bicep --stdout | Out-Null
bicep build-params infra/environments/hackathon.bicepparam --stdout | Out-Null
bicep build-params infra/environments/hackathon.runtime.bicepparam --stdout | Out-Null
```

Compilation reads local files only. It does not prove regional SKU availability,
subscription quota, provider registration, RBAC, data-plane access, image
availability, or model availability.

Slice 11C must preflight `Microsoft.Cache` registration, Azure Managed Redis
availability in `eastus2`, Balanced B0 SKU support, and relevant subscription
quota before deployment. Regional allocation can still fail after metadata and
quota checks pass. That failure must stop deployment; no automatic fallback to
East US or another region is allowed.
