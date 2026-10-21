# Spine production deployment composition contract

**Contract ID:** `spine-production-deployment-composition:v1`
**Status:** **ARCHITECTURE APPROVED — recorded by `dima55279` based on team lead approval communicated outside GitHub on 2026-10-21; [approval record](https://github.com/alkselsv/spine/pull/528#issuecomment-6317606512).**
**GitHub review:** This participant-authored comment records the communicated decision; it is not a formal GitHub review from the team lead. Required PR reviews and branch protections remain independent gates.
**Deployment status:** Not implemented or qualified. Security, Runtime/Provider, Storage/Recovery and operator-specific approvals remain prerequisites for their respective bindings and physical operations.
**Scope:** the approved production-owned composition boundary needed by existing API, durable Q&A, projection, and recovery contracts. This document defines deployment wiring; it does not implement it or authorize production deployment or physical operations.

## 1. Problem and relationship to existing decisions

Spine already has production application capabilities and infrastructure adapters, but no reviewed, settings-backed process composition has been demonstrated against the local SOURCE/RESTORE deployment. #527 adds a real Temporal SDK worker-registration factory to the existing durable Ask graph. It does not add a configuration-backed API/worker process, outbox loop, trusted local qualification identity, provider deployment profile, or backup/restore operator entrypoint.

ADR-0040 approves one shared production Q&A capability composition for API and observer, and #506 specifies a narrow qualification factory. The team lead has approved the broader settings-backed deployment ownership and process boundary across API, worker, dispatcher, projection and recovery as recorded above. This records architectural approval only; it does not mean the root/lifecycle is implemented, formally reviewed by the team lead on GitHub, or cleared for production or physical operations. The contract does not replace the approved durable Ask path, introduce another runtime, redefine authorization, or create a backup subsystem. It separates implementation evidence from deployment and release evidence. It does not change Issue #509 acceptance criteria.

### Approved architecture this contract preserves

- **ADR-0010:** explicit capability bindings; no module auto-discovery or executable paths from stored configuration; Temporal remains the only durable workflow engine.
- **ADR-0021:** authenticated aliases resolve to canonical identities and current Workspace/Environment memberships and grants in PostgreSQL. Token selectors, roles, and groups do not create authority.
- **ADR-0022:** one coordinated fence and quiescence lifecycle; immutable manifests and evidence; restore verification is isolated; incomplete or ambiguous evidence fails closed.
- **ADR-0027:** physical projection repair uses canonical revision lineage, never resurrects stale/tombstoned sources, and exposes safe typed results.
- **ADR-0038:** PostgreSQL atomically admits Ask and writes the outbox intent; Temporal owns execution, retries, cancellation, and terminal arbitration.
- **ADR-0040:** one production `AnswerQuestionCapability` composition is shared by the API and observer; observer output is bounded and disclosure is revalidated.
- **#506 / Issue #16:** one narrow production-capable profile, trusted readiness, API/worker composition, and operator-controlled deployment evidence. #16 remains the system-level qualification gate.

These decisions are not reopened here. The extension from the approved shared Q&A graph and #506 qualification factory to one deployment-owned root spanning separate processes and recovery entrypoints is architecturally approved by the recorded team lead decision. Implementation, process configuration and owner-specific binding approvals remain outstanding.

## 2. Verified implementation baseline

| Area | Existing implementation | Remaining deployment boundary |
| --- | --- | --- |
| Durable Q&A/API | `ProductionAnswerQuestionCapability`, `PersistedAskAdmissionService` / `AskAdmissionApplication`, `compose_durable_ask`, `DurableAskComposition`, `create_api_app()` and FastAPI dependency injection | No single settings-backed production process root or API process lifecycle. `create_api_app()` delegates to the compatibility `spine.api.main.create_app()` with injected durable services; the Compose image's legacy `spine.api.main:app` alone is not the approved production composition. |
| PostgreSQL | `PostgreSQLPersistence`, `PostgreSQLTenantUnitOfWorkFactory`, tenant repositories, migrations, canonical Ask admission/outbox, typed `RuntimeDatabaseSettings` / `DispatcherDatabaseSettings` | No deployment factory assembles the role-specific settings, trusted context factories, API and worker bindings as one profile. Migration remains an explicit operator/release action, not implicit application startup. |
| Temporal | `AskAdmissionWorkflowLauncher`, `AskAdmissionWorkflowExecutor`, `TemporalSdkWorkflowClient`, `create_ask_admission_worker()` and `DurableAskComposition.create_temporal_worker()`; the latter binds workflow/activity registration to its client/task queue | No deployable worker process entrypoint/configuration/lifecycle; #527's test-server registration does not prove deployed pollers or application execution. |
| Outbox | `OutboxDispatcher`, `OutboxWorker.dispatch_batch()`, `PostgreSQLOutboxDeliveryRepository`, Ask/cancellation consumer bindings, and `create_production_outbox_worker()` for projection/recovery | No long-running runner, owned poll/lease/retry settings, coordinated shutdown, or production registry combining the required Ask and projection consumers under the approved dispatch identity. |
| Authorization | `compose_local_api_authorization()`, `OIDCAuthenticationRuntime`, development `LocalAuthenticationAdapter`, `TrustedRequestContextBoundary`, PostgreSQL authorization directory, AccessPolicy/RLS checks | Local authentication is a configured development adapter, not an approved production qualification trust source. The test/migration seed helper is not a provisioning API. No verified canonical synthetic principal/grant binding exists for this deployment. |
| Projection | Cognee 1.5.4 adapter and `CogneeContextProjection`; `build_postgresql_cognee_projection_request_consumer()` plus the recovery route; `create_production_outbox_worker()` | No process binding to an approved SOURCE/RESTORE provider profile. #507's coherent physical run is useful evidence for its exact profile, but #507 still records deployed R4 and composed live R6 as blocked. |
| Backup | `CoordinatedBackupProfile`, ADR-0022 fence/coordinator, PostgreSQL and original-storage component sources, protected artifact store, immutable manifest and read-back verification | No reviewed operator entrypoint or deployed destination, encryption/retention binding, change authority, or backup identity. Runbook explicitly forbids replacing it with direct `pg_dump`, `tar`, or ad hoc receipts. |
| Restore | `IsolatedRestoreVerifier`, isolated PostgreSQL restore, original materialization and canonical reconciliation | No configured non-serving empty RESTORE target, separate credentials, process entrypoint or deployment inventory binding. The verifier may not restore over SOURCE. |
| Readiness/evidence | `ProductionRuntimeProfile:v1`, trusted runtime preflight and `RuntimeReadinessReport`; versioned #509 Temporal/runtime reports | Existing readiness contracts do not establish API deployment, trusted auth, live pollers, completed application execution, provider qualification, recovery or release approval. |

The #506 acceptance record needs a qualification follow-up: #512 implemented the profile/readiness/preflight, and #513 added disposable PostgreSQL + Temporal integration through the durable Ask composition. The #513 comment explicitly leaves live isolated-deployment reachability and authorization unverified. #506 was later closed without that operator evidence. Likewise, #507 is closed while its report explicitly leaves deployed R4 and composed live R6 blocked. Neither closed state is a substitute for those missing artifacts. Do not reopen either issue automatically; the proposed handoff asks the owners to decide whether existing follow-up work or a new ticket should carry the residual acceptance.

### #506 acceptance reconciliation

This table distinguishes implementation/tests from deployment evidence. “Code/test” is not a production deployment PASS.

| #506 AC | Code and test evidence now present | Physical deployment evidence | Reconciled status / owner |
| --- | --- | --- | --- |
| AC1 — one factory composes API, PostgreSQL, trusted auth, original storage, outbox, Temporal worker, production Q&A and capture | Existing seams are present across `DurableAskComposition`, authorization/persistence adapters, worker factory, projection bindings and readiness/preflight. No configuration-backed root assembling the complete list was found. | No complete API/worker/operator deployment. | **PARTIAL / NOT_VERIFIED** — Runtime owner; composition factory remains an implementation gap. |
| AC2 — API, worker and observer use one production composition and fail closed on mismatches | `DurableAskComposition` pairs API admission and SDK worker client/queue; production composition evidence and observer/preflight bind to that graph. #513 integration exercises the durable Ask path. | No deployed API plus application pollers or observer in the isolated profile. | **CODE/INTEGRATION PARTIAL; deployment NOT_VERIFIED** — Runtime owner. |
| AC3 — profile declares PostgreSQL, migration, Temporal, role, auth, storage, provider/model and release identities without secrets | `ProductionRuntimeProfile:v1` validates safe non-secret version pins, including database/migration, Temporal, auth, storage, provider/model, release/commit and fingerprint fields. | No current configuration-backed profile observed against the application deployment. | **CONTRACT IMPLEMENTED; live profile NOT_VERIFIED** — Runtime owner. |
| AC4 — readiness separates configuration, reachability, authorization, bindings and qualification | `RuntimeReadinessReport` has separate config/reachability/auth/bindings/qualification states and bounded evidence references. The #509 reports add server, namespace and worker-factory distinctions. | Trusted auth, API, deployed worker, provider, recovery and release remain unobserved; #526/#527 explicitly retain NOT_VERIFIED/BLOCKED. | **PARTIAL; application/deployment readiness NOT_VERIFIED** — Runtime/Release owners. |
| AC5 — content-free trusted #453 preflight is issued only by the production composition | `DurableAskComposition.create_trusted_runtime_preflight()` verifies composition/inspector evidence before issuing the preflight. Deterministic tests cover that trust boundary. | No live isolated deployment/authenticated preflight evidence. | **CODE CONTRACT PRESENT; deployment NOT_VERIFIED** — Runtime owner; benchmark use remains separately gated. |
| AC6 — mismatch/failure tests and safe report contract | Composition/preflight/readiness tests cover mismatch/fail-closed behavior and bounded report fields; #526/#527 CI passes for the merged code. | Test output is not live authorization or deployment evidence. | **TEST/CONTRACT EVIDENCE PRESENT; physical qualification NOT_VERIFIED** — Runtime and Security owners. |
| AC7 — disposable PostgreSQL 17 + Temporal integration proves migrations, admission, outbox, worker registration and terminal state through the composition | PR #513 added and passed the disposable PostgreSQL/Temporal durable Ask integration; merged tests remain in `main`. #527 adds SDK worker registration tests. | Not a deployed isolated runtime and not proof of active deployed pollers or live provider/auth bindings. | **INTEGRATION PASS ONLY** — no physical-deployment credit. |
| AC8 — operator runs the same composition in configured isolated deployment and verifies service reachability and existing authorization | No report or verified current authorization binding was found; the #513 merge comment explicitly says these remain unverified. | Not performed. | **BLOCKED / NOT_VERIFIED** — Runtime operator and Security owner. |
| AC9 — missing provider/service/authority blocks readiness and dependent corpus/benchmark actions | Readiness/preflight contracts and deterministic tests fail closed for unavailable prerequisites. | No live unavailable-service/auth scenario on the configured application deployment; #509 reports remain blocked and no corpus/benchmark was run. | **CODE CONTRACT PRESENT; live behavior NOT_VERIFIED** — Runtime/Security owners. |

The tracker closure of #506 therefore represents merged implementation/integration work, not completion of its live operator/security-owner acceptance. Team lead must assign that residual to an existing follow-up or approve a new one; this document does neither.

## 3. Composition ownership and process topology

### 3.1 One owner and one composition boundary

**Approved ownership boundary:** the Spine Runtime owner owns the canonical settings schema, production composition factory, process entrypoints, compatible version pins, and runtime readiness. Security, Projection/Provider, Storage/Recovery, and Release owners retain authority over their respective bindings and approvals. Naming the individual accountable Runtime owner and selecting the actual external secret mechanism/access policy remain Runtime owner follow-ups.

One production-owned composition root assembles existing application and adapter implementations. It exposes process-specific entrypoints over the same versioned configuration and logical runtime contract. API, Temporal worker, and outbox are separate OS/container processes; they cannot share Python object identity. They must instead prove compatible deployment profile, commit/release identity, contract versions, Workspace/Environment scope policy, canonical database identity, Temporal namespace/task queue, and provider/recovery profile references. Process-specific credentials are expected to differ by least-privilege role.

The same root may construct only the dependencies needed by the selected process role. It must not create a second AnswerQuestionCapability, dispatcher, authorization store, persistence authority, or orchestration engine. It must not provision identities, memberships, RLS policies, grants, namespaces, databases, provider resources, or recovery destinations during ordinary startup.

### 3.2 Process contract

| Process | Existing seam to consume | Required startup / shutdown behavior |
| --- | --- | --- |
| API | `compose_durable_ask()`, `DurableAskComposition.create_api_app()`, existing authenticated API factories and canonical PostgreSQL persistence | Validate profile and secret references; verify schema and required auth-store/provider-independent reachability; expose liveness separately from scoped readiness; fail startup closed on missing or mismatched bindings. On shutdown stop admission, drain bounded in-flight HTTP work, close Temporal client, database engines and provider clients. |
| Temporal application worker | `DurableAskComposition.create_temporal_worker()` / `create_ask_admission_worker()`; existing workflow and activity definitions | Connect with the declared Temporal endpoint/namespace and exact task queue; register only approved workflow/activity definitions; report actual poller/worker lifecycle. Stop polling cooperatively and let Temporal's configured activity/workflow behavior govern in-flight durable work; close SDK client and database/provider resources. Health of Temporal SERVER alone is not worker readiness. |
| Observer/capture binding | Existing authorized observer and `CaptureChannel` contracts from #478 / ADR-0040 | Bind the observer to the same logical Q&A composition/profile identity. Use the existing authenticated ephemeral channel when the approved topology separates worker and observer; this contract does not mandate a new observer process. Register before admission; accept capture only after canonical attempt binding, same-execution correlation and current disclosure validation. Bounded timeout/disconnect cleans up the subscription and never cancels the durable Ask. Do not run the private benchmark as a startup/readiness probe. |
| Outbox dispatcher | `create_outbox_worker()` plus existing Ask/cancellation and projection/recovery `OutboxConsumerBinding`s; dispatcher-role PostgreSQL adapter | Run bounded `dispatch_batch()` iterations with explicit lease, handler timeout, retry ceiling/backoff, batch size and polling cadence. Stop claiming new deliveries on shutdown, allow bounded active handlers to settle, and rely on canonical lease/retry/idempotency semantics for interruptions. Consumers are explicitly registered; no route is discovered dynamically. |
| Projection recovery worker | Existing projection recovery request consumer in the same canonical projection outbox route | Use canonical request resolver and trusted context; call the configured `ContextProjection` only after closing the PostgreSQL UnitOfWork; record typed result through the canonical bridge. Recovery is an outbox delivery, not a direct provider call or active-pointer edit. |
| Recovery operator | `CoordinatedBackupProfile`, `IsolatedRestoreVerifier`, reconciliation and existing runbooks | A separately authorized, one-shot operator invocation (command or owner-managed job; the exact launcher is D6) receives explicit approved target references and role credentials. No public API route, automatic schedule or ordinary application startup starts backup/restore. A RESTORE target stays fenced/non-serving until immutable evidence, reconciliation and projection criteria pass. |

### 3.3 Durable application path

```text
Authenticated API request
  → canonical identity + current Workspace/Environment + AccessPolicy/RLS
  → PostgreSQL Ask admission + idempotency receipt + transactional outbox
  → outbox dispatcher (Ask admission delivery)
  → Temporal workflow in declared namespace/task queue
  → registered Spine application worker and existing Ask executor
  → canonical PostgreSQL terminal persistence and disclosure checks
```

The dispatcher delivers a run-correlated command; the workflow executor alone loads the canonical command and executes it. API admission returns acceptance, never an inline answer. The workflow owns durable retries, cancellation and terminal arbitration under ADR-0038. Activity retries remain idempotent. Terminal success for readiness requires the actual application result and canonical terminal persistence; `TERMINATED`, workflow admission, worker factory tests, or a visible worker process are not a completed application scenario.

API and worker compatibility is checked using validated identities and versions, not Python object equality: settings schema/version, profile fingerprint, release/commit and workflow contract version, database deployment/schema context, auth profile, Temporal cluster alias/namespace/task queue, and projection/model profile must match. A role-specific secret reference or database login differs by design and is checked against the permitted role for that process.

## 4. Configuration contract

This approved contract does not itself add a settings model. Implementation must build on existing frozen/strict settings types and `ProductionRuntimeProfile:v1`; do not add competing copies of fields already owned by PostgreSQL, OIDC, Temporal or recovery adapters.

### 4.1 Versioned inputs and sources

The production root consumes a strict, immutable `spine-production-runtime-settings:v1` deployment document and a separate external secret bundle. The safe report continues to use `spine-production-runtime-profile:v1` and contains only non-secret pins/identities. Settings use Pydantic v2 validation (`extra="forbid"`, frozen definitions, bounded enums/identifiers); validation failures map to bounded reason codes and never serialize rejected values. Existing `SecretStr` database settings remain the pattern for in-process credentials.

| Settings group | Required fields / binding | Authority and constraints |
| --- | --- | --- |
| Deployment scope | profile ID/version, deployment ID, commit and image digests, release ID, Workspace/Environment policy IDs; distinct SOURCE and RESTORE profile IDs | Release/runtime owner supplies immutable versioned non-secret values. The server resolves scope from trusted context; request selectors never establish authority. |
| PostgreSQL | endpoint/database/schema/migration revision, role identity per process, pool/timeout bounds, TLS/secret references | Canonical storage is PostgreSQL. Runtime, dispatcher, migration, backup, restore and verifier identities are separate. Migration is explicit; no schema creation/granting on normal startup. Secret manager supplies credentials. |
| Temporal | endpoint/cluster profile, namespace, task queue, worker contract version, TLS/client-auth secret references, bounded RPC/startup/shutdown timeouts | Namespace/queue are profile-scoped and separate for SOURCE/RESTORE. API dispatcher and worker must use compatible values. No namespace provisioning on ordinary startup. |
| Original storage | adapter/profile/version, source root or opaque volume identity, finalized-object policy, immutable backup-destination reference | Canonical original bytes remain outside Cognee. SOURCE and RESTORE roots are distinct. Paths and object references do not appear in reports. |
| Authentication/authorization | `oidc` or explicitly permitted development-only `local` mode, issuer/audience/client profile, canonical authorization-store profile, identity/scope resolver version, policy versions | Follow ADR-0021 and existing `AccessPolicy`. Production composition rejects a development local bearer adapter. Authority comes from PostgreSQL identities/memberships/grants and current policies. |
| Observer capture | Capture contract version, opaque channel/service identity, workload-auth secret references when process-separated, bounded message size/buffer/TTL and delivery/ack limits | Follow ADR-0040. Protected typed capture remains ephemeral and same-execution; no payload enters Temporal history, diagnostics or benchmark durable storage. Do not add a process boundary solely to satisfy this settings group. |
| Projection | provider profile/version, Cognee and adapter version, Neo4j/APOC and PGVector versions, dataset/namespace mapping version, connection secret references, physical limits | PostgreSQL remains authoritative. Provider/profile is pinned; no silent fallback or model substitution. Startup/readiness makes no paid LLM/embedding call. |
| Models and spend | model/provider names and immutable model versions, embedding dimensions, max request/token/concurrency limits, per-run budget/approval reference | Runtime/Provider owner approves the profile and limit; operator separately confirms budget for any paid run. No cost-bearing call before both approvals. |
| Recovery | backup contract/profile version, scope inventory reference, fence/lease bounds, destination/encryption/retention references, compatible PostgreSQL tool version, restore target inventory ID, separate operation identities | Storage/Recovery owner approves SOURCE, destination and empty RESTORE target. Targets are checked against deployment inventory and forbidden when equal or connected to SOURCE. |
| Diagnostics/evidence | report schema, allowed reason-code set, evidence-store reference, audit profile, secret/payload redaction policy | Reports are metadata-only and bound to commit plus deployment fingerprint. No secrets, DSNs, documents, prompts, answers, filenames, raw provider payloads, or sensitive source identifiers. |

Secret values are provided only by the platform-approved external secret mechanism, by reference and scoped to the process role. The concrete secret provider is not named in this contract because no repository ADR selects one for this deployment. The Runtime/Operations owner must either identify the already-approved platform mechanism or obtain approval for the selected provider, process access policy, rotation/expiry behavior and safe-unavailability semantics before ticket implementation (D1/D5). Environment variables may carry references where policy allows; secret literals must not be embedded in Compose, command history, logs, images, readiness output, or Git.

### 4.2 Validation and error behavior

The composition validates all fields, profile IDs, cross-process compatibility, role assignments, source/restore separation and version pins before creating clients. Missing secrets, unsupported schema, drifted contract version, wrong database role, namespace/queue mismatch, unavailable required trust store, or a SOURCE/RESTORE collision fails closed. Safe errors expose only finite codes such as `CONFIG_INVALID`, `SECRET_UNAVAILABLE`, `PROFILE_MISMATCH`, `DATABASE_ROLE_MISMATCH`, `TEMPORAL_BINDING_MISMATCH`, `AUTHORITY_UNAVAILABLE`, `PROVIDER_PROFILE_UNAPPROVED`, `RESTORE_TARGET_NOT_ISOLATED`, `BUDGET_NOT_APPROVED`; they do not include raw exception text or values.

## 5. Trusted authorization contract

1. Human API requests use a signed API-audience access token under ADR-0021 OIDC, then resolve `(issuer, subject)` as an Authentication Alias to a canonical human identity in PostgreSQL. A local adapter is allowed only for an explicitly development deployment; it cannot establish production readiness.
2. Server-side authorization resolves current Workspace and Environment memberships, route purpose/operation, role and policy. Selector headers/fields are validated against that context but never create it. RLS context is set by the trusted boundary on each canonical transaction.
3. Human and service identities are distinct. API service, Q&A runtime/Temporal activity, dispatcher, migration, projection, backup coordinator, restore, and independent verifier receive only their approved least-privilege roles. Identities and grants are not implicitly shared between SOURCE and RESTORE.
4. On-behalf-of work carries the admitted canonical requester, workspace, environment and policy generation through the command/outbox/workflow. Workers revalidate current authority at the boundary required by the existing capability; they do not accept a caller-provided principal or broaden access because they run as a service.
5. Canonical identity, membership, grant and policy provisioning is security-owned and explicit. `PostgreSQLAuthorizationSeedHelper` is not a runtime or operator provisioning endpoint. No startup seed, blanket admin grant, RLS bypass, superuser or AccessPolicy modification is permitted.
6. Positive and negative verification uses real supported bindings: allowed operation in the authorized workspace; denied missing-role operation; denied second workspace/environment; current revocation/changed-scope rejection; role separation for dispatcher/verifier; and no protected content in denied responses, logs, or reports.

The canonical mechanism for creating temporary synthetic qualification identities and grants is unresolved and reserved to the Security Owner (D4). Until approved and physically verified, trusted authorization readiness is `BLOCKED`, not inferred from tests or development bearer configuration.

## 6. Provider and projection contract

The only authority for accepted source revisions, tombstones, publication, authorization and recovery state is PostgreSQL. Cognee, Neo4j, APOC and PGVector are rebuildable physical Context Projection components. The existing `ContextProjection`, canonical resolver, projection request/outbox route and recovery result bridge remain the only path for physical projection work.

Issue #507 records one real synthetic physical profile: Cognee 1.5.4, PostgreSQL 17, Neo4j 5.26.32 Community + APOC 5.26.32, PGVector 0.8.7, model `openai/gpt-5.4-nano`, and embedding `openai/text-embedding-3-small`. Its physical-profile GO applies to that observed run only; it is not deployment readiness, #507's full acceptance, or automatic authorization to make more calls. Provider owner must confirm that exact profile remains approved for this SOURCE/RESTORE run, bind credentials through the approved secret mechanism, set bounded request/concurrency/budget limits, and obtain run-specific spend approval before paid calls.

Readiness may validate pinned configuration and perform explicitly approved non-billable connectivity checks. It must not index, embed, retrieve private content, start reindexing, or call an LLM during process startup. Provider outage maps to a bounded degraded/unavailable state; no alternate provider/model is selected automatically. Physical projection recovery runs through the canonical recovery outbox route and ADR-0027 stale/tombstone/shadow safeguards. The restored projection must be rebuilt from verified canonical state; restored Cognee state is not promoted merely because its container is healthy.

## 7. Backup and restore operator boundary

This composition only binds existing recovery contracts:

- `CoordinatedBackupProfile` and the ADR-0022 fence/quiescence lifecycle;
- canonical PostgreSQL/custom-format and finalized original-storage artifact sources;
- protected destination, independently read-back verified component evidence and immutable coherent manifest;
- `IsolatedRestoreVerifier`, copied recovery evidence, dedicated empty isolated PostgreSQL/filesystem targets, and canonical original-reference reconciliation;
- projection recovery through the existing outbox worker and canonical result/publication path;
- the authorized canary and safe audit/diagnostic correlation required by the approved #16/#18/#509 runbooks.

The operator binding accepts an approved deployment/profile identity, full trusted Workspace/Environment inventory reference, approved operation/change reference, one-time recovery set ID, scoped coordinator identity, protected destination and encryption/retention profile references, and compatible tool versions. Restore accepts a coherent trusted manifest reference, distinct empty RESTORE target inventory ID, distinct restore/verifier credentials, and serving-identity inventory for explicit denial checks. The canary accepts a separate authorized principal, approved synthetic scope and action, current policy reference, and diagnostic/audit correlation reference. No parameter accepts an arbitrary database, path, identity, workspace selector, source volume, or “skip verification” switch.

The operator entrypoint is one-shot, explicit, auditable, and unavailable to normal API startup. It reads/records immutable evidence through existing ports; it cannot manufacture receipts or write manifests by hand. Any incomplete fence, artifact digest/read-back, reconciliation, authorization, target isolation, projection rebuild or audit result keeps RESTORE fenced and untrusted. The storage/recovery owner approves exact endpoints/volume aliases, tool versions, retention/encryption, role references and independent review. The security owner approves the identities and canary. No backup or restore is allowed until the decision register prerequisites are resolved.

The repository runbooks state there is currently no reviewed backup or restore CLI/HTTP entrypoint. Do not run direct `pg_dump`, `tar`, a hand-written manifest, or restore onto SOURCE as a substitute.

## 8. Mandatory security invariants

- Fail closed for missing, mismatched, stale, unavailable, unauthorized, ambiguous or indeterminate bindings.
- Never derive authority from request selectors, dataset/node-set names, arbitrary workspace/environment values, or worker presence.
- PostgreSQL remains canonical; physical projections and backups do not become authority by availability alone.
- Enforce Workspace and Environment isolation at API, persistence, provider routing, dispatch, recovery and evidence boundaries.
- No test fake, test server, synthetic receipt, manual artifact or healthcheck may be described as production qualification.
- Do not log or publish secrets, DSNs, protected payloads, provider bodies, prompts/answers, documents, filenames, raw stack traces, or sensitive source identifiers.
- Do not create identities, grants, policies, namespaces, databases or provider resources automatically; do not disable RLS, AccessPolicy, authentication, audit or role separation.
- SOURCE and RESTORE use independent data volumes, networks, trust contexts, database roles, Temporal namespaces, and provider routing; no restore writes to SOURCE.
- RESTORE remains isolated, fenced and untrusted until all manifest, digest, trust-chain, reconciliation, tenant-isolation, projection and canary requirements pass.
- Never serve stale, tombstoned, shadow, foreign-workspace or unverified projection evidence.
- No unapproved provider spend, automatic provider/model fallback, private Golden Dataset benchmark, or production use is authorized by this contract.
- Qualification/release promotion requires attributable human approval bound to exact commit, profile and evidence digest; startup cannot promote a deployment.

## 9. Readiness and evidence model

Each level is recorded separately in a versioned metadata-only report, with state `PASS`, `FAIL`, `BLOCKED` or `NOT_VERIFIED`, safe reason codes, evidence reference, target commit and deployment fingerprint. A later level never inherits PASS from an earlier one.

| Level | PASS evidence | Examples of BLOCKED / NOT_VERIFIED reason codes |
| --- | --- | --- |
| 1. Configuration validated | Strict schema accepted; secret references resolve for the process role without disclosure; profile/role/version and SOURCE/RESTORE cross-checks pass | `CONFIG_INVALID`, `SECRET_UNAVAILABLE`, `PROFILE_MISMATCH`, `ROLE_MISMATCH` |
| 2. Infrastructure reachable | Live probes to the configured canonical PostgreSQL, Temporal frontend/cluster RPC, original storage and explicitly required provider control plane; migrations/schema compatible | `DATABASE_UNAVAILABLE`, `TEMPORAL_NOT_SERVING`, `STORAGE_UNAVAILABLE`, `PROVIDER_UNAVAILABLE`, `NOT_VERIFIED` |
| 3. Trusted authorization verified | Actual approved identities and positive/negative scope/policy/RLS scenarios on the target deployment, with safe audit correlation | `IDENTITY_UNBOUND`, `GRANT_UNVERIFIED`, `AUTHORIZATION_DENIED`, `CROSS_SCOPE_TEST_MISSING` |
| 4. Worker registered | Actual application worker is running on the declared namespace/queue, registered with required workflow/activity definitions, and poller/task-delivery evidence is correlated | `WORKER_NOT_RUNNING`, `WORKER_NOT_POLLING`, `TASK_QUEUE_MISMATCH`, `NOT_VERIFIED` |
| 5. Application execution verified | Supported authenticated synthetic API request completes admission → outbox → Temporal application worker → terminal PostgreSQL persistence, with allowed and denied cases and no fake execution adapters | `APPLICATION_PATH_NOT_RUN`, `OUTBOX_NOT_DELIVERED`, `WORKFLOW_NOT_COMPLETED`, `TERMINAL_STATE_MISSING`, `AUTHORIZATION_UNVERIFIED` |
| 6. Projection physically qualified | Approved synthetic provider run through canonical resolver and actual physical Cognee profile; lineage/currentness, isolation, failure/recovery and successor publication satisfy #507/related contracts | `PROVIDER_PROFILE_UNAPPROVED`, `PROJECTION_NOT_RUN`, `CANONICAL_LIFECYCLE_UNVERIFIED`, `STALE_EVIDENCE` |
| 7. Backup/restore physically qualified | One coherent fenced backup with component digest/read-back and immutable manifest; separate restore target passes verifier/reconciliation, projection recovery and authorized canary | `BACKUP_BINDING_UNAPPROVED`, `FENCE_INCOMPLETE`, `MANIFEST_UNTRUSTED`, `RESTORE_TARGET_NOT_ISOLATED`, `RECONCILIATION_FAILED`, `CANARY_NOT_AUTHORIZED` |
| 8. Release qualification approved | All mandatory #16/#509 criteria PASS against the same current commit/profile/evidence set and the accountable release/security/storage owners record approval | `REQUIRED_EVIDENCE_MISSING`, `OWNER_APPROVAL_MISSING`, `EVIDENCE_STALE`, `QUALIFICATION_BLOCKED` |

`docker compose config`, container health, Temporal `SERVING`, a registered namespace, SDK test server, unit/integration tests, and `/health` are evidence only for their own boundary. They never promote levels 3–8. Readiness data must say which process/profile was observed and distinguish application-worker readiness from Temporal server readiness.

## 10. Acceptance criteria for implementing this approved contract

1. One Runtime-owned root and strict versioned settings contract assemble existing bindings; no duplicate Q&A runtime, persistence authority, dispatcher or workflow engine is introduced.
2. API, worker and dispatcher run in separate processes yet prove compatible profile/release/contract/database/namespace/queue identities while retaining separate least-privilege credentials; the observer binds to that same logical composition and uses the approved existing capture topology.
3. Migrations and provisioning are explicit operator actions; startup only validates and connects to pre-provisioned resources.
4. The supported API path is admission-only and executes through the canonical PostgreSQL outbox, Temporal worker, existing application capability and terminal persistence.
5. The outbox process explicitly registers the existing Ask/cancellation and approved projection routes, uses bounded retry/lease settings and shuts down cooperatively.
6. Actual trusted authentication, current canonical scope, AccessPolicy and RLS are proven by positive and negative deployment checks; local test auth or seed helpers cannot pass production readiness.
7. Provider/model settings are pinned, versioned, budget bounded and run-authorized; readiness does not make paid provider calls.
8. Backup and restore entrypoints call only the existing coordinator/verifier/reconciliation contracts, use distinct approved roles/targets and cannot target SOURCE for restore.
9. Evidence reports each readiness level separately and is metadata-only, immutable/versioned, attributable to exact commit and deployment fingerprint.
10. Existing #16/#18/#507/#509 acceptance and runbooks remain authoritative. Implementation may proceed against this approved architectural boundary; identity provisioning, provider spending, recovery targets, canary and physical operations remain blocked until their designated owners approve the exact bindings.

## 11. Verification strategy

After architecture approval, implementation is verified at the highest available seam in this order: strict settings and mismatch tests; composition tests proving API/worker/dispatcher compatibility; PostgreSQL role/RLS integration; Temporal integration proving actual SDK poller delivery and a successful synthetic terminal execution; supported-auth positive/negative API scenario; disposable Docker SOURCE/RESTORE deployment; separate physical projection qualification; then, only after target-specific operator approvals, the approved fenced backup, isolated restore, reconciliation, projection rebuild, canary and safe diagnostics. Bind every report to the tested code/image/profile. Run full repository gates including pytest, compileall, domain import boundary, R1 regression and web gates when relevant.

No unit, fake, test-server, disposable integration, or infrastructure health result substitutes for an operator-controlled physical deployment/evidence stage.

## 12. Non-goals and approval

This version does not implement settings, a deployment factory, process runner, identity provisioning, secret manager, backup CLI, restore CLI, new provider adapter, new workflow/orchestration system, schema migration, AccessPolicy change, new ADR, or a physical operation. It does not authorize production access, new production privileges, private corpus use, provider spending, backup, restore, canary, promotion, or issue closure.

**Architecture approval:** recorded by `dima55279` based on team lead approval communicated outside GitHub on 2026-10-21 ([comment](https://github.com/alkselsv/spine/pull/528#issuecomment-6317606512)); D1–D8 are approved at the architecture boundary. This is not a formal GitHub review from the team lead.

**Remaining prerequisites:** Runtime/Operations names the accountable implementation owner and approved external secret mechanism/access/rotation policy; Security Owner confirms the canonical identity/grant provisioning mechanism and local synthetic trust source; Projection/Provider Owner approves the exact pinned profile and run budget; Storage/Recovery Owner approves operator target/protection bindings; operators separately approve each physical operation; Release owner confirms evidence adjudication. These approvals do not reopen D1–D8 and do not imply deployment readiness.
