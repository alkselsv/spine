# R1 criterion R2 deployment qualification

This procedure qualifies only criterion R2 from Issue #16. It does not promote
R3–R17. A previous report, a healthy container list, or a self-declared `PASS`
field is not R2 evidence.

## Required authority and target

Use one new isolated deployment whose runtime profile is approved by the
operator and whose exact commit and deployment fingerprint are known before
startup. Supply runtime secrets through the ignored `.env` file and bounded
qualification/provider authority through `.env.qualification`. Never place
their values in the evidence artifact, command output, issue, or repository.
The current Compose authority is development-only; staging or production must
use a future environment-specific authority and collector contract and cannot
be selected by this workflow.

The live target must contain the API, Web, Q&A worker, outbox dispatcher,
PostgreSQL, Temporal, original storage, and the selected provider. Every
component observation carries its actual version or release identity, artifact
or configuration digest, and the same deployment fingerprint. A fallback or
legacy path makes the run fail.

## Live observation

Provision qualification authority before runtime startup, then start the exact
deployment using the established Compose procedure:

```bash
docker compose --env-file .env --env-file .env.qualification \
  --profile qualification-provisioning run --rm \
  qualification-authority-provisioner
docker compose --env-file .env --env-file .env.qualification \
  up --build --detach --wait
```

The main-only `r2-deployment-qualification` workflow owns provisioning, startup,
the execution ID, Docker inspection and authenticated HTTP probes. Configure its
protected `r2-qualification` environment with `SPINE_R2_RUNTIME_ENV` and
`SPINE_R2_QUALIFICATION_ENV`, then dispatch it from `main` with the exact
fingerprint, environment and accountable principal. The workflow runs
`spine-r2-deployment-evidence`; it accepts no caller-authored component receipts.

The collector derives running container/image identities and the shared Compose
network, verifies the deployment profile inside API and worker containers,
counts qualification-authority rows before and after startup/readiness, and scans
the instrumented worker boundary for actual outbound provider attempts. Its
authenticated health-summary canary must echo the requested workspace and
environment scope. It also requires invalid credentials and an unknown foreign
workspace/environment scope to be denied before authentication and scoped
readiness can be recorded as successful.

Capture `r2-deployment-evidence:v1` only from that live run. Readiness must be
authenticated and scope-bound, perform zero authority mutations, and make zero
provider calls. Run the separate authenticated bounded canary through the
configured application path. The export contains metadata only: no credentials,
network locations, protected content, provider payloads, or raw errors.

The evidence artifact must be produced and attested by the trusted `main`
workflow. Independent verification uses `gh attestation verify`; repository
presence or a matching JSON digest alone is insufficient.
If provisioning, startup or a probe fails, the workflow still attests a bounded
`r2-deployment-collection-failure:v1` artifact and independently emits the
criterion result. Missing configuration or an unavailable probe maps to
`BLOCKED`; an observed mismatch or runtime failure maps to `FAIL`. Raw command,
HTTP and provider diagnostics never enter either artifact.

## Independent decision

Download the exact artifact and its expected SHA-256 digest, then run:

```bash
uv run spine-r2-deployment-verify \
  --artifact r2-deployment-evidence.json \
  --artifact-digest <sha256> \
  --head-sha <40-character-main-commit> \
  --deployment-fingerprint <64-character-fingerprint> \
  --output r2-deployment-qualification-result.json
```

Exit `0` means the independently recomputed result is `R2 PASS`. Exit `1`
means an evaluated `FAIL`, `BLOCKED`, or `NOT_VERIFIED`; inspect only bounded
`reason_codes`. Exit `2` means the input could not be evaluated.

Status derivation is fail-closed:

- observed digest, target, component, readiness, canary, fallback, or privacy
  mismatch is `FAIL`;
- an unavailable required component/readiness/canary is `BLOCKED`;
- a missing live execution link or untrusted attestation is `NOT_VERIFIED`;
- `PASS` requires every component and observation plus trusted provenance.

After integration, run this procedure on the integrated `main` commit. Preserve
the resulting artifact, attestation bundle, exact digest, bounded component
manifest, and independently reproduced result as a new immutable follow-up.
Do not edit the historical Issue #511 or #560 records.
