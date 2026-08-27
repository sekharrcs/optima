---
title: OPTIMA Runtime and Container Deployment Contract
description: Production composition, evaluator modes, lifecycle, container entrypoints, and Slice 11C handoff
---

## Slice ownership

Slice 11B owns:

* Production FastAPI dependency composition
* FastAPI lifespan construction and cleanup
* API and UI image definitions and dependency SBOMs
* Public UI Entra authentication configuration
* Request-size, execution-time, and per-process concurrency limits
* Redis index inspection, compatibility validation, and creation
* East US 2 application parameters
* Runtime environment-variable and immutable image contracts

Slice 11C owns:

* GitHub Actions and OIDC login usage
* API and UI image publication to ACR
* Azure preflight and Bicep execution
* Runtime and external Foundry access application
* First deployment and live smoke testing
* Reviewed SMALL, STRONG, JUDGE, and embedding pricing inputs when monetary cost
  measurement is required

Slice 11E owns:

* Explicit `EXACT_REFERENCE` and `LLM_JUDGE` production modes
* Versioned reference-free judge prompt and response contracts
* Separately configured JUDGE role, timeout, lifecycle, usage, and pricing
* Judge failure, grounding, telemetry, and prompt-injection behavior

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

Production requires one explicit evaluator mode and never falls back between
modes or to a fake evaluator:

* `EXACT_REFERENCE` is deterministic benchmark measurement. It requires caller-
  supplied `reference_output`; the API rejects a missing reference with HTTP 422
  before cache lookup, model calls, or evaluator calls.
* `LLM_JUDGE` is reference-free production measurement. It requires a dedicated
  JUDGE deployment and model identity and rejects a configuration that requires
  caller reference output.

Container Apps derives both API and UI reference requirements from the selected
mode. The checked-in hackathon parameter files select `LLM_JUDGE` but keep
`deployContainerApps=false` and contain non-deployable judge placeholders.

Construction proceeds in this order:

1. Application Insights observability
2. Shared Foundry SMALL and STRONG provider resources
3. Separately timed Foundry JUDGE resources in `LLM_JUDGE` mode
4. Foundry embedding resources
5. Azure Managed Redis resources
6. Redis index bootstrap
7. Cosmos run-history resources
8. Immutable execution dependencies

Shutdown proceeds in reverse resource order: Cosmos, Redis, embedding, optional
JUDGE, shared Foundry, then telemetry. Each runtime closes once. Cleanup failures
are recorded by type and do not skip later cleanup or mask an earlier startup
failure.

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
starts `streamlit run src/ui/app.py`. Both use digest-pinned Azure Linux 3.0
Python 3.12 builder and non-root distroless runtime images. The pinned uv 0.12.5
stage installs from `uv.lock` with `--frozen --no-dev --no-editable --no-cache`.
The final image copies no package manager, compiler, shell setup, test tree, or
source-control metadata and runs as the base image's declared `nonroot` user.
The Docker context allow list contains only `pyproject.toml`, `uv.lock`, `src`,
and the SBOM generator, while excluding bytecode and cache directories.

Each builder generates a deterministic CycloneDX 1.6 inventory from its exact
installed environment. The final API image contains `sbom/api.cdx.json`; the UI
image contains `sbom/ui.cdx.json`. Repository copies under `security/sbom` are
reproducible Linux x64 evidence from the same frozen production closure.

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

The deployed UI sets `OPTIMA_UI_PRODUCTION_MODE=true`. This requires an explicit
HTTPS `OPTIMA_API_BASE_URL`; the Streamlit form exposes no destination override,
and its HTTP client refuses redirects. `OPTIMA_API_TIMEOUT_SECONDS=315` exceeds
the server's execution plus persistence budgets. Local development defaults to
`http://127.0.0.1:8000` and may use an environment-only override while
production mode is false.

The API rejects request bodies above 4 MiB before JSON deserialization. Parsed
requests limit input to 32,000 characters, context to 128,000, reference output
to 32,000, criteria to 20 entries of 2,000 characters each, canonical metadata
to 32 KiB and bounded nesting, and caller latency to 300,000 milliseconds.
Production allows four active executions per process and at most three API
replicas, applies a 300-second overall deadline, and aligns the Container Apps
HTTP scale threshold to four concurrent requests. These controls bound one
deployment but do not implement a distributed per-user quota.

Evaluator configuration is explicit:

* `OPTIMA_PRODUCTION_EVALUATOR_MODE` is `EXACT_REFERENCE` or `LLM_JUDGE`
* `OPTIMA_PRODUCTION_REQUIRE_REFERENCE_OUTPUT` is `true` only for
  `EXACT_REFERENCE`
* `OPTIMA_REQUIRE_REFERENCE_OUTPUT` gives the Streamlit UI the same mode-derived
  requirement
* `OPTIMA_JUDGE_DEPLOYMENT` identifies the JUDGE deployment in `LLM_JUDGE` mode
* `OPTIMA_JUDGE_MODEL` records the reviewed provider model identity
* `OPTIMA_JUDGE_TIMEOUT_SECONDS` sets a bounded JUDGE request timeout, default 30

Required external Foundry values are:

* HTTPS Azure OpenAI v1 root ending in `/openai/v1`
* SMALL chat-completions deployment name
* STRONG chat-completions deployment name
* JUDGE chat-completions deployment and provider model identity
* Embedding deployment name and provider-reported model identity
* Exact embedding vector dimension
* Token scope, normally `https://cognitiveservices.azure.com/.default`
* Cognitive Services OpenAI User for the API identity on the exact Foundry resource

Production Managed Identity mode requires the API user-assigned client ID for
Foundry, Cosmos, and Redis. Redis additionally requires the identity principal
ID as its AUTH username. Redis uses fixed Azure Managed Redis TLS port `10000`.

Application Insights must be enabled with its connection string, service name,
deployment environment, and sampling ratio. Live Metrics, performance counters,
and offline storage remain disabled. IaC carries the connection string through
secure module values and a Container Apps secret reference.

The external UI uses Container Apps built-in Microsoft Entra authentication.
Slice 11C must provide an existing single-tenant app registration and tenant ID,
enable ID-token issuance, register the exact UI callback URI
`https://<ui-fqdn>/.auth/login/aad/callback`, and restrict assignment to the
intended hackathon users when tenant-wide access is too broad. The checked-in
configuration stores no client secret and enables no token store.

LLM-judge evaluation sends the original task, candidate output, explicit
criteria, and required supplied context through the configured Foundry/APIM
security boundary. It does not send `reference_output` or unrelated caller
metadata. Raw judge prompts, responses, user tasks, candidate answers, and context
are not logged. The judge performs no external web or factual lookup.

## East US 2 preflight

Before Bicep execution, Slice 11C must validate as far as Azure APIs allow:

1. `Microsoft.Cache` resource-provider registration
2. Azure Managed Redis service availability in `eastus2`
3. Balanced B0 SKU support in `eastus2`
4. Relevant subscription quota and limits
5. Presence of immutable API and UI manifests in ACR
6. Replacement of every Foundry, embedding, and digest placeholder
7. Replacement of UI Entra client and tenant placeholders and verification of
  the exact callback URI and user-assignment policy

Valid SKU metadata and quota do not guarantee regional allocation capacity.
Allocation failure must stop deployment with a clear error. It must not place
Redis in East US or another fallback region.

## Remaining Slice 11C inputs

Reference-free production evaluation is implemented but not deployed. Slice 11C
must replace every image, endpoint, deployment, model, and pricing placeholder
with reviewed live values before enabling Container Apps.

The runtime central cost calculator assembles its versioned catalog from
configured rates. When no rates are configured, it uses an explicit unpriced
catalog: token usage remains measured, but monetary cost stays unavailable
rather than fabricated. OPTIMA never invents rates.

Slice 11C must supply the following reviewed pricing inputs as deployment
configuration, because the model deployments are not yet selected. All rates are
per-million-token `Decimal` values in one shared currency, keyed inside the
runtime to provider `microsoft-foundry-apim` and the exact SMALL, STRONG, JUDGE,
and embedding deployment names already configured for the runtime:

* `OPTIMA_PRICING_CATALOG_VERSION` — provenance/version identifier
* `OPTIMA_PRICING_CURRENCY` — shared currency code, default `USD`
* `OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS`
* `OPTIMA_PRICING_SMALL_OUTPUT_RATE_PER_MILLION_TOKENS`
* `OPTIMA_PRICING_SMALL_CACHED_INPUT_RATE_PER_MILLION_TOKENS` — optional
* `OPTIMA_PRICING_STRONG_INPUT_RATE_PER_MILLION_TOKENS`
* `OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS`
* `OPTIMA_PRICING_STRONG_CACHED_INPUT_RATE_PER_MILLION_TOKENS` — optional
* `OPTIMA_PRICING_JUDGE_INPUT_RATE_PER_MILLION_TOKENS`
* `OPTIMA_PRICING_JUDGE_OUTPUT_RATE_PER_MILLION_TOKENS`
* `OPTIMA_PRICING_JUDGE_CACHED_INPUT_RATE_PER_MILLION_TOKENS`, optional
* `OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS`

Partial pricing is rejected at settings construction so incomplete configuration
cannot fabricate monetary evidence. When
`OPTIMA_PRODUCTION_COST_MEASUREMENT_REQUIRED=true`, production startup fails
clearly if the complete SMALL, STRONG, and embedding catalog is absent. When it
is `false` and all pricing is absent, monetary cost is reported as unavailable
while token usage stays measured. `LLM_JUDGE` rejects a configured catalog that
omits JUDGE input or output rates, because generator-only cost is not a complete
run cost.

Slice 11C must also supply and verify:

1. `productionEvaluatorMode = 'LLM_JUDGE'`
2. A real `judgeDeployment` and `judgeModel` in both deployment parameter paths
3. A reviewed `judgeTimeoutSeconds` between 1 and 120
4. JSON-object response-format support on the selected JUDGE deployment
5. Managed-identity inference permission for the JUDGE deployment
6. Live SMALL, STRONG, JUDGE, and embedding rates under one catalog version and
  currency when cost measurement is required
7. Immutable API and UI image digests and all existing Redis, Cosmos,
  Application Insights, and Foundry inputs
8. Existing single-tenant UI Entra app client ID, tenant ID, ID-token issuance,
   callback URI, and intended-user assignment

Actual model names, deployment names, and rates remain deployment inputs. This
repository does not fabricate them.

## Slice 11C container validation gate

Docker and Podman were unavailable during Slice 11B, so only static Dockerfile
contracts, frozen Linux dependency artifacts, and each pinned base image were
verified. Static contracts and base scans are not sufficient for a first Azure
deployment.

Slice 11C must block application deployment until both the API and UI images
have:

1. built successfully as Linux `AMD64` images
2. started successfully
3. passed API and UI local or CI container smoke tests
4. produced immutable ACR manifest digests
5. been scanned as final images for OS and Python advisories, secrets, and
  unexpected executable content
6. been checked for non-root execution, embedded SBOMs, no development packages,
  no `.git` or `.env` content, and no package-manager caches

Only after all six hold for both images may Slice 11C publish digests and enable
Container Apps.