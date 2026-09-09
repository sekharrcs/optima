---
title: Foundation Diagnostic Capture
description: Safe failure evidence and an owner-encrypted CMS inspection procedure requiring separate setup and dispatch approval
---

## Scope and observed evidence

No key generation, environment-variable publication, workflow dispatch, retry,
deployment, merge, permission change, artifact deletion, or cost-date change is
authorized now. The commands below are prepared owner instructions, not commands
to execute through an agent. Use capture only for the NEXT separately authorized
`foundation-plan` run after the implementation and setup gates below pass.
Every nonempty `diagnostics` collection remains fatal, including `Info`, `Warning`,
recognized codes, malformed entries, and entries beyond the display limit.
The classifier and promotion acceptance rules are unchanged; failure evidence never authorizes apply.

Run `34355473718` retained `WHATIF_DIAGNOSTICS`, one diagnostic, nine `changes` entries, and `potentialChanges: null`. Its diagnostic code, level, message, target,
and cause were discarded. Nine entries do not establish nine approved changes.
Counts, digests, tool versions, and another run cannot recover those bytes.
Nested-deployment short-circuiting is a published possible explanation, not an
observed cause of this run. Outer-mode evaluation is one published message form,
not a conclusion supported by the lost diagnostic or by its count.

## Preconditions before any future run

1. Require reviewed merge of the v2 projection, stdout/stderr containment, and CMS capture changes,
   with required checks passing on the reviewed source. Confirm the exact current
   protected `main` SHA; obtain separate explicit authorization for one run at that SHA.
   These instructions do not authorize the merge or dispatch.
2. Confirm no conflicting foundation or production run, pending conflicting approval,
   or active foundation deployment. Preserve the existing concurrency and preflight gates.
3. Require the existing cost review to remain valid at execution time. Keep
   `OPTIMA_COST_REVIEWED_ON` and the approved estimate unchanged; stop if stale or invalid.
   Do not refresh a date, change configuration, or add permissions to pass a gate.
4. Use the normal `hackathon` environment approval and existing plan identity.
   Preserve identity, scope, source, and authorization checks without bypasses.
5. Establish the owner-encrypted route below BEFORE dispatch. Obtain separate setup
   approval covering local key generation, the two public `hackathon` environment
   variables, and retention of publicly copyable ciphertext. Prove local key possession
   offline before publication. Setup approval does not authorize dispatch.
6. Require passing helper and workflow tests on the reviewed source, including
   the same CMS profile on Windows and Linux, exact stream preservation, nonzero CLI exit handling,
   invalid-certificate rejection before Azure login, and failed-job-only upload.
   Also require the owner's offline synthetic proof below. Same-profile tests on
   each platform are not a direct Linux-to-Windows ciphertext exchange; that exchange
   has not yet been tested. Retain actual test evidence before setup or dispatch.

## Exact bounded capture procedure

1. After authorization, select `Foundation plan and apply`, the authorized current
   `main` SHA, and `operation=foundation-plan`. Leave all FIVE apply inputs empty:
   `confirm_commit_sha`, `confirm_foundation`, `plan_run_id`, `confirm_plan_actor`,
   and `confirm_plan_artifact_digest`. A moved SHA requires renewed authorization.
2. Verify the unchanged profile: `environmentName=hackathon`, `location=eastus2`,
   `resourceGroup=rg-optima-hackathon`, and `deployContainerApps=false`,
   `exposePublicUi=false`, `deployRuntimeAccess=false`, `semanticCacheEnabled=false`.
   Keep the existing foundation template, parameter file, and run provenance bindings.
3. Allow only the workflow's existing single authoritative what-if, with
   `ProviderNoRbac`, `FullResourcePayloads`, and structured non-pretty output unchanged.
   There is no diagnostic retry, reduced validation, debug mode, or additional input.
4. Require both what-if stdout and stderr to stay in narrowly owned runner temporary
   storage with restrictive permissions. Raw files must be removed on success and
   handled failure, without hiding the failing exit status. After the single CLI call,
   seal both streams before EXIT cleanup, including when the CLI exits nonzero.
   Do not echo, `tee`, upload plaintext, or add raw content to step outputs, logs,
   summaries, or caches. The separately approved CMS artifact is the only raw-content
   retention route. Runner loss or forced termination can defeat sealing and cleanup.
5. On classifier failure, retain the safe `foundation-classification-failure.json` in
   `foundation-classification-failure-RUN-ATTEMPT`, substituting that run ID and attempt.
   Retention is seven days. This separate failure artifact is never promotion evidence.
   Independently, upload the sealed ciphertext only when the plan job fails, as
   specified below. CLI failure before classification can leave no safe projection;
   sealing may still preserve the same call's two streams. Capture or upload failure
   may leave neither artifact. Stop without plaintext fallback or automatic retry.
   An approved plan discards the ciphertext and retains only the existing approved-plan
   evidence. Promotion acceptance and the sole-evidence requirement are unchanged.

## Inspect only the same-run safe artifact

1. Record the authorized SHA, actual run ID, attempt, actor, workflow, and outcome.
   Select the exact failure artifact from that SAME run and attempt, not a similarly
   named artifact from an earlier run. Require unexpired metadata and record its artifact ID.
2. Download that artifact's archive and compare its locally calculated SHA-256 with
   GitHub's artifact metadata `sha256:` digest. Hash the downloaded archive bytes,
   NOT the extracted JSON, a reserialized document, or a newly repacked archive.
   Stop on missing digest, mismatch, unexpected provenance, or expired evidence.
3. Require exactly the expected regular JSON file, without extra entries or symlinks.
   Validate the closed v2 shape before displaying fields; reject duplicate keys or
   unexpected artifact fields. Read only the known-safe fields listed below.
4. Keep the result `FAILED` and `promotable=false`. Review recognized literals and
   reconstructed summaries as limited evidence, not an override. If a field is unknown,
   withheld, invalid, unexpectedly missing/null, or omitted, or explanation is insufficient,
   stop and use the separately authorized private route. Never publish unknown contents.

## Safe output contract and limits

The schema is `optima-foundation-whatif-failure-v2`. Safe top-level fields are
`schema_version`, `classification`, `promotable`, `classifier_error`, `document`,
`fields`, `diagnostic_details`, and `toolchain`. `document` holds `parsed`, `json_type`,
and `unknown_field_count`. `fields` describes only `status`, `error`, `changes`,
`potentialChanges`, and `diagnostics` using `present`, `json_type`, and `array_count`.
`toolchain` holds bounded numeric versions or null for `python`, `azure_cli`,
`azure_cli_core`, `bicep`, and `runner_image`. No raw status or service error is copied.

* `diagnostic_details.entries` contains at most 32 entries in original order.
  `omitted_count` counts the remainder; it is null when no diagnostic array is available.
  An empty projection never proves an empty or accepted original response.
* Each entry contains exactly `index` (zero-based), `json_type`, `code`, `code_state`,
  `level`, `level_state`, `message_state`, `message_form`, `message_summary`, and
  `unknown_field_count`. Non-object entries retain their type; their field count is null.
* States are `recognized`, `missing`, `null`, `invalid`, or `withheld`.
  Missing means absent, null means explicit JSON null, invalid means wrong type, and
  withheld means an unrecognized string. Nonrecognized code/level values are null;
  nonrecognized message forms and summaries are null. Unknown names and values are omitted.
* The ONLY emitted service code is the trusted literal `NestedDeploymentShortCircuited`.
   Code validation is exact literal lookup, not a broad identifier regex: secrets can look alphanumeric. Levels use exact trusted `Info`, `Warning`, or `Error` literals
  from `level`, never inferred `severity` or case-normalized input.
* A message is recognized only with a recognized code AND level and a full match of
  the exact published text, punctuation, and fixed guidance suffix, at most 4096 characters.
  Its deployment path must exactly equal `target`: `/subscriptions/{subscription}`,
  optionally `/resourceGroups/{group}`, then `/providers/Microsoft.Resources/deployments/{name}`.
  The subscription slot is 36 ASCII hex-or-hyphen characters; group/name slots are
  1-90/1-64 ASCII letters, digits, underscores, periods, parentheses, or hyphens.
  Source line and column each allow 1-10 ASCII decimal digits. These are recognition
  bounds, not proof of a real scope or a full ARM resource-name validator.
* The sole form is `nested-deployment-outer-evaluation-v1`. It emits this trusted
  reconstructed constant, never captured target, positions, URL, or raw message:

```text
The nested deployment '[REDACTED]' at line '[REDACTED]' and column '[REDACTED]' could not be expanded because it uses outer-mode evaluation and its template contains expressions that could not be evaluated. This is a reconstructed explanation; analysis is incomplete and review is required.
```

`target`, `additionalInfo`, and unknown fields are never published; unknown fields
contribute only counts. Prefixes, suffix drift, oversized text, and unknown prose
are withheld, not partially scrubbed. These bounds cover projection and matching,
not total raw-response size or parser depth. The catalogue is intentionally incomplete:
the service schema declares open strings, not an exhaustive code/message catalogue.
No guarantee exists that the next response matches. Code alone does not prove outer-mode evaluation.

## Unknown prose and private inspection

GUID/URL removal, truncation, masking, secret scanners, and AI summaries cannot prove
unknown prose safe. The hosted job has no owner-private shell or raw viewer. GitHub
artifact access is not made owner-only by environment approval or short retention.
The selected boundary is encryption to an owner-held private key before upload,
followed by local owner decryption. The private key and passphrase must NEVER enter
GitHub, a runner, chat, an agent, a transcript, or a synced directory.

An unknown diagnostic message survives byte-for-byte inside the captured CLI stream,
regardless of projection recognition. While the ciphertext remains available, the
owner can download and decrypt that SAME artifact later without a code change or
another what-if. The old run's discarded bytes remain lost. A new response is new
evidence, never recovery of the old response.

### Capture contract and approval boundary

The helper uses existing OpenSSL with the same CMS profile tested on Linux 3.0.13
and Git for Windows 3.5.7. Those results do not prove direct cross-version exchange.
It adds no infrastructure, Python packages, permissions, or Azure credentials.
The implemented helper entry points are:

```text
python scripts/foundation_diagnostic_capture.py prepare --directory PATH
python scripts/foundation_diagnostic_capture.py seal --directory PATH --whatif-exit-code INTEGER
```

`PATH` is the workflow-owned private temporary directory, not a user-supplied workflow
input. Both entry points consume the separately approved public environment variables
`OPTIMA_DIAGNOSTIC_CERTIFICATE_BASE64` and `OPTIMA_DIAGNOSTIC_CERTIFICATE_SHA256`.
The first is canonical Base64 of exactly one DER certificate, without PEM markers,
whitespace, a certificate chain, or trailing DER objects. The second is its lowercase
64-character SHA-256 hex digest over DER bytes. Unset or invalid configuration must
fail in `prepare` BEFORE Azure login or what-if. `seal` must revalidate it.

Export DER using the owner command below. The helper enforces canonical Base64,
exact-byte fingerprint binding, and a single certificate that survives OpenSSL
decode/re-encode unchanged. This is not a strict ASN.1 DER validator: OpenSSL can
preserve some correctly signed BER encodings. Do not claim it rejects every
noncanonical inner encoding. Owner-approved certificate bytes, not normalization,
are the recipient trust boundary.

The certificate must be self-signed, currently valid with at least 24 hours remaining,
and use RSA 3072 or 4096 bits with exponent 65537. Required extensions are critical
`keyUsage=keyEncipherment` and critical `basicConstraints=CA:FALSE`. Owner generation
uses `subjectKeyIdentifier=hash`; the helper checks SKI presence and hexadecimal
format (1-64 bytes), not its hash derivation. Check the certificate signature,
canonical encoding, and the independently verified fingerprint before accepting it.
Certificate material is for diagnostic encryption, not Azure authentication.

Seal an uncompressed TAR containing exactly three regular members: `manifest.json`,
`whatif.stdout`, and `whatif.stderr`. Each stream is at most 64 MiB. Preserve exact
bytes, including empty streams, encoding, newlines, unknown prose, and nonzero CLI
exit results. Do not decode and reserialize streams or truncate an oversized capture.
Here, full raw means bytes emitted by the Azure CLI to these two streams, not an
undocumented wire-response serialization or data the CLI never emitted.

Encrypt the TAR as OpenSSL CMS AuthEnvelopedData using AES-256-GCM, RSA-OAEP with
SHA-256 and MGF1-SHA-256, and `-binary -outform DER -keyid`. Name the ciphertext
`foundation-private-capture.cms`. Seal before EXIT cleanup even on nonzero CLI exit;
preserve the CLI failure and any capture failure without turning either into success.
Remove raw streams, TAR, and temporary certificate files on handled exits. The
workflow allocates one private scratch directory before login, passes it through
`OPTIMA_DIAGNOSTIC_SCRATCH_DIRECTORY`, and records only that nonsecret path as a
step output. The later `always()` cleanup removes that exact allocated directory,
including interrupted helper snapshots, when the runner is still executing steps.
Forced runner termination can prevent even this cleanup.

Only a failed plan job may upload `foundation-private-capture.cms`, as the sole file
in distinct artifact `foundation-private-diagnostic-RUN-ATTEMPT`, with
`retention-days: 1` and `compression-level: 0`. Do not bundle the manifest, logs,
plaintext, or safe projection into that artifact. An approved plan deletes its local
ciphertext and retains the sole existing promotion artifact. No promotion code,
artifact acceptance rule, normal OIDC gate, or dedicated plan identity changes.

Upload additionally requires `sealed=true` from this invocation after successful
encryption. Pre-existing output, preparation failure, and encryption failure never
set that marker and cannot upload a stale or unencrypted file. The marker contains
no response content and does not turn a failed CLI or classifier into success.

A personal local `az login` uses the wrong principal. The existing federated identity
credential cannot be used locally as a replacement for the hosted OIDC gate without
a new credential or token export, neither of which is authorized. Azure Support
availability and recovery are unverified. GPG is feasible but introduces keyring
handling complexity; `age` is not installed. Neither is the selected route.

### Owner setup in Windows PowerShell 5.1

Do not execute these blocks now. After separate setup approval, run them yourself in
one local Windows PowerShell 5.1 session outside VS Code and all AI tools. Use a
nonrecorded terminal with no transcript, session capture, or terminal sharing. If
organizational recording cannot be excluded, stop and arrange an approved private
environment. Do not disable organizational controls or ask an agent to enter secrets.

Choose an existing owner-controlled directory on a fully encrypted, unlocked
BitLocker volume, outside every repository, OneDrive, other sync root, and shared
folder. Check all parent directories for redirection and shared access. The commands
create a fresh child with inheritance disabled and access for your Windows SID only;
administrators and malware on the owner machine remain within the threat boundary.
If the read-only BitLocker check is unavailable or denied, stop for an owner-managed
verification; do not automate elevation or install tools. Existing prerequisites
include permission to read BitLocker status, repository administration rights for
environment-variable setup, and Actions read access for artifact inspection.
`AccessDenied` means stop, not request elevation or grant rights now.

```powershell
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {
   throw 'Use the reviewed Windows PowerShell 5.1 procedure.'
}
$OpenSsl = 'C:\Program Files\Git\usr\bin\openssl.exe'
$TarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path -LiteralPath $OpenSsl -PathType Leaf) -or
   -not (Test-Path -LiteralPath $TarExe -PathType Leaf)) {
   throw 'A required existing local tool is unavailable; stop.'
}
$Version = & $OpenSsl version
if ($LASTEXITCODE -ne 0) { throw 'OpenSSL version check failed.' }
if ($Version -cnotmatch '^OpenSSL 3\.5\.7(?: |$)') { throw 'Unreviewed OpenSSL version.' }
$PrivateParent = [IO.Path]::GetFullPath((Read-Host 'Existing private nonsynced directory'))
if ($PrivateParent -cnotmatch '^[A-Za-z]:\\[^"\x00-\x1f]*$' -or
   $PrivateParent -match '(?i)OneDrive') {
   throw 'Use a local nonsynced directory.'
}
$Parent = Get-Item -LiteralPath $PrivateParent
if (-not $Parent.PSIsContainer) { throw 'The parent must be an existing directory.' }
for ($Ancestor = $Parent; $null -ne $Ancestor; $Ancestor = $Ancestor.Parent) {
   if (($Ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw 'Redirected paths are not allowed.'
   }
}
$Volume = Get-BitLockerVolume -MountPoint ([IO.Path]::GetPathRoot($PrivateParent))
if ($Volume.VolumeStatus -ne 'FullyEncrypted' -or
   $Volume.ProtectionStatus -ne 'On' -or $Volume.LockStatus -ne 'Unlocked') {
   throw 'BitLocker protection has not been established.'
}
$OwnerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$PrivateAcl = New-Object Security.AccessControl.DirectorySecurity
$PrivateAcl.SetOwner($OwnerSid)
$PrivateAcl.SetAccessRuleProtection($true, $false)
$OwnerRule = New-Object Security.AccessControl.FileSystemAccessRule(
   $OwnerSid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'
)
$PrivateAcl.AddAccessRule($OwnerRule)
$PrivateRoot = Join-Path $PrivateParent ('foundation-inspection-' + [guid]::NewGuid().ToString('N'))
$null = [IO.Directory]::CreateDirectory($PrivateRoot, $PrivateAcl)
$ActualAcl = Get-Acl -LiteralPath $PrivateRoot
if (-not $ActualAcl.AreAccessRulesProtected -or @($ActualAcl.Access).Count -ne 1 -or
   $ActualAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $OwnerSid.Value -or
   $ActualAcl.Access[0].IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $OwnerSid.Value -or
   $ActualAcl.Access[0].AccessControlType -ne 'Allow' -or
   $ActualAcl.Access[0].FileSystemRights -ne 'FullControl') {
   throw 'Private directory ACL verification failed.'
}
$KeyPem = Join-Path $PrivateRoot 'owner-private-key.pem'
$CertPem = Join-Path $PrivateRoot 'cert.pem'
$CertDer = Join-Path $PrivateRoot 'cert.der'
```

The next block generates an encrypted RSA-4096 private key with an interactive
OpenSSL passphrase prompt. Type the passphrase directly into OpenSSL, never into
chat or a PowerShell command. Do not use an inline password, `-passin env:`, a
passphrase environment variable, or a plaintext key export. The generic subject
contains no owner name or email. Thirty days provides room for the 24-hour gate;
expiry still needs checking before each future run. Keep the encrypted private key
and its passphrase in separately protected owner custody for the approved retention.

```powershell
& $OpenSsl genpkey -algorithm RSA -aes-256-cbc -pkeyopt rsa_keygen_bits:4096 `
   -pkeyopt rsa_keygen_pubexp:65537 -out $KeyPem
if ($LASTEXITCODE -ne 0) { throw 'Private key generation failed; stop.' }
& $OpenSsl req -new -x509 -sha256 -days 30 -key $KeyPem -out $CertPem `
   -subj '/CN=OPTIMA Diagnostic Recipient' `
   -addext 'keyUsage=critical,keyEncipherment' `
   -addext 'basicConstraints=critical,CA:FALSE' `
   -addext 'subjectKeyIdentifier=hash'
if ($LASTEXITCODE -ne 0) { throw 'Certificate generation failed; stop.' }
& $OpenSsl x509 -in $CertPem -outform DER -out $CertDer
if ($LASTEXITCODE -ne 0) { throw 'DER export failed; stop.' }
& $OpenSsl x509 -in $CertPem -checkend 86400 -noout
if ($LASTEXITCODE -ne 0) { throw 'Certificate has less than 24 hours remaining.' }
& $OpenSsl verify -check_ss_sig -CAfile $CertPem $CertPem
if ($LASTEXITCODE -ne 0) { throw 'Self-signature verification failed.' }
& $OpenSsl x509 -in $CertPem -noout -text
if ($LASTEXITCODE -ne 0) { throw 'Public certificate inspection failed.' }
$DerBytes = [IO.File]::ReadAllBytes($CertDer)
$PublicCertificateBase64 = [Convert]::ToBase64String($DerBytes)
$Hasher = [Security.Cryptography.SHA256]::Create()
try {
   $PublicCertificateSha256 = [BitConverter]::ToString($Hasher.ComputeHash($DerBytes)).Replace('-', '').ToLowerInvariant()
} finally { $Hasher.Dispose() }
$FingerprintLine = & $OpenSsl x509 -in $CertPem -noout -fingerprint -sha256
if ($LASTEXITCODE -ne 0) { throw 'Independent public fingerprint calculation failed.' }
$OpenSslFingerprint = ($FingerprintLine -split '=', 2)[1].Replace(':', '').Trim().ToLowerInvariant()
if ($PublicCertificateSha256 -cnotmatch '^[0-9a-f]{64}$' -or
   $OpenSslFingerprint -cne $PublicCertificateSha256) {
   throw 'Independent DER fingerprint comparison failed.'
}
```

Inspect only the public certificate output for the exact key size, exponent,
validity dates, and extensions above. Compare the fingerprint independently with
the approved recipient certificate held by the owner; agreement between two commands
alone does not establish the intended recipient. Do not publish values yet.

### Offline proof of possession before publication

Disconnect networking for this synthetic round trip. It does not call Azure, GitHub,
the helper, or what-if. All test bytes are synthetic. The commands exercise the
selected CMS flags and require possession of the encrypted private key and its
interactive passphrase. They do not substitute for Linux 3.0.13 to Windows 3.5.7
direct-exchange evidence, which is not yet established; same-profile platform
tests and this owner proof are separate gates.

```powershell
$ProofInput = Join-Path $PrivateRoot 'proof-input.bin'
$ProofCipher = Join-Path $PrivateRoot 'proof.cms'
$ProofQuarantine = Join-Path $PrivateRoot 'proof-quarantine.bin'
$ProofBytes = [byte[]]@(0, 1, 10, 13, 127, 128, 255, 0, 10)
[IO.File]::WriteAllBytes($ProofInput, $ProofBytes)
& $OpenSsl cms -encrypt -binary -in $ProofInput -out $ProofCipher -outform DER `
   -aes-256-gcm -keyid -recip $CertPem -keyopt rsa_padding_mode:oaep `
   -keyopt rsa_oaep_md:sha256 -keyopt rsa_mgf1_md:sha256
if ($LASTEXITCODE -ne 0) { throw 'Synthetic CMS encryption failed.' }
& $OpenSsl cms -decrypt -binary -inform DER -in $ProofCipher -recip $CertPem `
   -inkey $KeyPem -out $ProofQuarantine
if ($LASTEXITCODE -ne 0) {
   Remove-Item -LiteralPath $ProofQuarantine -Force -ErrorAction SilentlyContinue
   throw 'Synthetic CMS authentication/decryption failed.'
}
if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($ProofQuarantine)) -cne
   [Convert]::ToBase64String($ProofBytes)) {
   throw 'Synthetic byte preservation failed.'
}
Remove-Item -LiteralPath $ProofInput, $ProofCipher, $ProofQuarantine -Force
Write-Output 'Synthetic local CMS proof passed; no private data displayed.'
```

After the offline proof passes, reconnect only as needed for the separately approved
setup. These are the only values to publish, as public variables in the `hackathon`
environment, not repository secrets or a private-key upload:

```powershell
Write-Output ('OPTIMA_DIAGNOSTIC_CERTIFICATE_BASE64=' + $PublicCertificateBase64)
Write-Output ('OPTIMA_DIAGNOSTIC_CERTIFICATE_SHA256=' + $PublicCertificateSha256)
```

Use the GitHub environment settings after explicit setup authorization. No variable
mutation command is supplied or authorized here. Independently verify the published
DER fingerprint against the owner-approved certificate and require passing `prepare`
validation on the reviewed workflow before login. Stop on substitution or mismatch.
Public-variable setup, ciphertext retention approval, reviewed merge, and a future
single-run dispatch are separate decisions. None is authorized by this runbook.

### Read-only same-artifact download and provenance checks

After a separately authorized run fails, use the same private local directory and
owner session. Use existing `gh` authentication; do not export a token, print
credentials, or log in as the Azure plan identity. Record run IDs, digests, and other
linkage metadata only locally. The following `gh api --method GET` requests are
read-only. Obtain expected SHA, actor, and plan-job display name from the reviewed
source and authorization record, not from untrusted artifact content.

For a fresh session, first re-establish the same private-terminal, BitLocker, path,
tool-version, and ACL checks. Restore these paths to the original owner directory;
do not repeat key generation or overwrite the recipient certificate. Only use an
inspection directory whose output filenames below are absent. A failed or interrupted
inspection needs owner reconciliation, not an automatic overwrite.

```powershell
$ErrorActionPreference = 'Stop'
$OpenSsl = 'C:\Program Files\Git\usr\bin\openssl.exe'
$TarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
$PrivateRoot = [IO.Path]::GetFullPath((Read-Host 'Original private owner directory'))
if ($PrivateRoot -cnotmatch '^[A-Za-z]:\\[^"\x00-\x1f]*$' -or
   $PrivateRoot -match '(?i)OneDrive' -or $PrivateRoot.EndsWith('\')) {
   throw 'Invalid private directory syntax; stop before native commands.'
}
$KeyPem = Join-Path $PrivateRoot 'owner-private-key.pem'
$CertPem = Join-Path $PrivateRoot 'cert.pem'
$CertDer = Join-Path $PrivateRoot 'cert.der'
foreach ($ExistingFile in @($KeyPem, $CertPem, $CertDer)) {
   if (-not (Test-Path -LiteralPath $ExistingFile -PathType Leaf)) {
      throw 'Original owner key or certificate is missing; stop.'
   }
}
```

```powershell
$Repo = 'sekharrcs/optima'
$RunId = Read-Host 'Authorized failed run ID'
$Attempt = Read-Host 'Authorized run attempt'
$ExpectedHead = Read-Host 'Authorized full main commit SHA'
$ExpectedActor = Read-Host 'Authorized workflow actor login'
$ExpectedPlanJobName = Read-Host 'Exact reviewed plan-job display name'
if ($RunId -cnotmatch '^[1-9][0-9]*$' -or $Attempt -cnotmatch '^[1-9][0-9]*$' -or
   $ExpectedHead -cnotmatch '^[0-9a-f]{40}$' -or [string]::IsNullOrWhiteSpace($ExpectedActor) -or
   [string]::IsNullOrWhiteSpace($ExpectedPlanJobName)) { throw 'Invalid expected provenance.' }
$RunJson = gh api --method GET "repos/$Repo/actions/runs/$RunId/attempts/$Attempt" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Run metadata request failed.' }
$Run = try { ($RunJson -join "`n") | ConvertFrom-Json } catch { throw 'Invalid run metadata.' }
$WorkflowJson = gh api --method GET "repos/$Repo/actions/workflows/foundation.yml" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Workflow metadata request failed.' }
$Workflow = try { ($WorkflowJson -join "`n") | ConvertFrom-Json } catch { throw 'Invalid workflow metadata.' }
if ([string]$Run.id -cne $RunId -or [string]$Run.run_attempt -cne $Attempt -or
   $Run.repository.full_name -cne $Repo -or $Run.head_sha -cne $ExpectedHead -or
   $Run.head_branch -cne 'main' -or $Run.event -cne 'workflow_dispatch' -or
   $Run.path -cne '.github/workflows/foundation.yml' -or
   $Run.workflow_id -ne $Workflow.id -or $Workflow.path -cne '.github/workflows/foundation.yml' -or
   $Run.actor.login -cne $ExpectedActor -or $Run.triggering_actor.login -cne $ExpectedActor -or
   $Run.status -cne 'completed' -or $Run.conclusion -cne 'failure') {
   throw 'Run provenance does not match the authorization.'
}
$JobsJson = gh api --method GET "repos/$Repo/actions/runs/$RunId/attempts/$Attempt/jobs?per_page=100" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Job metadata request failed.' }
$Jobs = try { ($JobsJson -join "`n") | ConvertFrom-Json } catch { throw 'Invalid job metadata.' }
if ($Jobs.total_count -gt 100 -or $Jobs.total_count -ne @($Jobs.jobs).Count) {
   throw 'Incomplete job metadata; stop for read-only reconciliation.'
}
$PlanJobs = @($Jobs.jobs | Where-Object { $_.name -ceq $ExpectedPlanJobName })
if ($PlanJobs.Count -ne 1 -or $PlanJobs[0].conclusion -cne 'failure' -or
   $PlanJobs[0].head_sha -cne $ExpectedHead -or [string]$PlanJobs[0].run_attempt -cne $Attempt) {
   throw 'Expected failed plan job was not established.'
}
$ArtifactsJson = gh api --method GET "repos/$Repo/actions/runs/$RunId/artifacts?per_page=100" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Artifact listing failed.' }
$Artifacts = try { ($ArtifactsJson -join "`n") | ConvertFrom-Json } catch { throw 'Invalid artifact listing.' }
if ($Artifacts.total_count -gt 100 -or $Artifacts.total_count -ne @($Artifacts.artifacts).Count) {
   throw 'Incomplete artifact metadata; stop for read-only reconciliation.'
}
$ExpectedArtifactName = "foundation-private-diagnostic-$RunId-$Attempt"
$Matches = @($Artifacts.artifacts | Where-Object { $_.name -ceq $ExpectedArtifactName })
if ($Matches.Count -ne 1) { throw 'Expected exactly one same-attempt private artifact.' }
$ArtifactId = [string]$Matches[0].id
if ($ArtifactId -cnotmatch '^[1-9][0-9]*$') { throw 'Invalid artifact ID.' }
$ArtifactJson = gh api --method GET "repos/$Repo/actions/artifacts/$ArtifactId" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Artifact metadata request failed.' }
$Artifact = try { ($ArtifactJson -join "`n") | ConvertFrom-Json } catch { throw 'Invalid artifact metadata.' }
try {
if ([string]$Artifact.id -cne $ArtifactId -or $Artifact.name -cne $ExpectedArtifactName -or
   $Artifact.expired -ne $false -or [DateTimeOffset]$Artifact.expires_at -le [DateTimeOffset]::UtcNow -or
   [string]$Artifact.workflow_run.id -cne $RunId -or
   $Artifact.workflow_run.head_sha -cne $ExpectedHead -or $Artifact.workflow_run.head_branch -cne 'main' -or
   $Artifact.workflow_run.repository_id -ne $Run.repository.id -or
   $Artifact.workflow_run.head_repository_id -ne $Run.repository.id -or
   $Artifact.digest -cnotmatch '^sha256:[0-9a-f]{64}$' -or
   $Artifact.size_in_bytes -le 0 -or $Artifact.size_in_bytes -gt 130MB -or
   [DateTimeOffset]$Artifact.created_at -lt [DateTimeOffset]$PlanJobs[0].started_at -or
   [DateTimeOffset]$Artifact.created_at -gt [DateTimeOffset]$PlanJobs[0].completed_at) {
   throw 'Artifact provenance, retention, size, or digest is invalid.'
}
} catch { throw 'Artifact provenance, retention, size, or digest is invalid.' }
```

Keep the bound metadata in memory and, if needed, in the private directory only.
The artifact name binds the attempt because artifact metadata does not itself expose
a run-attempt field. Reviewed upload code and the failed job's time window support
that binding; a name or embedded manifest alone does not prove it. Also confirm the
approved `foundation-plan` inputs and normal environment approval in the trusted run
record. Stop on missing fields or any discrepancy; do not loosen checks to continue.

PowerShell 5.1 `>` and text pipelines must not handle ZIP or TAR member bytes.
These in-session functions count bytes during copying, reject before writing past
the limit, and discard their partial destination on failure. Native failure kills
the reader and discards output; stderr drains concurrently to a discard stream with
an 8 KiB buffer, never into a string or the console. Errors contain fixed text only.
Use only the fixed destinations and validated paths below. Cleanup failure requires
private owner reconciliation. No helper script is created.

```powershell
function Copy-BoundedBytes {
   param([IO.Stream]$Source, [string]$Destination, [long]$MaximumBytes,
      [long]$ExpectedBytes = -1)
   $Output = $null
   $Created = $false
   try {
      if ($MaximumBytes -lt 0 -or $MaximumBytes -gt 1GB -or
         $ExpectedBytes -lt -1 -or $ExpectedBytes -gt $MaximumBytes) {
         throw 'Invalid copy bound.'
      }
      $Output = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
      $Created = $true
      $Buffer = New-Object byte[] 81920
      [long]$Count = 0
      while (($Read = $Source.Read($Buffer, 0,
         [int][Math]::Min($Buffer.Length, $MaximumBytes - $Count + 1))) -gt 0) {
         if ($Read -gt $MaximumBytes - $Count) { throw 'Copy bound exceeded.' }
         $Output.Write($Buffer, 0, $Read)
         $Count += $Read
      }
      if ($ExpectedBytes -ge 0 -and $Count -ne $ExpectedBytes) { throw 'Copy length mismatch.' }
      $Output.Dispose()
      $Output = $null
   } catch {
      try {
         if ($null -ne $Output) { $Output.Dispose() }
         if ($Created) { [IO.File]::Delete($Destination) }
      } catch { throw 'Bounded copy failed; reconcile private files.' }
      throw 'Bounded copy failed; output discarded.'
   }
}
function Export-NativeBytes {
   param([string]$Executable, [string]$Arguments, [string]$Destination,
      [long]$MaximumBytes)
   $StartInfo = New-Object Diagnostics.ProcessStartInfo
   $StartInfo.FileName = $Executable
   $StartInfo.Arguments = $Arguments
   $StartInfo.UseShellExecute = $false
   $StartInfo.CreateNoWindow = $true
   $StartInfo.RedirectStandardOutput = $true
   $StartInfo.RedirectStandardError = $true
   $Process = New-Object Diagnostics.Process
   $Process.StartInfo = $StartInfo
   $Started = $false
   $Copied = $false
   try {
      if (-not $Process.Start()) { throw 'Native binary reader did not start.' }
      $Started = $true
      $ErrorDrain = $Process.StandardError.BaseStream.CopyToAsync([IO.Stream]::Null, 8192)
      Copy-BoundedBytes -Source $Process.StandardOutput.BaseStream `
         -Destination $Destination -MaximumBytes $MaximumBytes
      $Copied = $true
      $Process.WaitForExit()
      $null = $ErrorDrain.GetAwaiter().GetResult()
      if ($Process.ExitCode -ne 0) { throw 'Native binary read failed.' }
   } catch {
      try {
         if ($Started -and -not $Process.HasExited) {
            $Process.Kill()
            $Process.WaitForExit()
         }
      } catch { throw 'Native read failed; reconcile the private process and files.' }
      finally {
         if ($Copied) {
            try { [IO.File]::Delete($Destination) }
            catch { throw 'Native read failed; reconcile private files.' }
         }
      }
      throw 'Native read failed; output must not be inspected.'
   } finally {
      $Process.Dispose()
   }
}
$GhExe = (Get-Command gh -CommandType Application).Source
$ArchivePath = Join-Path $PrivateRoot 'private-artifact.zip'
Export-NativeBytes -Executable $GhExe `
   -Arguments "api --method GET repos/$Repo/actions/artifacts/$ArtifactId/zip" `
   -Destination $ArchivePath -MaximumBytes 130MB
$ZipHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if (('sha256:' + $ZipHash) -cne $Artifact.digest) { throw 'Downloaded ZIP digest mismatch.' }
```

This verifies GitHub's digest against the original downloaded ZIP, not against its
extracted ciphertext or a repacked archive. Missing digest, expiry, or unavailable
archive means stop. Do not rerun what-if to fill a gap.

### Validate the ZIP and authenticate before exposing plaintext

Validate the entire ZIP entry list before writing any member. Require one regular
file with the exact case-sensitive name below, no directory, traversal, alternate
path, extra member, symlink, or device. Write only to a fixed destination using the
entry stream, never `ExtractToDirectory` or an archive-provided path. The 130 MiB
outer limit allows TAR/CMS/ZIP overhead for the two 64 MiB streams; it is not a raw
stream truncation policy.

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
function Export-ZipCiphertext {
   param([string]$ArchivePath, [string]$Destination, [long]$MaximumBytes)
   $Zip = $null
   try {
      if ((Get-Item -LiteralPath $ArchivePath).Length -gt 130MB -or
         $MaximumBytes -le 0 -or $MaximumBytes -gt 130MB) { throw 'Invalid ZIP bound.' }
      $Zip = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
      if ($Zip.Entries.Count -ne 1) { throw 'ZIP must contain exactly one file.' }
      $Entry = $Zip.Entries[0]
      $UnixType = ($Entry.ExternalAttributes -shr 16) -band 0xF000
      if ($Entry.FullName -cne 'foundation-private-capture.cms' -or
         $Entry.Name -cne $Entry.FullName -or $Entry.Length -le 0 -or $Entry.Length -gt $MaximumBytes -or
         ($Entry.ExternalAttributes -band 0x10) -ne 0 -or
         ($Entry.ExternalAttributes -band 0x400) -ne 0 -or
         ($UnixType -ne 0 -and $UnixType -ne 0x8000)) {
         throw 'ZIP entry is not the expected regular ciphertext file.'
      }
      $Input = $Entry.Open()
      try {
         Copy-BoundedBytes -Source $Input -Destination $Destination `
            -MaximumBytes $MaximumBytes -ExpectedBytes $Entry.Length
      } finally { $Input.Dispose() }
   } catch { throw 'ZIP validation or bounded copy failed; reconcile private files.' }
   finally { if ($null -ne $Zip) { $Zip.Dispose() } }
}
$CipherPath = Join-Path $PrivateRoot 'foundation-private-capture.cms'
Export-ZipCiphertext -ArchivePath $ArchivePath -Destination $CipherPath -MaximumBytes 130MB
```

Disconnect networking before private decryption and review. CMS can write plaintext
before GCM authentication completes. Write into a quarantined file in the private
directory; never pipe decryption to a viewer or print it. Check `$LASTEXITCODE`
immediately, remove any partial output on failure, and expose it only after success.
Passphrase entry remains interactive and local to OpenSSL.

First inspect the envelope using OpenSSL's parser, writing its output to a private
file rather than the console. Its hexadecimal rendering is capped at 1 GiB during
copying; allow local disk space and delete it after review. Treat this rendering
as untrusted data, including any optional fields. In the offline non-AI text editor,
require outer `contentType` `id-smime-ct-authEnvelopedData`
(`1.2.840.113549.1.9.16.1.23`), one key-transport recipient identified by subject key
identifier, key encryption `rsaesOaep` (`1.2.840.113549.1.1.7`) with explicit SHA-256
and MGF1-SHA-256 parameters, and content encryption `aes-256-gcm`
(`2.16.840.1.101.3.4.1.46`). Require the expected authentication tag, no detached
content, and no unexpected recipient or alternate envelope type. Confirm the
recipient identifier against the owner's public certificate. Stop on any discrepancy.
Do not substitute CBC: `cms -decrypt` alone does not enforce the chosen algorithm.

```powershell
$CmsStructurePath = Join-Path $PrivateRoot 'cms-structure.txt'
Export-NativeBytes -Executable $OpenSsl `
   -Arguments ('cms -cmsout -print -inform DER -in "' + $CipherPath + '"') `
   -Destination $CmsStructurePath -MaximumBytes 1GB
```

Proceed to the next block only after the local envelope inspection passes. Do not
open the quarantine file, even in a text editor, while OpenSSL is running.

```powershell
$QuarantineTar = Join-Path $PrivateRoot 'quarantine.tar'
$VerifiedTar = Join-Path $PrivateRoot 'authenticated.tar'
if ((Test-Path -LiteralPath $QuarantineTar) -or (Test-Path -LiteralPath $VerifiedTar)) {
   throw 'Use fresh quarantine destinations; do not overwrite earlier inspection data.'
}
& $OpenSsl cms -decrypt -binary -inform DER -in $CipherPath -recip $CertPem `
   -inkey $KeyPem -out $QuarantineTar
if ($LASTEXITCODE -ne 0) {
   Remove-Item -LiteralPath $QuarantineTar -Force -ErrorAction SilentlyContinue
   throw 'CMS authentication/decryption failed; no plaintext may be inspected.'
}
if ((Get-Item -LiteralPath $QuarantineTar).Length -gt 130MB) { throw 'Decrypted TAR exceeds the bound.' }
Move-Item -LiteralPath $QuarantineTar -Destination $VerifiedTar
```

GCM authenticates the ciphertext against alteration; it is not sender authentication.
Anyone with the public certificate can create another valid encrypted message.
Require the GitHub provenance checks, reviewed capture implementation, and private
manifest comparison as well. Do not treat successful decryption as proof that Azure
or this workflow authored the content.

### Check TAR members and the manifest before viewing streams

Use the existing Windows `tar.exe` only to list and stream allowlisted members.
Bound each listing to a private 64 KiB file before reading lines into variables;
never display names or stderr. Require
exactly three regular files, each once, with no directory prefixes, links, extra
logical entries, or unexpected type. Do not extract paths supplied by the archive.
This validates the logical view exposed by `tar`, not physical USTAR headers or the
absence of compression/sparse encoding. The helper writes uncompressed USTAR, but
`tar` may accept other encodings. Count expanded member bytes while streaming to
fixed files (manifest 1 MiB; each stream 64 MiB), killing the reader on overflow.
This bounds stored expansion, not the native parser's CPU/time or internal memory;
it is not a general archive-bomb sandbox or a byte-format validator. Unexpected
logical output is a stop condition, not a reason to broaden the checks.

```powershell
$TarNamesPath = Join-Path $PrivateRoot 'tar-names.txt'
$TarTypesPath = Join-Path $PrivateRoot 'tar-types.txt'
Export-NativeBytes -Executable $TarExe -Arguments ('-tf "' + $VerifiedTar + '"') `
   -Destination $TarNamesPath -MaximumBytes 64KB
Export-NativeBytes -Executable $TarExe -Arguments ('-tvf "' + $VerifiedTar + '"') `
   -Destination $TarTypesPath -MaximumBytes 64KB
try {
   $Names = @([IO.File]::ReadAllLines($TarNamesPath))
   $VerboseEntries = @([IO.File]::ReadAllLines($TarTypesPath))
} catch { throw 'Private TAR listing read failed.' }
$AllowedNames = @('manifest.json', 'whatif.stdout', 'whatif.stderr')
if ($Names.Count -ne 3 -or $VerboseEntries.Count -ne 3) { throw 'Unexpected TAR member count.' }
foreach ($Name in $AllowedNames) {
   if (@($Names | Where-Object { $_ -ceq $Name }).Count -ne 1) { throw 'TAR allowlist mismatch.' }
}
foreach ($EntryLine in $VerboseEntries) {
   if (-not $EntryLine.StartsWith('-')) { throw 'TAR contains a nonregular member.' }
}
foreach ($Name in $AllowedNames) {
   $Destination = Join-Path $PrivateRoot $Name
   $MemberLimit = if ($Name -ceq 'manifest.json') { 1MB } else { 64MB }
   Export-NativeBytes -Executable $TarExe `
      -Arguments ('-xOf "' + $VerifiedTar + '" -- ' + $Name) `
      -Destination $Destination -MaximumBytes $MemberLimit
}
$ManifestPath = Join-Path $PrivateRoot 'manifest.json'
$StdoutPath = Join-Path $PrivateRoot 'whatif.stdout'
$StderrPath = Join-Path $PrivateRoot 'whatif.stderr'
if ((Get-Item -LiteralPath $ManifestPath).Length -gt 1MB -or
   (Get-Item -LiteralPath $StdoutPath).Length -gt 64MB -or
   (Get-Item -LiteralPath $StderrPath).Length -gt 64MB) { throw 'Private member size limit exceeded.' }
$LocalStreamFacts = @(
   foreach ($StreamPath in @($StdoutPath, $StderrPath)) {
      [pscustomobject]@{
         Name = [IO.Path]::GetFileName($StreamPath)
         Bytes = (Get-Item -LiteralPath $StreamPath).Length
         Sha256 = (Get-FileHash -LiteralPath $StreamPath -Algorithm SHA256).Hash.ToLowerInvariant()
      }
   }
)
$LocalStreamFacts | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PrivateRoot 'local-stream-facts.json') -Encoding UTF8
```

Open only `manifest.json` and `local-stream-facts.json` first, in a locally approved,
offline, non-AI plain-text editor. Never open the stream files until the following
comparison passes. Do not guess a missing field, infer successful capture from an
empty stream, or accept a different schema because the ciphertext decrypted.

1. Require exactly these nine top-level fields: `schema`, `promotable`, `repository`,
   `commit_sha`, `run_id`, `run_attempt`, `whatif_exit_code`, `recipient_sha256`, and
   `streams`. Require `schema="optima-foundation-private-capture-v1"` and boolean
   `promotable=false`. Reject duplicate, unknown, missing, or malformed fields at
   every level; do not use a JSON viewer that silently collapses duplicate keys.
2. Require string `repository`, `commit_sha`, `run_id`, and `run_attempt`, matching
   `$Repo`, `$ExpectedHead`, `$RunId`, and `$Attempt` exactly. Run ID and attempt are
   decimal strings, not JSON numbers. Workflow/path/job/actor bindings come from
   the independent GitHub checks and reviewed source above, not manifest fields.
3. Require string `recipient_sha256`, exactly 64 lowercase hexadecimal characters,
   matching the independently verified owner certificate's DER SHA-256. Do not
   substitute the ZIP digest or a public-key-only fingerprint.
4. Require `whatif_exit_code` to be a JSON integer in `0..255`, not a boolean,
   string, or fractional number. The reviewed workflow's EXIT trap captures `$?`
   after the CLI call and passes it to `seal` before cleanup. There is no independent
   fixed numeric status record to compare. Do not equate it with the final step/job
   status: capture or cleanup can fail later. Nonzero remains a failed plan; zero
   proves neither classification success nor promotability.
5. Require `streams` to contain exactly `whatif.stdout` and `whatif.stderr`, each an
   object with exactly `length` (JSON integer in `0..67108864`) and `sha256` (string
   of 64 lowercase hexadecimal characters). Match length and hash to the respective
   `local-stream-facts.json` entries. No truncation marker or partial/missing stream
   is acceptable. Never display arbitrary manifest values in a terminal or chat.

After those checks pass, open `whatif.stdout` and `whatif.stderr` only in that same
offline non-AI text editor. Select files through the editor's local file-open dialog;
do not execute a command, filename, or URL supplied by the plaintext. Treat every
message as untrusted data. Disable link activation, previews, plugins, cloud features,
and automatic submissions. Never paste raw prose, identifiers, digests, run IDs,
URLs, secrets, or plaintext fragments into chat. Release only a human-approved,
fixed redacted summary such as `Private review completed; plan remains failed.`

### Retention, failure limits, and owner cleanup

Encryption leaves length and linkage metadata visible: approximate plaintext size,
artifact timing, repository/run/attempt association, and recipient key identifier.
Neither a one-day retention setting nor environment approval guarantees erasure of
publicly copied archives. Public copies may persist indefinitely, and a future
compromise of the private key can expose those copies. This is not forward secrecy.

If the key or passphrase is lost, the runner is killed before sealing, either stream
exceeds its bound, preservation/sealing/upload fails, or the ciphertext expires
before download, the route cannot recover missing bytes. Forced termination can
also prevent cleanup. Do not promise guaranteed erasure or recovery, infer the lost
historical cause, or automatically rerun a what-if.

After private review, the owner deletes plaintext streams, manifest, TARs, partial
quarantine output, CMS structure rendering, listings, synthetic leftovers, and local metadata from the private
directory, including editor recovery files, under the approved data-handling policy.
Deletion on SSDs and BitLocker is not proof of forensic erasure. Keep the encrypted
private key only under the separately approved owner custody policy; do not delete
it as an accidental side effect of deleting an inspection directory.

Deletion of retained local ciphertext or GitHub cipher artifacts requires a separate
approved owner cleanup action. No automatic artifact deletion, cleanup command,
dispatch, credential change, or configuration change is authorized now. Owner deletion
cannot revoke public archive copies. Record only an approved safe summary and cleanup
confirmation outside private storage. Failure remains nonpromotable, and any later
operation needs separate authorization.

## Pinned source evidence

CLI 2.89.1 uses deployment SDK 1.0.0b1 with REST 2025-04-01. CLI conversion flattens `properties.diagnostics` to `diagnostics`; SDK optional attributes may serialize as null.
The upstream examples establish the one display form, not this repository's historical cause.

* [CLI dependency pin](https://github.com/Azure/azure-cli/blob/d3d81d5007da1688e8dc1e65d852151176fad757/src/azure-cli/requirements.py3.Linux.txt)
* [CLI JSON conversion](https://github.com/Azure/azure-cli/blob/d3d81d5007da1688e8dc1e65d852151176fad757/src/azure-cli-core/azure/cli/core/util.py#L674)
* [SDK diagnostic models](https://github.com/Azure/azure-sdk-for-python/blob/11f7eb8bee01b9d7a9b3fdefec9610f8d2521326/sdk/resources/azure-mgmt-resource-deployments/azure/mgmt/resource/deployments/models/_models_py3.py)
* [SDK API default](https://github.com/Azure/azure-sdk-for-python/blob/11f7eb8bee01b9d7a9b3fdefec9610f8d2521326/sdk/resources/azure-mgmt-resource-deployments/azure/mgmt/resource/deployments/_configuration.py)
* [REST diagnostic schema](https://github.com/Azure/azure-rest-api-specs/blob/8338afaf221a4ca1cc2a134cdc38952bdd5c7245/specification/resources/resource-manager/Microsoft.Resources/deployments/stable/2025-04-01/deployments.json)
* [Published formatter example](https://github.com/Azure/azure-cli/blob/d3d81d5007da1688e8dc1e65d852151176fad757/src/azure-cli/azure/cli/command_modules/resource/tests/latest/test_resource_formatters.py#L577)
* [Published sanitized recording](https://github.com/Azure/azure-cli/blob/d3d81d5007da1688e8dc1e65d852151176fad757/src/azure-cli/azure/cli/command_modules/resource/tests/latest/recordings/test_subscription_level_what_if_with_short_circuiting.yaml#L429)

The following command references support the selected procedure, not evidence that
the local owner proof has run. Keep same-profile Windows/Linux test results and
owner proof with the implementation review before authorizing setup or dispatch;
do not label them direct cross-version exchange evidence without a separate test.

* [OpenSSL 3.0 CMS options](https://docs.openssl.org/3.0/man1/openssl-cms/)
* [OpenSSL 3.5 CMS options](https://docs.openssl.org/3.5/man1/openssl-cms/)
* [OpenSSL 3.5 RSA key generation](https://docs.openssl.org/3.5/man1/openssl-genpkey/)
* [OpenSSL 3.5 certificate request options](https://docs.openssl.org/3.5/man1/openssl-req/)
* [OpenSSL 3.5 certificate extensions](https://docs.openssl.org/3.5/man5/x509v3_config/)
* [OpenSSL 3.5 RSA OAEP options](https://docs.openssl.org/3.5/man1/openssl-pkeyutl/)
* [GitHub artifact metadata and archive API](https://docs.github.com/en/rest/actions/artifacts)
* [GitHub workflow run attempts API](https://docs.github.com/en/rest/actions/workflow-runs)
* [GitHub workflow job attempts API](https://docs.github.com/en/rest/actions/workflow-jobs)
* [GitHub immutable artifact digest and retention](https://github.com/actions/upload-artifact#outputs)
