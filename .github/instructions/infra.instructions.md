---
description: "Azure infrastructure and deployment conventions for OPTIMA"
applyTo: "infra/**/*.bicep,infra/**/*.json,azure.yaml,.github/workflows/**/*.yml"
---
# Azure infrastructure instructions

Use Bicep for Azure infrastructure.
Use Azure Developer CLI conventions where practical.
Prefer managed identities over secrets.
Use least-privilege RBAC.
Make resource names parameterized.
Do not hard-code subscription IDs, tenant IDs, credentials, regions, or model deployment names.

Separate expensive/optional services behind parameters where possible so local and low-cost hackathon environments remain usable.

Deployment automation must:
- build/test before deployment
- fail fast on errors
- never echo secrets
- expose application URLs as deployment outputs
