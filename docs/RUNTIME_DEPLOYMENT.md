---
title: OPTIMA Runtime and Container Deployment Contract
description: Slice 11B production composition, lifecycle, Redis bootstrap, container entrypoints, and Slice 11C handoff
---

## Slice ownership

Slice 11B owns:

* Production FastAPI dependency composition
* FastAPI lifespan construction and cleanup
* API and UI image definitions
* Redis index inspection, compatibility validation, and creation
* East US 2 application parameters
* Runtime environment-variable and immutable image contracts

Slice 11C owns:

* GitHub Actions and OIDC login usage
* API and UI image publication to ACR
* Azure preflight and Bicep execution
* Runtime and external Foundry access application
* First deployment and live smoke testing

Neither slice may silently select another Azure region.

## Production application

The API image starts:

```text
uvicorn optima.api.production:create_production_app --factory --host 0.0.0.0 --port 8000
```

The factory validates production settings before opening resources. It never
falls back to fake or demo dependencies. The lightweight
`optima.api.app:app` and deterministic `optima.api.demo:app` remain explicit
local entry points.

Production selects the reviewed `EXACT_REFERENCE` evaluator and requires
`reference_output`. The API rejects a missing reference with structured HTTP
422 before cache lookup, model calls, or evaluator calls. Container Apps also
sets `OPTIMA_REQUIRE_REFERENCE_OUTPUT=true` for the Streamlit form.

Construction proceeds in this order:

1. Application Insights observability
2. Shared Foundry SMALL and STRONG provider resources
3. Foundry embedding resources
4. Azure Managed Redis resources
5. Redis index bootstrap
6. Cosmos run-history resources
7. Immutable execution dependencies

Shutdown proceeds in reverse resource order: Cosmos, Redis, embedding, Foundry,
then telemetry. Each runtime closes once. Cleanup failures are recorded by type
and do not skip later cleanup or mask an earlier startup failure.

The API health endpoint is `/api/v1/health`. Uvicorn does not accept traffic
until FastAPI lifespan yields, so an index or configuration failure prevents a
false ready response.

## Redis index bootstrap

Startup inspects `FT._LIST`. If `optima-cache-v1` is absent and no stale contract
hash exists, it creates this schema:

```text
FT.CREATE optima-cache-v1 ON HASH PREFIX 1 optima:semantic-cache: SCHEMA
  schema_version TAG
  embedding_profile TAG
  task_type TAG
  complexity TAG
  embedding VECTOR FLAT 6 TYPE FLOAT32 DIM <dimension> DISTANCE_METRIC COSINE
```

The companion hash is
`optima:semantic-cache-index-contract:optima-cache-v1`. It binds the index name,
cache schema version, embedding-profile schema, semantic-input policy, and exact
embedding-profile identity.

An existing index is a no-op only when `FT.INFO` and the companion hash match.
Missing fields, a different dimension or metric, a stale profile, an unknown
schema, or a stale contract fails startup. Bootstrap never invokes
`FT.DROPINDEX`, replaces an index, or deletes user/cache data.

Replica startup is coordinated by a bounded Redis `SET NX EX` lock. Followers
reinspect until the creator writes the contract, then validate it normally.
Lock release uses compare-and-delete Lua, so a replica cannot release a lock it
no longer owns. A stale index without a contract fails after proving no active
creator owns the lock.

The stable Azure Managed Redis `default` policy grants broader data-plane access
than OPTIMA needs. RediSearch is incompatible with the intended fine-grained
custom ACL model. Slice 11B preserves this reviewed tradeoff.

## Container images

`Dockerfile.api` exposes port `8000`. `Dockerfile.ui` exposes port `8501` and
starts `streamlit run src/ui/app.py`. Both use Python 3.12.12 slim, install from
`uv.lock` with `uv 0.12.5`, exclude development dependencies, and run as UID and
GID `10001`. The Docker context is an allow list containing only
`pyproject.toml`, `uv.lock`, and `src`.

Build and smoke test locally when Docker is available:

```powershell
docker build --file Dockerfile.api --tag optima-api:local .
docker build --file Dockerfile.ui --tag optima-ui:local .
docker run --rm --env-file .env -p 8000:8000 optima-api:local
docker run --rm -e OPTIMA_API_BASE_URL=http://host.docker.internal:8000 -p 8501:8501 optima-ui:local
```

Slice 11C publishes each image and supplies its manifest digest as
`apiImageDigest` or `uiImageDigest`. The all-zero parameter values are
non-deployable placeholders. `deployContainerApps` remains `false` by default.
Enabling it with an all-zero value or anything other than lowercase `sha256:`
plus 64 hexadecimal characters triggers a Bicep `fail()` guard before Container
Apps composition. Slice 11C still verifies exact manifest existence in ACR.

## Runtime configuration

Container Apps owns `OPTIMA_DEPLOYMENT_ENVIRONMENT`, managed-identity IDs,
Cosmos resource values, Redis endpoint values, Application Insights values, and
the UI API URL. The AI/model owner supplies the Foundry endpoint and deployment
identities.

Required external Foundry values are:

* HTTPS Azure OpenAI v1 root ending in `/openai/v1`
* SMALL chat-completions deployment name
* STRONG chat-completions deployment name
* Embedding deployment name and provider-reported model identity
* Exact embedding vector dimension
* Token scope, normally `https://cognitiveservices.azure.com/.default`
* Cognitive Services OpenAI User for the API identity on the exact Foundry resource

Production Managed Identity mode requires the API user-assigned client ID for
Foundry, Cosmos, and Redis. Redis additionally requires the identity principal
ID as its AUTH username. Redis uses fixed Azure Managed Redis TLS port `10000`.

Application Insights must be enabled with its connection string, service name,
deployment environment, and sampling ratio. Live Metrics, performance counters,
and offline storage remain disabled.

## East US 2 preflight

Before Bicep execution, Slice 11C must validate as far as Azure APIs allow:

1. `Microsoft.Cache` resource-provider registration
2. Azure Managed Redis service availability in `eastus2`
3. Balanced B0 SKU support in `eastus2`
4. Relevant subscription quota and limits
5. Presence of immutable API and UI manifests in ACR
6. Replacement of every Foundry, embedding, and digest placeholder

Valid SKU metadata and quota do not guarantee regional allocation capacity.
Allocation failure must stop deployment with a clear error. It must not place
Redis in East US or another fallback region.

## Remaining product inputs

The current production-safe evaluator is exact-reference. Reference-free
requests are rejected before paid work. A separately reviewed natural-language
evaluator is still required before the production contract can accept
reference-free demo requests.

The runtime central cost calculator currently uses an empty versioned catalog.
Token usage remains measured, but monetary costs are unavailable until reviewed
SMALL, STRONG, and embedding rates for provider `microsoft-foundry-apim` are
supplied through an approved pricing contract. OPTIMA does not fabricate rates.