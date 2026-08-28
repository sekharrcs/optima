---
title: OPTIMA Pre-Deployment Security
description: Threat boundaries, supply-chain evidence, scan results, and deployment gates for the hackathon runtime
---

## Scope and status

Slice 11B-S hardens the repository and deployment definitions without creating,
updating, or deleting any Azure resource. It creates no credential, service
principal, app registration, or cloud resource.

The reviewed source base is
`f538ea3b670a8a9f1b33c07868711afb0f309d1e`. The documented scans found no
malicious execution or exfiltration indicator and no secret in the 56-commit Git
history. This is evidence from named tools at reviewed versions, not a guarantee
that the software or every dependency is malware-free.

The deployment recommendation remains **NO-GO** until both final images are
built, started, inspected, and scanned. Docker, Podman, WSL, BuildKit, containerd,
and equivalent local builders were unavailable on the review workstation.

## Threat model

The hackathon deployment accepts browser input that can trigger paid model,
embedding, evaluation, cache, and persistence work. Relevant threats include:

* Anonymous use of the public UI to generate paid traffic
* Server-side requests to attacker-selected or internal destinations
* Oversized or deeply nested requests that exhaust memory or model capacity
* Unbounded parallel execution and excessive request duration
* Cross-user disclosure through shared run history
* Credential, prompt, context, or exception leakage through source or telemetry
* Malicious source, dependency, install hook, base image, or copied build content
* Vulnerable native libraries in the deployable artifact

## Authentication boundary

The UI is the only external Container App. Its ingress rejects plaintext traffic,
and Container Apps built-in authentication requires a tenant-restricted Microsoft
Entra session before Streamlit receives a request. Anonymous browser requests are
redirected to the configured Entra provider. The API remains internal because it
has no application-layer caller authentication.

The UI Entra registration uses the confidential-client authorization-code (hybrid)
flow. Container Apps built-in authentication needs a client credential to complete
the server-side authorization-code exchange. When no credential is configured, it
silently falls back to the OpenID Connect implicit flow and receives only an ID
token, a flow Microsoft recommends avoiding. The `Microsoft.App/containerApps`
`authConfigs` schema exposes only a client secret (`clientSecretSettingName`) or a
signing certificate for the Entra registration; it has no federated or
managed-identity credentialless option. OPTIMA therefore references a client secret
through `clientSecretSettingName`.

The secret is handled Azure-natively and never committed:

* A `@secure()` `uiAuthClientSecret` parameter carries it into the deployment and
  compiles to an ARM `securestring`.
* It is stored as the `ui-auth-client-secret` Container Apps secret and referenced
  only by the auth registration, never as a plain container environment value.
* It has an empty default, so parameter files carry no credential, and it is never
  a Bicep output.
* Deployment fails when Container Apps are enabled without a non-placeholder client
  ID, tenant ID, and a non-empty client secret. There is no permissive fallback.

The token store stays disabled: OPTIMA only authenticates the user and never calls
downstream APIs with a delegated user token, so no access or refresh token needs
persisting. OPTIMA does not trust or consume caller-supplied identity headers.

A certificate credential (`clientSecretCertificateThumbprint` with its issuer or
subject-alternative-name variants) is the documented alternative. It was not
selected. For a short-lived hackathon environment it adds certificate issuance,
upload, and rotation lifecycle without a practical security gain over a
secret-referenced confidential client, because both are confidential-client
credentials held server-side by the platform. It remains a future hardening option.

Slice 11C must, before public exposure, use an existing single-tenant Entra app
registration and:

* Replace the checked-in client and tenant ID placeholders
* Create a client secret on the registration and pass it only as the secure
  `uiAuthClientSecret` deployment parameter at preflight
* Register `https://<ui-fqdn>/.auth/login/aad/callback` as a Web redirect URI
* Restrict app assignment to intended hackathon users when tenant-wide access is
  broader than required

This flow is configured in infrastructure but not yet verified against a live
tenant. A successful Bicep build is not proof that interactive sign-in works.
Authentication is confirmed only after the live preflight in the security backlog.

## API and UI trust boundary

Production Streamlit obtains `OPTIMA_API_BASE_URL` only from trusted process
configuration. `OPTIMA_UI_PRODUCTION_MODE=true` requires an explicit HTTPS root,
rejects user information, paths, queries, and fragments in the configured URL,
and refuses every HTTP redirect. The form contains no API destination control.
Its 315-second production transport timeout exceeds the 300-second execution
deadline plus the 10-second persistence budget, so the browser-facing service
does not abandon paid work that remains within the server contract.

Local development defaults to `http://127.0.0.1:8000`. Developers may override
that root through the environment only while production mode is false.

The public UI stores run history only in its Streamlit session and never calls
the backend history GET/list routes. Those shared Cosmos routes remain internal.
Exposing the API externally or adding durable history browsing to the public UI
requires identity-aware ownership and authorization first. Client-supplied user
IDs or unprotected headers are not an acceptable boundary.

## Managed identity and network boundary

The API user-assigned managed identity accesses Foundry, Cosmos DB, Azure Managed
Redis, and ACR. The UI identity pulls only its ACR image. Cosmos local auth,
Redis access keys, and ACR admin credentials remain disabled; Redis and Cosmos
require TLS.

Cosmos and Redis retain public service endpoints for the hackathon. Private
endpoints, VNet integration, private DNS, and egress filtering remain production
hardening backlog items because they add cost and deployment complexity. Their
absence does not change the Entra-only data-plane authentication requirement.

## Request and cost-abuse controls

The API enforces these limits before provider execution:

* Raw HTTP body: 4 MiB
* Input text: 32,000 characters
* Context: 128,000 characters
* Reference output: 32,000 characters
* Criteria: 20 entries, 2,000 characters each
* Metadata: 32 KiB canonical UTF-8 JSON and bounded nesting
* Caller latency ceiling: 300,000 milliseconds
* Server execution deadline: 300 seconds
* Active executions: four per process, rejected without queueing when full
* API replicas: at most three in the reviewed Container Apps configuration

These controls bound one hackathon deployment. They do not implement a
distributed user quota. Add an API gateway or another platform-enforced,
identity-aware quota before operating a sustained or broadly shared workload.
Best-effort run-history persistence uses its own Cosmos timeout after the paid
execution deadline ends. A stalled save reports `RUN_HISTORY_TIMED_OUT` while
returning the completed result, so clients are not encouraged to repeat paid
work.

Task type, complexity, supplied token count, risk tier, cache eligibility, and
large-context state remain caller-supplied demo profile facts. The UI labels them
as such. They influence Planner V1 but cannot bypass request size, concurrency,
deadline, mandatory evaluation, or strong-fallback controls. Server-side request
profiling remains future work and must not silently replace Planner V1.

## Secrets and telemetry

No credential is checked into Bicep or parameter files. The Application Insights
connection string is a destination identifier, but IaC still carries it through
a secure module output, a secure module input, and a Container Apps secret
reference. It is not returned by the root template.

The UI Entra confidential-client secret is handled the same way. A secure parameter
with an empty default becomes the `ui-auth-client-secret` Container Apps secret,
referenced only by the auth registration. It is never committed, defaulted to a real
value, emitted as an output, or exposed as a container environment value.

Production logs emit fixed messages or bounded exception type names. Telemetry
does not export request or response bodies, raw paths, query strings, headers,
cookies, authorization values, API keys, prompts, context, reference output,
connection strings, or raw exception text. Prompt and output logging remains
disabled.

## Dependency provenance

`pyproject.toml` has ten direct runtime dependencies. `uv.lock` has 82 package
records: one local project root, 71 runtime registry packages across supported
markers, and ten development-only packages. Every third-party source is the
approved Microsoft package-feed proxy. Artifact URLs use Microsoft feed hosts
and carry SHA-256 hashes. No third-party Git, arbitrary URL, local path, virtual,
or editable source is present.

The local project is the only editable lock record, and Docker explicitly uses
`--no-editable`. Executable tests verify that every lock record has a direct or
transitive parent and that development packages do not enter the runtime graph.
Native runtime packages include CFFI, cryptography, NumPy, pandas, Pillow,
PyArrow, psutil, pydantic-core, rpds-py, and watchdog. They are expected parents
of Azure Identity, Streamlit, Pydantic, or telemetry dependencies.

Streamlit 1.50.0 and Pillow 11.3.0 were rejected after advisory scanning.
The lock now contains Streamlit 1.54.0 and Pillow 12.3.0; the refreshed
production audit reports zero known vulnerabilities.

## Reproducible images and SBOMs

Both Dockerfiles use these registry-resolved manifest-list digests:

* uv 0.12.5:
  `sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1`
* Azure Linux Python 3.12 builder:
  `sha256:0b729c82c0ddc0769248e287d7414f0cc4e42ae4aa5b786aa99883c247e42bdb`
* Azure Linux non-root distroless Python 3.12 runtime:
  `sha256:d921452dba64944bf959f22450bb3740f5b2fff4a59faa64bd6b8eaf4c57b5b8`

The uv, builder, and runtime bases each produced zero Trivy vulnerability and
secret findings at the reviewed digests. The rejected Debian base had applicable
fixed high and critical findings and was replaced rather than suppressed.

Docker uses `uv sync --frozen --no-dev --no-editable --no-cache`. Runtime stages
contain no package manager or compiler copied from the builder, run as the base
image's declared `nonroot` user, and receive only the virtual environment, source,
and SBOM. The allow-listed build context excludes `.git`, tests, `.env` files,
documentation, infrastructure, caches, and Python bytecode.

`security/sbom/api.cdx.json` and `security/sbom/ui.cdx.json` are deterministic
CycloneDX 1.6 artifacts for the frozen Linux x64 production closure. Each names
its runtime image component and records 70 third-party Python libraries. The
Docker build regenerates the matching SBOM inside each exact builder environment.

These are Python dependency SBOMs generated from the frozen environment, not final
container image SBOMs. They inventory Python distributions only. They do not
include the base image operating-system packages or the native shared libraries
present in the built image. A final-container SBOM must be generated and verified
against each built image, including applicable operating-system and native
components, during the blocking artifact gate below.

## Security scan results

Scans executed on 2026-08-27:

* Malicious-code review: no unexplained execution or exfiltration behavior
* Dangerous primitive hits: fixed-name Redis dynamic imports; two constant,
  no-shell test subprocesses; regular-expression compilation; SBOM metadata
  inspection; explicit Azure destinations and auth modes
* pip-audit: 71 production distributions examined, zero known vulnerabilities
* Bandit: 13,946 lines, zero high, zero medium, three justified low findings
* Gitleaks 8.30.1: 56 commits and 3.99 MB scanned, zero leaks
* Trivy 0.74.0 filesystem: zero vulnerabilities and zero secrets; two low
  missing-`HEALTHCHECK` notices covered by Container Apps probes
* Trivy base images: zero findings for the pinned uv, Azure Linux builder, and
  Azure Linux distroless runtime images
* Microsoft Defender: engine 1.1.26080.3, signatures 1.457.364.0, zero detections
  for the repository custom scan
* Bicep 0.46.1: changed modules and entry points build and lint cleanly

Bandit's pseudorandom finding is renewal retry jitter, not a security token or
identifier. Its two exception findings are best-effort telemetry cleanup loops
that continue closing independent owned resources. Trivy's Dockerfile lows ask
for image-level health checks; the deployed apps already define explicit Azure
Container Apps liveness and readiness probes.

## Rerun security checks

Use the approved Microsoft package proxy and do not modify the project lock when
running scanners:

```powershell
uv lock --check
uv sync --frozen --no-dev --no-editable --python-platform x86_64-unknown-linux-gnu
python scripts/generate_sbom.py --component api --output security/sbom/api.cdx.json
python scripts/generate_sbom.py --component ui --output security/sbom/ui.cdx.json
uvx --default-index https://packagefeedproxy.microsoft.io/pypi/simple --from pip-audit pip-audit --path .venv/Lib/site-packages
uvx --default-index https://packagefeedproxy.microsoft.io/pypi/simple --from bandit bandit -r src scripts
gitleaks git --redact=100 .
trivy filesystem --scanners vuln,secret,misconfig .
```

After an OCI builder is available, complete the blocking artifact gate:

```powershell
docker build --file Dockerfile.api --tag optima-api:security .
docker build --file Dockerfile.ui --tag optima-ui:security .
trivy image --scanners vuln,secret optima-api:security
trivy image --scanners vuln,secret optima-ui:security
```

Inspect both final images for their declared non-root user, entrypoint, embedded
SBOM, package list, executable files, `.git`, tests, `.env` files, credentials,
SSH material, package-manager caches, and build tools before publishing.

## Unresolved security backlog

The following items remain explicit:

* Build, start, inspect, and scan both final production images
* Create, supply, and rotate the UI Entra client secret at preflight, then validate
  the Entra callback, tenant restriction, and intended-user assignment in a
  non-production preflight before public exposure
* Add identity-aware durable history ownership before exposing backend history
  through a public multi-user experience
* Add distributed identity-aware quotas before sustained or broad usage
* Add private endpoints and controlled egress for a production deployment
* Revisit Redis command-level ACLs when module-compatible stable APIs exist
* Re-scan dependencies and immutable bases immediately before image publication

No Azure deployment occurred during this security slice.