---
title: OPTIMA Azure Infrastructure
description: Offline validation and deployment boundaries for the Slice 11A Bicep foundation
---

# OPTIMA Azure Infrastructure

> [!IMPORTANT]
> Slice 11A does not perform an Azure deployment. Do not run deployment commands
> until the architecture, costs, identity bootstrap, and application blockers in
> [the Azure infrastructure design](../docs/AZURE_INFRASTRUCTURE.md) are reviewed.

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
