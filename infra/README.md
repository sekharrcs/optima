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
keep `deployContainerApps=false`. These values are deliberate deployment gates.

## Modules

| Module                     | Responsibility                                   |
|----------------------------|--------------------------------------------------|
| `identities.bicep`         | API and UI user-assigned managed identities      |
| `container-registry.bicep` | Basic ACR without admin credentials               |
| `monitoring.bicep`         | Log Analytics and Application Insights           |
| `cosmos.bicep`             | Serverless NoSQL account, database, and container|
| `managed-redis.bicep`      | B0 Redis and RediSearch-capable database          |
| `container-apps.bicep`     | Gated API/UI Container Apps definitions           |

No module defines Azure RBAC assignments, Cosmos SQL role assignments, Managed
Redis access-policy assignments, deployment scripts, federated credentials, or
provider registration.

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
