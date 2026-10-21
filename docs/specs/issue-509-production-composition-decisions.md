# Issue #509 production composition decision register

**Contract:** [`spine-production-deployment-composition:v1`](issue-509-production-composition-contract.md)
**Status:** **ARCHITECTURE APPROVED — recorded by `dima55279` based on team lead approval communicated outside GitHub on 2026-10-21; [approval record](https://github.com/alkselsv/spine/pull/528#issuecomment-6317606512).**
**GitHub review:** This participant-authored comment records the communicated decision; it is not a formal GitHub review from the team lead.
**Use:** D1–D8 architecture recommendations are approved. The owner-specific and operational prerequisites below remain outstanding; they are not implied by this architecture approval.

## Decisions already settled by accepted contracts

- One production `AnswerQuestionCapability` composition is shared by API and observer; durable Q&A uses PostgreSQL admission/outbox and Temporal execution (ADR-0038, ADR-0040, #469/#476/#478).
- PostgreSQL is canonical; Temporal is the sole durable workflow engine (ADR-0010, ADR-0038).
- Canonical authorization is resolved from a trusted identity and PostgreSQL membership/grants/policy; selectors and token claims do not grant authority (ADR-0021).
- Coordinated backup fencing, quiescence, immutable manifest and recovery trust semantics are fixed (ADR-0022, #18/#362).
- Projection state is derived, canonical lineage and tombstones govern physical writes, and recovery/publication stay on the existing outbox/CAS path (ADR-0027, ADR-0030/#287/#364).
- Existing `CoordinatedBackupProfile`, `IsolatedRestoreVerifier`, projection recovery path and runbooks are the implementation targets. No second backup or recovery system is an option.

## Recorded D1–D8 architecture approval

| Decision | Approved architecture boundary | Separate approval/prerequisite still required |
| --- | --- | --- |
| D1 | Runtime owns one settings-backed production composition root and compatible version/profile identities; component owners control their bindings. | Runtime owner name; external secret mechanism, process access and rotation/expiry policy. |
| D2 | API, Temporal worker and dispatcher use process-specific lifecycles over compatible settings; observer/capture uses the existing approved topology. | Actual deployment configuration and per-process secret references. |
| D3 | One bounded outbox runner explicitly registers existing consumers and owns its lifecycle. | Runtime/Operations approval of concrete dispatcher role and lease/retry/polling profile. |
| D4 | Qualification identities are provided only through a canonical Security-owned mechanism; existing identity, membership, policy and RLS rules remain authoritative. | Security Owner confirms the concrete provisioning interface, synthetic identities, memberships/grants, lifecycle and test scope. |
| D5 | Provider/model configuration is pinned and versioned, spend bounded, and never invoked at startup/readiness. | Provider owner approves exact profile/models; operator separately authorizes connectivity/run and available spend budget; Runtime confirms secret mechanism. |
| D6 | Recovery uses only the existing coordinated backup, immutable manifest, isolated verifier and reconciliation contracts through an explicitly authorized operator entrypoint. | Storage/Recovery owner approves destination, encryption/retention, SOURCE/RESTORE inventory, identities, isolation and each operation. |
| D7 | A canary must exercise an authorized synthetic operation with safe measurable output, audit correlation and independent review. | Security/Storage/Runtime owners approve action, principal/scope, target and reviewer. |
| D8 | Readiness/evidence levels remain distinct; physical PASS and release GO require attributable evidence and designated owner review; #509 remains the qualification issue. | Release owner adjudicates evidence; team lead reconciles residual #506/#507 ownership without treating closed status as PASS. |

The approval is architectural only. No specific identities/grants, provider spend, storage target, recovery permission, canary, formal GitHub approval, or deployment readiness has been granted by this record.

## D1 — Runtime composition owner and version governance

**Problem.** ADR-0040 approves one shared Q&A capability composition for API and observer, and #506 specifies a qualification factory. Neither settles operational ownership of one settings-backed deployment root spanning separate API, worker, dispatcher, projection and recovery processes, nor the approved external secret provider, access policy, rotation and expiry semantics. #527 provides an SDK worker factory but not ownership or deployment configuration.

**Settled contract.** ADR-0040 D2 requires one production Q&A composition shared by API and observer; #506 calls for a narrow production qualification factory. The recorded approval extends this into one Runtime-owned deployment root. Secret-provider selection and the named implementation owner remain Runtime owner follow-ups, not open architecture alternatives.

**Options.** (A) Runtime owner owns one root, settings schema and compatible release/profile identities while security, provider and storage owners approve their bindings. (B) Each process team independently owns construction/configuration. (B) would drift from ADR-0040 and create multiple runtime graphs.

**Approved decision.** A. The Runtime owner and release/version owner are accountable; identify an already-approved platform secret provider or obtain approval to select one, including process access, rotation/expiry and unavailable-secret handling; require schema migration and compatibility policy for changes. Use existing factories as seams and keep typed settings immutable/strict.

**Consequences.** One implementation ticket can consume the accepted contracts; changes to composition remain reviewable at one owner boundary. No new runtime framework or service architecture is required.

**Owner:** Team lead / Runtime architecture owner.
**Dependencies:** #469, #476, #478, #506 and merged #527.
**Acceptance:** Named owner; approved root scope and one versioned settings/profile compatibility rule; named platform secret mechanism and process access/rotation policy; component owners and approval inputs documented; tests reject incompatible bindings.
**Remaining owner confirmation:** Name the Runtime owner and approved external secret mechanism/access/rotation policy. These values do not reopen the approved D1 architecture.

## D2 — Separate API, Temporal worker and dispatcher lifecycle

**Problem.** API app injection, worker registration, observer/capture contracts and `OutboxWorker.dispatch_batch()` exist, but no deployable process bootstrap owns connect/start/readiness/drain/close behavior for the deployed process roles. Different processes cannot share a Python object.

**Settled contract.** API and observer share one logical production capability composition; PostgreSQL admission/outbox and Temporal own durable execution (ADR-0038/0040). API, worker and dispatcher are separate process roles where deployed separately. ADR-0040's authenticated ephemeral channel supports a separated worker/observer topology but does not mandate an additional observer process.

**Options.** (A) Each existing process role uses one production-owned root with a strict shared settings/profile contract and its own role-scoped resources; observer/capture binds to that same composition using the existing channel only when required by its selected topology. (B) serialize or share Python composition instances; impossible across process boundaries. (C) let each process construct independently from unversioned environment variables; incompatible and drift-prone.

**Approved decision.** A. API, Temporal worker and dispatcher are distinct process roles that build compatible local graphs and prove matching non-secret runtime identities/contract versions. Observer/capture uses the same logical graph and existing #478 topology; do not create a new process boundary solely for it. Start/stop hooks are process-owned; schema migration/provisioning remains explicit outside startup.

**Consequences.** Integration tests must cross process/service boundaries or prove the real transport path; object identity assertions remain appropriate only within one process. Health endpoints distinguish liveness, dependency reachability, worker registration and completed application execution.

**Owner:** Runtime owner; API and workflow maintainers implement their process adapters.
**Dependencies:** D1; PostgreSQL role configuration; approved Temporal namespace/task queue.
**Acceptance:** Real API app, SDK worker lifecycle and dispatcher process start with matching compatible profile; the existing observer binds to that graph and authenticated capture topology; shutdown is bounded/cooperative; mismatches fail closed; deployed poller and successful synthetic application terminal state are observable.
**Remaining owner confirmation:** Runtime owner supplies compatible process-specific settings and role-scoped secret references. The process model itself is approved.

## D3 — Outbox dispatcher runner and consumer ownership

**Problem.** The dispatcher handles bounded batches and delivery leases, but there is no owned long-running process loop, combined consumer registration contract, or approved poll/retry/lease lifecycle configuration.

**Settled contract.** The transactional outbox is delivery-only. It does not own workflow state; Temporal owns durable Ask retry/cancellation/terminal arbitration (ADR-0038). Projection recovery stays on the canonical outbox route (#287/#364/runbook O05).

**Options.** (A) One dispatcher process explicitly composes the existing Ask/cancellation and approved projection bindings using dispatcher-role credentials. (B) separate loops directly query each outbox consumer. This risks duplicate claims and retries outside the canonical dispatcher. (C) add a new broker/queue; outside scope.

**Approved decision.** A. Runtime owner configures bounded batch size, cadence, lease duration/renewal, handler timeout, retry ceiling/backoff and graceful stop. Consumer list is explicit and versioned; every handler remains idempotent and tenant-safe.

**Consequences.** Add long-running lifecycle and integration tests for crash/lease retry and shutdown; do not widen dispatcher database grants to application data.

**Owner:** Runtime/Operations owner.
**Dependencies:** D1/D2; dispatcher database identity; approved consumer bindings and audit/diagnostic sink.
**Acceptance:** One runner delivers Ask, cancellation and authorized projection work through the existing repository and routes; no duplicate logical effect across redelivery; bounded stop leaves canonical lease/retry semantics intact; no secret or payload logging.
**Remaining owner confirmation:** Runtime/Operations selects the least-privilege dispatcher role and concrete bounded retry/lease/poll profile.

## D4 — Trusted synthetic authorization provisioning

**Problem.** OIDC and local authorization compositions use the canonical PostgreSQL authorization directory, but no supported operator path is identified for creating temporary synthetic local identities/memberships/grants. Local bearer authentication is development-only; the test/migration seed helper is not production provisioning.

**Settled contract.** ADR-0021 controls alias-to-canonical identity and current memberships/grants. No identity or authority comes from request selectors; RLS and AccessPolicy remain mandatory. #506 explicitly says the issue does not create authority and needs a security owner to verify an existing principal.

**Options.** (A) use an already-approved security-owned identity/admin provisioning path with a bounded synthetic principal and least privilege. (B) add a narrowly scoped, separately reviewed operator provisioning command/API. (C) use the test seed helper, selectors, blanket admin, disabled RLS or local dev token. C is prohibited; B requires a separate security decision if A does not exist.

**Approved decision.** Use A if an existing Security-owned mechanism exists. If unavailable, Security Owner must approve a small provisioning contract and authoritative interface before implementation. Do not build a general provisioning API or turn test helpers into runtime APIs by inference.

**Consequences.** Until selected and verified on both SOURCE and RESTORE, runtime authorization is `BLOCKED`; no API/application readiness or physical canary can pass.

**Owner:** Security Owner.
**Dependencies:** ADR-0021; canonical identity/membership/grant storage; isolated SOURCE and RESTORE scopes.
**Acceptance:** Security-approved synthetic identity lifecycle; distinct SOURCE/RESTORE contexts; positive/negative workspace/environment, role, policy and RLS scenarios; no blanket privileges or secret-bearing evidence.
**Remaining owner confirmation:** Security Owner identifies the canonical interface (or separately approves its design), concrete temporary identities/grants, scope, expiry/revocation and verification procedure.

## D5 — Provider profile, secrets and spend limits

**Problem.** #507 records a real synthetic provider profile and a physical-profile GO for that exact run, but its overall issue remains blocked for deployed R4 and composed live R6. #509 has no run-specific provider credential binding, approved external-secret mechanism, or cost authorization.

**Settled contract.** PostgreSQL remains canonical; Cognee is a rebuildable projection; no provider fallback is allowed. Provider calls need a pinned model/profile and must not occur during readiness/startup (#507, ADR-0027 and projection contracts). Private Golden Dataset #510 is outside #509.

**Existing candidate evidence.** #507's coherent physical run used Cognee 1.5.4, PostgreSQL 17, Neo4j/APOC 5.26.32, PGVector 0.8.7, `openai/gpt-5.4-nano`, and `openai/text-embedding-3-small`. That evidence qualifies the named run, not another deployment or future expenditure.

**Options.** (A) Reuse that exact pinned profile after Provider owner confirms compatibility and operator grants explicit bounded run budget/volume. (B) change provider/model; requires its own qualification and approval, not an automatic fallback.

**Approved decision.** A may be reused only for the synthetic #509 scope if the Provider owner reconfirms it is current. Runtime owner identifies the approved external secret mechanism (or obtains approval to select one), including process-scoped access and rotation/expiry. Provider owner defines concurrency/request caps, and the operator confirms the available budget before paid calls. If the provider cannot be used, keep that blocker isolated and continue non-billable infrastructure work.

**Consequences.** Compose startup and readiness do not make LLM/embedding calls. No provider failure may silently switch model or suppress stale/tombstoned isolation.

**Owner:** Projection/Provider owner; budget approval by the designated operator/product owner.
**Dependencies:** D1/D4; #507 exact profile evidence; current SOURCE deployment and spend policy.
**Acceptance:** Exact version pins and secret refs approved; external-secret mechanism and process access/rotation policy named; bounded call/concurrency/budget policy recorded; connectivity tested without billable requests where possible; approved synthetic test proves lineage/currentness and failure handling.
**Remaining owner confirmation:** Provider owner approves the exact profile/models and limits; operator approves any paid run and budget; Runtime confirms secret mechanism and process access/rotation.

## D6 — Recovery operator configuration and launcher

**Problem.** Coordinator, component sources, immutable evidence, verifier and runbooks exist, but no reviewed operator entrypoint or deployment-approved destination/restore target/identity configuration exists.

**Settled contract.** ADR-0022 fence, quiescence, manifest trust and invalidation semantics are fixed. #362/#363 and `docs/storage-backup-profile.md`, `docs/runbooks/r1-backup-evidence.md`, `r1-isolated-restore.md`, and `r1-projection-recovery.md` define operations. Direct dumps, manual manifests, serving-role restore, SOURCE overwrite and untrusted promotion are prohibited.

**Options.** (A) one-shot operator command invoking existing contracts; (B) an owner-managed scheduled/job runner invoking the same contracts. No HTTP endpoint or new backup framework. Either is acceptable if it has equivalent approvals, identity separation and evidence; current runbooks say neither reviewed entrypoint exists.

**Approved decision.** Recovery owner chooses the least operationally complex one-shot entrypoint using only existing contracts, and approves exact synthetic SOURCE scope, destination/encryption/retention, PostgreSQL tooling, lease/fence role, empty non-serving RESTORE PostgreSQL/filesystem targets, restore role and independent verifier role. Security and deployment owners independently confirm identities/network isolation before execution.

**Consequences.** No physical backup/restore begins until every target and operator binding is checked at runtime. Failed attempts preserve incomplete immutable evidence and cannot promote RESTORE.

**Owner:** Storage/Recovery owner; Security and Runtime owners approve roles and deployment targets.
**Dependencies:** D1/D4/D5 as applicable; existing #362/#363/#368 runbooks and ADR-0022.
**Acceptance:** Actual coordinated SOURCE backup and immutable read-back; distinct empty RESTORE verification and reconciliation; serving access denied; projection rebuild uses existing outbox/recovery contract; evidence is content-free and independently reviewed.
**Remaining owner confirmation:** Storage/Recovery approves the entrypoint form and target inventory/protection; Security/Runtime approve role bindings and isolation; operators authorize each physical action.

## D7 — Authorized recovery canary

**Problem.** #16/#509 runbooks require an authenticated canary before a restored deployment can be trusted/promoted, but the exact allowed synthetic action, principal and evidence reviewer are not deployment-bound.

**Settled contract.** The canary cannot grant authority or bypass projection, lineage, disclosure, policy, RLS or audit checks. #16 R2/R13 require authenticated canary evidence; #509 requires safe diagnostics and correlation.

**Options.** (A) a bounded read-only Q&A on the approved synthetic corpus through the normal authorized API and completed Temporal application path, with expected synthetic citation/revision lineage. (B) a narrower documented API authorization/readback operation if Q&A would trigger paid provider spend or exceed the approved recovery objective. (C) an unauthenticated health endpoint or synthetic fake; prohibited as a qualification canary.

**Approved decision.** Select A only if D4/D5 approvals permit a no-private-content bounded request and provider budget is explicit; otherwise select B with a precise outcome that still exercises restored canonical authorization and required recovery state. A canary result must be correlated to restored commit/profile, audit and diagnostics without disclosing content.

**Consequences.** Until nominated and approved, canary and restored-serving readiness remain `BLOCKED`; no canary is run against SOURCE or a private corpus.

**Owner:** Security owner and Storage/Recovery operator; Runtime/Projection owners verify the path.
**Dependencies:** D4–D6; restored target is isolated and all trust/reconciliation/projection checks pass.
**Acceptance:** Approved principal/scope/action produces expected bounded result; denied principal/scope is rejected; audit correlation is present; no protected payload in evidence; independent reviewer accepts result.
**Remaining owner confirmation:** Security/Storage/Runtime approve the concrete canary action/principal/scope and independent evidence reviewer before execution.

## D8 — Evidence gate and release/issue ownership

**Problem.** Reports exist for Temporal SERVER readiness, worker-factory integration and blocked recovery, but evidence levels differ and closed #506/#507 tracker states do not match all their documented acceptance outcomes. Someone must decide who adjudicates current evidence and whether old closure requires follow-up.

**Settled contract.** Issue #16 already defines qualification semantics: PASS needs named evidence at the named boundary; BLOCKED means a trusted prerequisite/decision is unavailable; NOT_VERIFIED means missing/stale/incomplete/unbound evidence; mandatory blocked criteria prevent GO. #509 must remain open until its required physical checks are actually evidenced. #526/#527 reports explicitly disclaim application/recovery qualification.

**Options.** (A) Use the existing #16/#509 states and reports, naming release/runtime/security/storage reviewers; carry residual #506/#507 work in existing follow-up ownership or separately approved tickets. (B) treat closed issue state, healthy containers, integration tests or worker registration as completion. B contradicts existing evidence rules and is rejected.

**Approved decision.** A. Keep separate report fields for configuration, reachability, authorization, worker registration, application execution, projection, backup, restore/reconciliation, canary, diagnostics, and release decision. Release owner accepts only same-commit/profile-linked evidence after designated domain owners review it. Team lead/owners must reconcile follow-up ownership for closed #506/#507 residuals; this does not reopen either issue automatically.

**Consequences.** Existing reports remain historical and unchanged. New versioned metadata-only reports are emitted only for observed state. #509 stays OPEN / IN-PROGRESS and assigned to current executors.

**Owner:** Release owner with Runtime, Security, Projection and Storage/Recovery sign-off; team lead resolves closed-issue follow-up ownership.
**Dependencies:** #16 specification and mandatory gates; D1–D7; current issue/dependency graph.
**Acceptance:** Every required level has exact current evidence or bounded blocker; no test/health result is promoted; owner approval is attributable and tied to evidence digest; Issue #509 remains open until required ACs pass and executors coordinate completion.
**Remaining owner confirmation:** Release owner confirms evidence adjudication; team lead assigns residual #506/#507 ownership without treating closure state as PASS.

## Outstanding owner-specific approvals

- **Security Owner:** canonical identity provisioning interface, temporary identities, membership/grants, least privilege, source/restore separation, expiry/revocation and audit.
- **Runtime/Operations Owner:** accountable composition owner; external secret mechanism and process access/rotation; dispatcher role and bounded runtime profile.
- **Projection/Provider Owner and operator:** exact model/provider profile, request/concurrency limits, budget, connectivity and run-specific paid-call authorization.
- **Storage/Recovery Owner, Security and Runtime:** operator entrypoint, protected destination, retention/encryption, exact SOURCE/RESTORE targets, independent role bindings and isolation.
- **Security/Storage/Runtime and independent reviewer:** authorized canary action, principal/scope, evidence correlation and reviewer.
- **Release owner and current #506/#507 owners:** evidence adjudication and explicit residual-work ownership.

Architecture approval does not create credentials or authorize physical operations. Obtain current target-specific approval before provider spend, backup, restore, projection recovery, or canary.
