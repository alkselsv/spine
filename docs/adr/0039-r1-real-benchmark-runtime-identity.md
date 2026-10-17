---
status: accepted
---

# R1 real benchmark runtime identity and observation boundary

This decision reuses Spine's canonical source, authorization, projection, Q&A,
and evaluator contracts to run the approved real R1 dataset locally. It keeps
the dataset's opaque identities alongside canonical identities, provisions the
corpus through canonical ingestion and projection, and joins gold labels only
after a validated production observation is frozen. The technical architecture
decisions A–F below were explicitly approved by the authorized project
participant who initiated the decision request on 2026-10-17. This approval
covers architecture and implementation contracts only. It does not authorize
use of particular private documents, create identities or grants, approve an
AccessPolicy change, or establish runtime readiness.

## Decision status

| Decision | Status | Approval authority |
| --- | --- | --- |
| Dataset alias mapping and staged binding | Accepted technical architecture | Authorized project technical/architecture approver |
| Trusted runtime scope | Accepted technical architecture | Authorized project technical/architecture approver; actual identity/policy verification remains security-owned |
| Canonical corpus preparation | Accepted reuse of existing lifecycle | Authorized project technical/architecture approver; actual data use and operations remain separate |
| Production observation seam | Accepted reuse of authorized Q&A boundary | Authorized project technical/architecture approver |
| Outcomes and latency | Accepted reporting contract | Authorized project technical/architecture approver |
| Evaluation join and label semantics | Accepted semantics constrained by #443; no release thresholds approved | Authorized project technical/architecture approver; threshold use remains separately governed |

## Approval matrix and deferred runtime authority

| Decision | Accepted choice | Approval authority | Current approval evidence | Missing approval | Blocks #448 contract implementation | Blocks real runtime execution |
| --- | --- | --- | --- | --- | --- | --- |
| T — architecture integration | Staged mapping, trusted-context boundary, existing Q&A/projection paths and versioned interface map | Authorized project technical/architecture approver | Initiator explicitly approved A–F in the request dated 2026-10-17; recorded by this accepted ADR | None for architecture contract | No; #448 may start after #447 closes | Runtime prerequisites below remain mandatory |
| A — mapping | Private `r1-runtime-mapping-manifest:v1`; verified canonical binding after ingestion | Technical approver for contract; data owner for actual corpus use | Explicit technical approval dated 2026-10-17 | Exact document-use permission and private manifest remain runtime prerequisites | No | Yes; owner-approved mappings and source-use authority required |
| B — trusted scope | Existing dev-local auth through canonical PostgreSQL directory; operator config is selector only | Technical approver for boundary; security owner for actual identity/scope/policy verification | Explicit technical approval dated 2026-10-17; WSL configuration remains unverified | No architecture approval missing; runtime security verification remains | No | Yes; identity, memberships, grants, generation and current policy must be verified |
| C — corpus lifecycle | Existing canonical ingestion, acceptance, projection, activation and exact membership reads | Technical approver for path; data/operations owners for actual run | Existing lifecycle ADRs and explicit technical approval dated 2026-10-17 | No architecture approval missing; operational/data prerequisites remain | No | Yes; current revisions and exact active-snapshot membership required |
| D — observation | Existing authorized Q&A path with typed in-flight capture | Authorized project technical/architecture approver | Explicit technical approval dated 2026-10-17; persisted run-history gap verified | None for selected boundary | No | Yes; actual capability/runtime composition and capture must be configured |
| E — disposition/latency | Typed execution disposition and bounded observer-call duration; queue and attempts separate | Authorized project technical/architecture approver | Explicit technical approval dated 2026-10-17 | None for selected reporting semantics | No | Execution must supply measurements; no runtime readiness asserted |
| F — evaluation | Gold joins after frozen observation; full citation recall set; explicit claim links; zero-class N/A | Technical approver for semantics; threshold owner for release use | Explicit technical approval dated 2026-10-17; semantics constrained by #443 | No approval missing for these semantics; release thresholds are outside this ADR | No | Real evaluation requires approved dataset access; thresholds remain separate |

The approval recorded here is the initiator's explicit technical architecture
approval, not approval by another person or role. It does not substitute for
data-owner permission to use the private corpus, security verification of
existing canonical identity and AccessPolicy, or operations verification of
local runtime configuration. Those are preconditions for real provisioning or
execution, not deterministic fake-port contract work in #448. Existing accepted
ADRs govern canonical lifecycle and authorization invariants; they do not
approve benchmark-specific identities or real-data access.

## Existing facts and verified seams

- `SourceObject` and immutable `SourceRevision` are canonical, tenant-scoped
  identities in `src/spine/domain/sources/models.py`. No dataset-alias registry
  or opaque real-dataset ID field was found.
- `DocumentUploadCommand.upload_identity` in
  `src/spine/application/document_ingestion.py` is an explicit logical upload
  identity. The ingestion orchestrator resolves/creates canonical source and
  revision identities and persists observations. Its UUID derivation is an
  existing upload identity policy; it does not authorize deriving identities
  from dataset IDs, filenames, or digests.
- `CanonicalAcceptanceCoordinator.accept` records canonical acceptance and
  current-head changes. `resolve_current` is the currentness seam. Parsing,
  acceptance, currentness, projection, and retrieval readiness are separate
  states.
- Ingestion emits a content-free projection request only after successful
  processing. `ProjectionRequestConsumer` resolves it against scoped canonical
  state. `ProjectionPublicationCoordinator` validates and seals a candidate,
  then activates it through the PostgreSQL publication repository. The
  projection repository exposes active publication and snapshot membership
  reads. Direct vector-index writes bypass accepted lifecycle controls.
- `compose_local_api_authorization` is development-only; OIDC and local
  authorization composition both use `RequestAuthorizationComposer`,
  `PostgreSQLAuthorizationDirectory`, current membership/role records, and
  `TrustedRequestContextBoundary`. No benchmark-specific CLI identity issuer
  was found. A local config value cannot itself grant authority.
- The requested WSL runtime probe found all five `SPINE_LOCAL_AUTH_*` settings
  absent from the current process, `SPINE_DATABASE_URL` absent, and `.env` /
  `.env.local` absent. `LocalAuthenticationSettings` sets `env_file=None`, so
  `.env.example` placeholders do not configure the adapter. No credential was
  printed or inspected. Without the configured PostgreSQL runtime, the local
  authentication alias cannot be resolved and workspace/environment
  membership, route grants, authorization generation, and current `AccessPolicy`
  cannot be verified. Runtime readiness is therefore **unverified / not ready**.
- `api/main.py:create_app` is the injectable API composition root.
  `install_authorized_ask_routes` invokes `PersistedAuthorizedAskService`,
  whose capability is a port. The repository does not contain a turnkey
  benchmark assembly of every production dependency.
- `AuthorizedContextBroker` is the retrieval path. The context service persists
  immutable bundles, validates them, revalidates disclosure dependencies, and
  passes accepted answers through the existing disclosure gate.
- `ProductionContractObservation` v1 requires the actual question, Context
  Response, bundle, projection retrieval, retrieval result, answer, and
  disclosure. `PersistedQARunHistoryService.get_run` exposes run/attempts,
  bundles, and accepted evidence after disclosure revalidation, but does not
  expose `ContextResponse`, `ProjectionRetrievalResult`, or `RetrievalResult`.
  `AnswerQuestionAttempt` stores safe outcome, start/completion times, bundle
  reference and artifacts, not the full retrieval record. Persisted history
  alone therefore cannot satisfy observation v1.
- Existing Q&A terminal outcomes are `answered`, `clarification_required`,
  `abstained`, `denied`, `failed`, and `cancelled`. Retrieval separately has
  `evidence_available`, `insufficient_evidence`, `unavailable`, and `failed`.
  `unavailable` is not a Q&A terminal outcome.
- The existing synthetic `QACase` requires `synthetic=True` and UUID identities.
  Real opaque identity must not be coerced into that contract. The current
  production case request is gold-label-free but uses canonical UUID runtime
  scope and revisions. Real evaluation needs a versioned adapter/contract if
  preserving opaque identity and provenance cannot be done losslessly.
- The approved dataset has 136 answered cases, no abstained/denied cases, and
  19 supplementary expected citations without explicit claim-evidence links.
  #443 requires the full approved citation set for Citation Recall and explicit
  1:1 claim-evidence relationships for groundedness; empty abstained/denied
  slices are not applicable with `eligible_count=0`.

Relevant accepted architecture records include ADR-0011 (AccessPolicy),
ADR-0018 (source/projection lifecycle), ADR-0021 (request authorization),
ADR-0025–0028 and ADR-0030–0031 (projection lifecycle), ADR-0029 and ADR-0033
(Context Bundle and disclosure), ADR-0034 (authorized Context Broker),
ADR-0035 (resumable Q&A events), and ADR-0038 (atomic Q&A admission).
None defines a real-dataset alias registry or a ready-made benchmark runtime.

## Accepted decisions A–F

### A. Dataset identity mapping

**Accepted decision.** A data owner supplies a private, immutable,
versioned `r1-runtime-mapping-manifest:v1`, selected explicitly by the local
operator and pinned by content digest in local run metadata. The manifest maps
`(dataset_version, dataset_digest, opaque_source_id)` to an owner-approved
`upload_identity`; revision records separately map opaque revision IDs to an
ingestion intent and expected source digest as a content-integrity check. The
digest is not identity proof. The manifest contains no generated canonical
UUIDs before ingestion.

Use four explicit stages:

1. **Intent/registration:** validate the approved manifest and unique opaque
   source/revision keys. Require exactly one approved upload identity per
   dataset source and one ingestion intent per selected dataset revision.
2. **Canonical ingestion:** call `DocumentIngestionOrchestrator` with trusted
   workspace/environment, approved source bytes, the explicit upload identity,
   and stable idempotency input. Read the returned/persisted canonical IDs; do
   not derive IDs from dataset identifiers, filenames, or digests.
3. **Verified binding:** persist a local binding record that retains opaque IDs
   and actual `SourceObject`/`SourceRevision` UUIDs, plus source digest,
   canonical acceptance reference, and manifest digest. Enforce one-to-one
   binding for selected dataset revisions and reject collision, missing, or
   ambiguous source resolution. Repeated runs reuse an exact verified binding;
   they do not silently rebind.
4. **Currentness/readiness:** before observation, resolve the current canonical
   head and active snapshot for the exact workspace/environment. Require the
   bound revision to remain current and to appear exactly in the active healthy
   publication. Any change invalidates readiness and requires explicit
   re-provisioning/rebinding.

The source crosswalk belongs to the data owner; the local benchmark runtime
reads it read-only. Run metadata pins manifest and binding digests; canonical
ingestion/acceptance audit records remain the source of truth for canonical
source changes. The local mapping is never an authorization grant. A private
manifest is a smaller first-run mechanism than adding a permanent canonical
alias registry; the trade-off is that the crosswalk itself is locally audited
by immutable version/digest and run history, rather than stored as a new
canonical PostgreSQL business record. If policy requires central append-only
audit for every alias binding, that is a separate decision and schema work.

### B. Trusted runtime scope

**Accepted decision.** Use the existing authorized API composition path with the
existing development-only local adapter for the first local benchmark, as
requested by the coordinator. A local operator may select an approved
workspace/environment in run configuration, but the selection is only a
request: the configured authentication alias must resolve through the canonical
PostgreSQL authorization directory to an existing human identity with current
membership, role, route grant, and authorization generation. Obtain subject,
workspace, environment, and generation only from the issued request context.
Resolve the applicable current `AccessPolicy` at retrieval and disclosure
boundaries. Dataset fields cannot select scope or principal. Do not create
identities, admins, or service principals automatically, and do not widen
service-principal authority. This choice does not assert that local auth is
currently configured: the WSL readiness probe found required auth and database
configuration absent, so mapping the alias and verifying the target scope and
policy remain runtime prerequisites. A benchmark deployment that cannot meet
these conditions fails before observation.

Option A (operator configuration plus canonical validation) is acceptable
only as a scope selector feeding this authorization root, never as an issuer.
Option C has no verified existing implementation.

### C. Corpus provisioning

**Accepted decision.** Keep the existing canonical lifecycle and report each
state independently:

`ingestion_succeeded` → `revision_accepted` → `revision_current` →
`projection_request_processed` → `projection_candidate_validated_and_activated`
→ `revision_in_active_snapshot` → `retrieval_ready`.

Use the supported upload/ingestion orchestrator, canonical acceptance
coordinator, persisted processing receipt and outbox projection request,
projection request consumer, configured Context Broker/Cognee adapter,
physical validation/sealing, PostgreSQL activation, and exact snapshot-member
read. For all required revisions, verify source/environment/workspace, current
head, active publication health/freshness, and exact membership before a
benchmark case is eligible. `retrieval_ready` additionally requires a trusted
request and current authorization; it cannot be inferred from a completed
ingestion or projection receipt alone. Provisioning time is reported
separately from Q&A latency.

### D. Production Q&A observation

**Accepted decision.** Use `api/main.py:create_app` and its existing authorized
ask route as the trust/composition boundary; do not call Cognee or introduce a
second retrieval flow. Build the observer adapter at the existing
`ProductionContractApplicationFlow` seam and compose the same production
`PersistedAuthorizedAskService`, `AnswerQuestionCapability`, authorized
Context Broker, and disclosure gate used by the API. Issue the request with
trusted context from the existing auth composer.

Before implementation, the production owner must identify the actual
capability/runtime adapter and whether this runtime is synchronous or admitted
to durable dispatch. The persisted run-history read contract is appropriate
for safe run identity, attempts, bundle and disclosure revalidation, but it
does not supply all observation v1 values. The minimal capture seam must
therefore return the typed `ContextResponse`, `ProjectionRetrievalResult`, and
`RetrievalResult` from the actual in-flight authorized broker call, alongside
the final persisted/revalidated answer and disclosure. Persisting protected
retrieval payloads merely to satisfy evaluation is not proposed. If that
in-flight typed seam cannot be exposed safely, #450 must propose a separately
versioned observation contract with narrower fields and prove equivalent
lineage; it must not fabricate missing values from run history.

### E. Outcomes and latency

**Accepted decision.** A versioned per-case execution record has one of:

- `observed`: a validated `ProductionContractObservation`, whose terminal
  outcome is the existing `AnswerQuestionOutcome`;
- `unavailable`: preflight/runtime prerequisite unavailable, with a bounded
  reason such as mapping missing, stale revision, or projection unavailable;
- `denied`: authorization denied by the existing boundary, with no protected
  answer/context content;
- `failed`: execution failed with a safe category and correlation reference.

Mapping/schema errors remain structural/preflight categories; they are not
Q&A outcomes. An observation can carry `abstained` as the existing terminal
outcome. Cancellation is reported from the existing terminal outcome and
execution record; it is not rewritten as failure. Partial execution failure
retains any completed attempt metadata but is not evaluated as a successful
answer.

The primary `observer_elapsed_ms` starts immediately before one
bounded `ProductionContractApplicationFlow.execute` invocation and stops after
the returned `ProductionContractObservation:v1` passes validation, or after the
invocation returns control to the outer observer, which records a safe typed
execution disposition. The existing `execute` contract remains
observation-only; `r1-case-execution-record:v1` is the outer observer result
that wraps a validated observation or a denied/unavailable/failed disposition.
This decision does not change the flow method signature or Q&A terminal enums.
It includes authorized retrieval, answer generation, validation and
disclosure revalidation performed
inside that call, including internal retries/backoff. It excludes mapping load,
canonical ingestion, projection build/activation, gold join, metric calculation
and report writing. Preflight mapping or projection failures and authorization
denial detected before the flow call are represented by the outer execution
record and have no observer latency because execution did not start. An in-flow
Q&A denial or abstention retains the existing terminal outcome inside the
observation. For durable admission, report dispatch/queue wait separately
and retain per-attempt `started_at`/`completed_at` from
`AnswerQuestionAttempt`; retries are separate attempt measurements, not
silently folded into one attempt. Cancellation and partial failure record
elapsed time to their terminal event. Use monotonic clock duration for observer
elapsed time; no latency is imputed when execution never starts.

### F. Gold join and metric semantics

**Accepted decision.** Keep observation input gold-blind. Validate and freeze the
observation first; only a subsequent evaluator adapter may join expected
answers, claims, and evidence from the approved dataset. Preserve the synthetic
contracts and evaluator formulas. Add a versioned real evaluation adapter if
opaque source/revision IDs or citation provenance cannot pass existing
contracts without information loss.

Citation Recall uses all approved expected citations, including the 19
supplementary references. Those references do not become claim-level support.
Groundedness uses only explicit `ClaimEvidenceRelationship` links. Because
there are no eligible abstained or denied gold cases, those metric slices are
`not_applicable` with `eligible_count=0`. These semantics are required by #443;
Product/Evaluation Owner confirmation is still required before citation-recall
results are used for thresholds. No threshold or release policy is changed.

## Versioned contract examples (synthetic placeholders only)

The following examples show identity separation and gold isolation. The mapping
manifest and binding are accepted versioned interfaces for implementation, not
current repository types. `owner_approval_ref` is an approval reference supplied by an authorized
data owner; it is not a current Spine contract.

```json
{
  "schema_version": "r1-runtime-mapping-manifest:v1",
  "dataset": {
    "version": "example-approved-version",
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "owner_approval_ref": "approval-record-123",
  "sources": [
    {
      "dataset_source_id": "source:opaque-example",
      "upload_identity": "approved.logical.source",
      "revisions": [
        {
          "dataset_revision_id": "revision:opaque-example",
          "expected_original_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "ingestion_intent_id": "intent:example-01"
        }
      ]
    }
  ]
}
```

```json
{
  "schema_version": "r1-runtime-revision-binding:v1",
  "manifest_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "dataset_source_id": "source:opaque-example",
  "dataset_revision_id": "revision:opaque-example",
  "workspace_id": "<UUID from issued runtime context>",
  "environment_id": "<UUID from issued runtime context>",
  "source_object_id": "<canonical UUID returned by ingestion/readback>",
  "source_revision_id": "<canonical UUID returned by ingestion/readback>",
  "canonical_acceptance_ref": "acceptance:example-01",
  "original_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

```python
ProductionContractCaseRequest(
    schema_version="r1-production-contract-case-request:v1",  # existing
    case_id="case:opaque-example",  # preserve dataset case ID as an opaque string
    question="Example question with no gold labels",
    language="en",
    workspace_id=issued_request_context.workspace_id,
    environment_id=issued_request_context.environment_id,
    principal_id=issued_request_context.acting_subject_id,
    available_revision_ids=verified_binding.canonical_revision_ids,
    retrieval_strategy="server-resolved",
)
```

Construct the existing gold-blind v1 production request only after the
real-case adapter has retained the opaque case/source/revision identities,
resolved canonical revision IDs, and obtained a request-bound scope from the
existing authorization composer. These IDs are actual values read from
canonical records/context, not placeholders or dataset-selected authority.
`ProductionContractCaseRequest.from_case` remains for synthetic `QACase`; no
change to that constructor or synthetic contract is proposed. A separately
versioned real evaluation input is needed only where existing evaluator inputs
would otherwise lose real identity or provenance.

## Accepted versioned interface map for #448–#453

These names are accepted interface definitions for implementation planning;
they are not repository types until implemented. Keep them gold-blind until the
post-observation evaluation boundary.

| Ticket | Interface | Status and contents |
| --- | --- | --- |
| #448 | `r1-runtime-mapping-manifest:v1`; `r1-runtime-revision-binding:v1`; existing `r1-production-contract-case-request:v1` | Implemented in `spine.evals.real_qa_r1`: manifest intent and persisted verified bindings retain opaque IDs, upload/ingestion provenance, actual canonical IDs, acceptance reference, manifest and binding digests, and currentness. Exact bindings are reused on replay; conflicting rebinding fails closed. The adapter builds the existing gold-blind production request only after canonical readback and trusted request-context issuance. Synthetic `QACase` and request behavior are unchanged. |
| #449 | `r1-corpus-readiness:v1` | Accepted per-revision interface for ingestion, acceptance, current head, projection processing, activation, exact active membership, health/freshness, and retrieval eligibility. |
| #450 | Existing `r1-production-contract-observation:v1`; `r1-case-execution-record:v1` | `ProductionContractApplicationFlow.execute` continues returning only the validated observation. The outer observer wrapper carries that observation or a safe unavailable/denied/failed disposition, failure category, correlation, and measured durations without changing Q&A terminal enums. |
| #451 | `r1-real-evaluation-case:v1`; `r1-real-evaluation-result:v1` | Accepted adapter/output only where current evaluator inputs would lose opaque IDs, locators, or provenance. Gold joins after frozen observation; formulas remain unchanged. |
| #452 | `r1-local-benchmark-run:v1`; `r1-local-benchmark-report:v1` | Accepted run pin and metadata-only report for dataset, mapping, observer, application/projection, evaluator versions, per-case outcomes, metrics, applicability, failures, latency, and stage statuses. Detailed protected artifacts stay local. |
| #453 | `r1-benchmark-qualification:v1` | Accepted qualification record for deterministic gates and the opt-in real-flow integration result; separate from release qualification and #17 thresholds. |

## Decision matrix

| Concern | Option | Benefits | Costs / risk | Recommendation |
| --- | --- | --- | --- | --- |
| Alias storage | Private versioned mapping manifest | Local-only, explicitly selected, digest-pinned, no canonical schema | Crosswalk audit is local digest/run history, not canonical DB audit | Use for this benchmark; data-owner permission is still required for actual corpus use |
| Alias storage | New canonical PostgreSQL alias registry | Central append-only ownership/audit and uniqueness | New domain contract, migrations, lifecycle and broader scope | Choose only if policy requires central mapping records |
| Runtime scope | Operator IDs/config alone | Simple CLI | Does not authenticate or authorize | Reject as authority; selector only |
| Runtime scope | Existing API auth composition | Canonical membership, policy and generation; no identity creation | Requires configured auth and usable local runtime | Accepted |
| Observation capture | Persisted run history only | Existing safe read contract | Missing context response and raw retrieval records | Insufficient for observation v1 |
| Observation capture | Typed in-flight capture from existing authorized flow | Full actual lineage without second retrieval path | Requires safe composition seam and runtime ownership | Accepted |
| Latency | Observer bounded call | Reproducible across observer outcomes; matches #443 | Durable dispatch wait needs separate measure | Accepted as primary; report attempt and queue values separately |

## Runtime prerequisite checklist

- [ ] Data owner approves dataset digest, source-to-upload identity mappings,
  exact revisions, and permission to use the private corpus for this local run.
- [ ] Mapping manifest is private, versioned, validated, and its digest is
  pinned in run metadata; no private manifest or document is committed or sent
  to GitHub.
- [ ] The development-only local adapter is explicitly configured through the
  process environment or trusted composition settings; no automatic OIDC
  fallback occurs. An existing human identity, canonical alias binding,
  workspace/environment memberships, route permissions, authorization
  generation, and applicable `AccessPolicy` are verified in PostgreSQL. No
  principal or permission is created by the benchmark.
- [ ] Security/authorization owner approves selected workspace/environment,
  applicable AccessPolicy, authorized source access, and authorization
  generation behavior.
- [ ] Operations/runtime owner supplies supported PostgreSQL, object storage,
  parsing, projection worker, Context Broker/Cognee adapter, model/runtime and
  secret configuration without granting broader authority.
- [ ] Every ingestion result is accepted and current; every required canonical
  revision is an exact member of the active healthy snapshot.
- [ ] The same production Q&A composition root and disclosure revalidation are
  active; the observer can capture all typed v1 observation fields in flight.
- [ ] Gold files are unavailable to the observer process/interface until the
  validated observation collection is frozen.

## #448 implementation versus real-run prerequisites

Writing #448 unit/domain/contract code does **not** require a live PostgreSQL
instance or the private corpus. Once mapping authority, cardinality/currentness,
gold-blind request shape, and the trusted scope source are approved, those
contracts can be implemented against deterministic fake ports. #448 may start
after #447 is accepted and closed; it does not require live PostgreSQL, private
documents, or a named runtime configuration owner. The later real run still
requires all runtime and data-use prerequisites below.

Actual #449–#453 execution does require configured local auth and database,
canonical alias-to-human binding, approved workspace/environment and
route/source permissions, current authorization generation and AccessPolicy,
supported ingestion and projection services, exact active membership, and the
configured Q&A/model runtime. Missing or stale prerequisites produce a safe
not-ready/unavailable result; they do not trigger identity or permission
creation. The WSL probe found auth and database settings absent, so runtime
qualification is explicitly deferred.

## Impact and implementation handoff

- **#448:** Implement the gold-blind real mapping/scope contracts and resolver;
  explicit opaque-to-approved-upload identity intent first, canonical IDs only
  after readback. Fail on absence, ambiguity, cardinality conflict, stale head,
  scope mismatch, or authorization denial. No UUID synthesis or principal
  creation.
- **#449:** Use the ingestion, acceptance, projection request/consumer,
  publication and snapshot read seams above. Return separate lifecycle states
  and exact revision membership; no vector-index writes.
- **#450:** Reuse the API auth and authorized Q&A flow. Locate/configure the real
  capability adapter; add only an in-flight typed observation capture seam if
  necessary. Persisted run history may corroborate, but cannot replace, missing
  retrieval contracts. Keep gold inaccessible.
- **#451:** Version a real evaluation input adapter only where existing
  synthetic UUID/source assumptions lose opaque identity or provenance. Keep
  formulas and synthetic runner unchanged; preserve full citation recall and
  explicit groundedness links.
- **#452:** Select an approved dataset explicitly; pin dataset, mapping,
  observer, evaluator, application and projection versions; distinguish
  structural validation, observation, evaluation and release qualification;
  keep detailed output private.
- **#453:** Exercise mapping failures, workspace isolation, access denial,
  missing/stale projection, invalid lineage/citations, no evidence, partial
  failures, cancellation, repeated runs, no-gold leakage and synthetic
  regression compatibility. Add an opt-in actual composition-root runbook.

Minimum implementation order remains #448 → #449; #450 follows both; #451
follows #448 and can proceed in parallel with #449/#450 after its contract
boundary is agreed; #452 follows #450 and #451; #453 follows #452. These
interfaces are the accepted architecture for those tickets.

## Deferred runtime prerequisites (not architecture approvals)

1. A data owner must authorize actual local corpus use and provide the private,
   versioned mapping manifest. This ADR accepts its local digest-pinned audit
   boundary for this benchmark; a canonical registry requires a separate
   decision only if an applicable binding policy requires it.
2. The existing development-only local adapter is selected, but
   the current WSL process lacks all required auth settings and the database
   URL. Operations must provide the protected configuration; security must
   confirm an existing canonical alias binding, human identity, approved
   workspace/environment memberships, route permissions, authorization
   generation, and applicable `AccessPolicy`. No identity or permission is
   named or presumed here.
3. The accepted in-flight typed observation seam must be implemented through
   the existing authorized flow; persisted run history alone cannot supply all
   observation v1 fields.
4. Real evaluation requires authorized access to the approved labels. Use of
   metrics for release thresholds remains subject to the separate threshold
   policy and Issue #17; this ADR grants no such approval.
5. Operations must verify local database/auth/model configuration and canonical
   ingestion/projection services. The current WSL runtime remains unverified.
