# Issue #531 — Security-owned qualification identity provisioning

**Status: APPROVED ARCHITECTURAL CONTRACT — PROJECT PARTICIPANT DECISION**

The project participant recorded agreement to this Security-owned provisioning contract and S1–S8 in [PR #540](https://github.com/alkselsv/spine/pull/540#issuecomment-6326615206). This records that participant decision; it is not a formal GitHub Review or a claim that the Security Owner personally reviewed or approved the PR. It authorizes contract documentation and implementation work through repository workflow, but does not authorize creating identities, memberships, grants, credentials, database roles, or operating concrete targets. Those actions require separate target-specific operational approval.

## Problem Statement

The SOURCE/RESTORE qualification needs temporary synthetic principals with precisely scoped Workspace and Environment authority. Spine already resolves canonical identities, memberships, environment-local roles, AccessPolicy, RLS, and authorization generations. It does not identify an approved Security-owned operator interface that provisions and revokes temporary bindings for isolated SOURCE and RESTORE targets.

Without that interface, the qualification cannot prove deployed authorization using trusted identities. Development authentication, request selectors, test seed helpers, or broad administrator privileges cannot fill the gap safely.

## Solution

Provide a one-shot operator-invoked, qualification-only provisioning interface. It accepts a versioned approved target/profile reference and independently verifies the operator and operation/change approval before creating or selecting only the canonical records and service bindings required for that qualification. SOURCE and RESTORE receive distinct principals, credentials, database-role references, and operator scopes.

The interface reuses Spine's canonical identity, membership, role, policy, audit, authorization-generation, and RLS contracts. It does not create a general user administration API. read_content and process_content remain separate grants issued only through the existing approved AccessPolicy workflow. The provisioning interface may verify and bind a pre-approved policy reference; it does not write or activate AccessPolicy grants itself.

S1–S8 are agreed architectural contract decisions recorded below. Canonical identity and membership records currently have no built-in expiry field. The agreed hard-expiry requirement is option A: add canonical schema representation and enforce the deadline using database time during every applicable human and service authority resolution. Expiry is a real authority constraint even when cleanup is unavailable; reaping and secret expiry are supplementary. This changes canonical authorization behavior and requires a separate ADR before implementation; ADR-0021 is not silently amended here.

## User Stories

1. As an authorized qualification operator, I want to invoke a bounded provisioning operation with an approved run reference, so that only the intended qualification deployment receives temporary authority.
2. As the Security Owner, I want operator authority checked independently from request selectors and local bearer authentication, so that the caller cannot authorize their own provisioning request.
3. As a qualification operator, I want to select a versioned approved target profile, so that Workspace, Environment, process roles, and lifecycle rules are validated as one immutable input.
4. As the Security Owner, I want SOURCE and RESTORE provisioning to produce separate canonical principals and aliases, so that credentials from one environment cannot authorize work in the other.
5. As an API operator, I want the API runtime to use only the server-configured service principal and the current human request context, so that API runtime authority cannot be selected by a request.
6. As a workflow operator, I want the Temporal worker's service authority limited to approved purpose, operation, and Environment, so that a worker cannot inherit broad human or administrator authority.
7. As an outbox operator, I want the dispatcher to receive only the database permissions needed to claim and deliver approved outbox work, so that it cannot read protected content or provision identities.
8. As a recovery operator, I want backup, restore, and verifier database roles bound independently from application principals, so that operational data movement does not imply content disclosure authority.
9. As a canary operator, I want an explicit synthetic human principal and source-specific read_content grant, so that the canary proves authorized disclosure rather than administrator privilege.
10. As a worker operator, I want process_content granted independently from read_content, so that processing authority cannot be mistaken for human disclosure authority.
11. As the Security Owner, I want missing AccessPolicy grants and RLS denials tested together, so that one enforcement layer cannot hide a bypass in the other.
12. As an operator, I want each provisioning request to be idempotent by approved operation identity and input digest, so that retry does not create duplicate principals or broaden bindings.
13. As an operator, I want partial failures to leave bindings inactive until all required records and secret references are valid, so that an interrupted operation cannot expose half-provisioned authority.
14. As the Security Owner, I want explicit revoke and cleanup operations that retain history and advance authorization generations, so that deprovisioning denies subsequent work without erasing evidence.
15. As the Security Owner, I want the binding to expire at a bounded deadline even if a cleanup job or operator is unavailable, so that expiry is an enforcement rule rather than a best-effort reminder.
16. As an auditor, I want immutable evidence to identify the approved profile, operator reference, state transitions, and authorization generations without storing credentials, raw tokens, claims, or protected content.
17. As a deployment owner, I want secret values supplied by an approved external secret mechanism, so that credentials are never committed or copied into issue comments, reports, command arguments, or logs.
18. As a recovery verifier, I want SOURCE credentials and principals rejected on RESTORE and verifier credentials rejected on SOURCE, so that separation can be tested directly.
19. As a deployment reviewer, I want deterministic and PostgreSQL tests clearly separated from actual target evidence, so that repository tests cannot be reported as live SOURCE/RESTORE readiness.
20. As the Runtime Owner, I want this interface invoked only as an explicit operator action outside API/worker startup, so that deployment restarts cannot create or refresh privileges implicitly.
21. As the Security Owner, I want conflicting existing bindings to fail closed rather than be silently rebound, so that identity provenance remains stable.
22. As the qualification owner, I want cleanup to leave immutable audit/history records while removing active authority and secrets, so that retries and reviews remain explainable.

## Implementation Decisions

### Approved boundary and invocation

- **Approved operation:** a one-shot operator command or owner-managed job with explicit provision, revoke, and cleanup operations. Do not add an HTTP endpoint or run it from API, worker, dispatcher, migration, or readiness startup.
- **Approved ownership:** Security owns the provisioning contract and permitted principal/grant shapes. Operations owns invocation controls and target profile inventory. Runtime consumes pre-provisioned bindings. Storage/Recovery owns target inventory and backup/restore role constraints.
- **Operator authority:** the caller must authenticate through an owner-approved operator channel and present an independent approval/change reference. The command must validate that reference and caller eligibility against a source outside request-controlled values. The architecture requires this independent check; the concrete operator channel and approval reference are run-specific configuration prerequisites, not a reason to select a provider here.
- **Versioned input:** accept only a strict immutable profile containing a profile version/digest, qualification run reference, exact SOURCE or RESTORE target inventory reference, Workspace/Environment inventory references, approved process-role references, allowed operations, expiry policy, and secret references. An independent approval/change reference binds the requested profile to the operation. Reject arbitrary database URLs, paths, identity IDs, Workspace selectors, source volumes, inline secrets, or verification bypass flags.
- **No target discovery from identity inputs:** a target/profile identifier selects configuration only. It cannot create membership, grant, role, or canonical identity authority without the independently verified operator approval.

### Canonical records and privilege separation

- Reuse CanonicalHumanIdentity, AuthenticationAliasBinding, WorkspaceMembership, EnvironmentMembership, EnvironmentRoleBinding, and AuthorizationGeneration as the canonical human authorization records.
- Authentication Alias remains an exact provider-scoped issuer/subject pair; local bearer authentication may authenticate a configured alias in explicit development but does not authorize provisioning or establish a canonical grant.
- Reuse existing service-principal references and PrincipalKind.SERVICE only where the consuming application contract supports the required scoped authority and expiry checks. Where it does not, add a qualification-scoped binding record; do not add a general service-principal registry.
- Workspace membership, Environment membership, and environment-local roles must be scoped to the approved Workspace and Environment. The current R1 role vocabulary is exactly administrator; no new role is implied.
- Administrator remains control-plane authority, not content authority. Human read_content and service process_content grants remain separate, source-specific, purpose/operation scoped where applicable, and subject to existing AccessPolicy approval and activation. Provisioning may validate a current approved policy reference but cannot manufacture or activate grants.
- API runtime, Temporal worker, outbox dispatcher, migration operator, backup coordinator, restore operator, verifier, and canary use distinct authority references and least-privilege PostgreSQL/RLS scopes. These do not all need new entity types. Each role is included only if the selected qualification profile requires it.
- Database privileges and PostgreSQL RLS do not replace Spine AccessPolicy. Both layers must be enabled and checked. No superuser, BYPASSRLS, broad tenant grants, content superuser, or cross-Workspace authority is permitted.

### SOURCE/RESTORE separation

- Provision SOURCE and RESTORE as independently named target bindings, each with distinct canonical principal/alias bindings, credentials/secret references, database-role references, runtime profile identity, and operator scope.
- Restore is initially empty, isolated, fenced, and non-serving. Its verifier principal is separate from restore writer and serving/canary principals.
- SOURCE credentials and identity bindings are never copied into RESTORE. A restore migration/import may reproduce canonical source data only under a separately approved recovery procedure; this proposal does not authorize copying active credentials or grants.
- Reject use of SOURCE principal/credential against RESTORE, RESTORE verifier against SOURCE, and serving/canary principal before the restore target is trusted. Cross-target requests fail closed and emit safe audit evidence.

### Approved expiry contract (S4)

Canonical human identity, alias, Workspace membership, Environment membership, and role records currently use version/status history and generation but do not carry an expiry timestamp. AccessPolicy grants support valid_from and valid_until. The agreed contract requires schema and resolver changes so cleanup failure cannot extend identity or service authority beyond its deadline.

| Option | Enforcement | Advantages | Risks / required work | Assessment |
|---|---|---|---|---|
| **A. Canonical deadline checked on every authority resolution** | Store an immutable UTC deadline on the bounded qualification binding; canonical human and service authority resolution compares it to database time. At or after the deadline, resolution denies authority even if cleanup never runs. AccessPolicy continues to enforce its own grant interval. | Hard fail-closed deadline; no dependency on cleanup availability; explicit testable expiry boundary. | Requires a new schema representation and resolver changes; every human/service consumer must use the authoritative resolver. S-01 must inventory every canonical human and service authority path, including consumers that do not pass through `PostgreSQLAuthorizationDirectory`, before claiming complete coverage. Already admitted durable work must obtain fresh authority at each activity/action boundary before each subsequent protected operation and be unable to extend authority past expiry. | **Approved architectural requirement.** |
| **B. Reaper/scheduled revocation only** | A scheduled process appends revoked/disabled successor records and advances generation at expiry. | Reuses current status-history and generation mechanics; leaves explicit audit transition. | If the reaper is delayed or unavailable, authority remains active after deadline. Does not satisfy hard expiry by itself. Requires a new privileged scheduled operator and recovery semantics. | Rejected as sole enforcement; may supplement A. |
| **C. External credential expiry / target shutdown only** | Secret provider expires credentials or deployment is disabled at deadline; canonical records are later revoked. | Uses an external control and can reduce exposure beyond Spine. | A still-valid credential may be replayed or another auth route may resolve the same alias; database-side authority remains active. Depends on provider behavior and operator availability. | Rejected as sole enforcement; defense in depth only. |
| **D. Expiry of AccessPolicy grants only** | Set valid_until on content grants. | Already supported for content access; no identity schema change for this layer. | Does not expire route role, alias, membership, service reference, or database credentials. | Required for temporary content grants, but insufficient as full qualification binding expiry. |

**Approved combination:** A for human and service authority expiry, D for content grant expiry, with B and C as defense-in-depth only. The deadline is compared against database time within authoritative resolution; equality means expired. Revocation advances the matching authorization generation atomically with its audit record and binding state. S-01 must inventory all applicable resolution and consumption paths, including service-principal consumers that bypass `PostgreSQLAuthorizationDirectory`, and define how each obtains current authority. An already admitted durable workflow must obtain fresh authority at each activity/action boundary before each subsequent protected read or action, reject work after expiry/revocation, and cannot renew or extend the binding. Its orchestration may continue only for safe cancellation/cleanup that does not use expired protected authority.

This is a new canonical authorization contract. Record it in a separate ADR that explicitly extends ADR-0021 before changing schema or resolver behavior; retain ADR-0021 unchanged until that ADR is accepted.

### Lifecycle, idempotency, partial failure, rollback

The approved lifecycle is:

requested → validated → inactive bindings recorded → external secrets bound → active → revoked → cleaned

- A strict operation identity and canonical request digest make retries idempotent. Reusing an operation identity with different target/profile or authority inputs is a conflict, not an update.
- Every target binding starts inactive. It becomes active only after operator approval, target/profile checks, canonical record consistency, required external secret references, AccessPolicy references, database role qualification, and audit readiness succeed.
- If external secret creation fails, keep authorization inactive and retain a safe incomplete operation record. Retry using the same idempotency identity or run explicit cleanup; do not silently create a second principal.
- Activation changes the target binding to active only in the same PostgreSQL transaction that commits its immutable audit event and the required authorization generation changes. If the binding, audit, or generation write fails, the transaction rolls back and authority remains inactive. The bounded operation ledger supports idempotency and partial recovery; it is not a general identity CRUD repository.
- Revocation atomically records the denied/revoked binding, audit event, and affected authorization generation. Canonical resolution denies the principal before external credentials are revoked. If secret revocation fails, canonical expiry/revocation still denies authority and cleanup stays pending for safe retry.
- Cleanup removes or disables only externally managed credentials and target resources covered by the approved profile. Canonical identity/audit history is retained; no history is deleted to make retries succeed.
- Audit is part of the activation/revocation database transaction: audit write failure prevents activation, and revocation is not reported complete unless denial, generation, and audit commit together. If an external audit sink is added, an outbox committed in that same transaction provides retry; authority remains inactive or denied while delivery is pending.
- Effective alias, membership, role, or service-authority changes advance the matching authorization generation in the same database transaction. A stale generation must not authorize the next request or content decision.
- Since the secret provider is external, database and secret writes cannot be one distributed atomic transaction. Inactive-first activation and durable operation state are the required recovery boundary: an external failure never leaves an active partial binding, and retries use the same idempotency identity.

### Audit and secret handling

- Record operation ID, profile version/digest, approval/change reference, operator audit identity, target reference, state transition, canonical principal references, generation before/after, bounded outcome code, and trace/correlation references.
- Audit data must not include bearer values, JWTs, claims, secret values, database URLs, raw provider errors, protected content, or unredacted source identifiers. Evidence uses approved opaque references and bounded reason codes.
- Secret values are fetched/stored only through an externally approved secret mechanism. Provider choice, process-scoped read policy, rotation, expiry, revocation, and unavailable-secret behavior remain open under S7 and Runtime ownership. GitHub stores only non-secret references.
- Do not put credentials in command-line arguments, issue comments, logs, Compose files, deployment artifacts, evidence reports, or exception text.

### Source-level reuse and #508 boundary

| Existing component | Reuse proposal | Boundary |
|---|---|---|
| Canonical auth record types and validation | Reuse contracts and validation semantics | Does not authorize mutation or define temporary lifecycle. |
| PostgreSQLAuthorizationDirectory | Reuse for post-provisioning current-resolution checks | Resolver only; never call it a provisioning API. |
| TrustedRequestContextBoundary and request composer | Reuse for normal authenticated API checks after provisioning | Request context cannot create operator authority or grants. |
| AccessPolicy activation workflow and PostgreSQL RLS | Reuse unchanged for content grants and tenant enforcement | Provisioner can require/verify an approved policy; it cannot bypass, author, or activate it. |
| Audit and generation contracts | Reuse existing audit writer and atomic generation rules where the selected mutation seam supports them | New provisioning operation must have durable safe evidence and atomic effective changes. |
| spine-qualification-bootstrap (#508) | Reuse as reference for strict frozen config, explicit development guard, deterministic identity construction tests and disposable-target naming patterns | Its output is a single administrator identity shared across two #508 scopes. It lacks SOURCE/RESTORE separation, service principals, content grants, hard expiry and revoke/cleanup. Do not point it at #509 targets or extend its privileges implicitly. |
| PostgreSQLAuthorizationSeedHelper | Reuse only as test/migration implementation evidence when designing contract tests | It remains migration-owner/test-only and is excluded from this approved operator interface. |
| DB role bootstrap/migration | Reuse existing role separation checks as input to target validation | Provisioning does not create broad roles or perform migrations during app startup. Recovery-specific roles require their own approved references. |

No function from #508 is proposed as the approved SOURCE/RESTORE provisioning entrypoint. Reusing a validation type or test pattern is distinct from reusing the CLI, changing the CLI, or approving a new Security interface.

The reuse boundary is explicit:

- **Reuse a concrete existing operation:** use the canonical authorization
  directory's current-resolution operation to verify a completed binding; use
  the existing AccessPolicy approval/activation workflow when a separately
  approved synthetic content grant is required; use existing audit and
  generation-aware persistence contracts where the mutation boundary supports
  them.
- **Reuse record contracts, not a provisioning implementation:** canonical
  identity/alias/membership/role record types can validate proposed inputs. The
  #508 record builder must not be used as-is: it binds one human alias to
  administrator in two scopes and is not SOURCE/RESTORE-separated.
- **Do not reuse as the operator entrypoint:** the #508 bootstrap command,
  QualificationBootstrapSettings, and PostgreSQLAuthorizationSeedHelper remain
  confined to their current development or migration/test boundaries. Reusing
  their patterns does not authorize changing their supported mode or role.
- **Approved new interface:** S1 selects a separate one-shot command/use case
  and bounded mutation adapter because no existing Security-owned SOURCE/
  RESTORE provisioning entrypoint was found. This contract does not select the
  concrete operator channel, secret provider, or run-specific profile values.

### Existing contracts versus approved new work

| Concern | Existing contracts to reuse | Agreed new work |
|---|---|---|
| Canonical identity, aliases, memberships, environment roles, trusted request context | Existing record/resolver contracts and normal request checks | A bounded mutation boundary for one independently approved operation; no current operator provisioning entrypoint is approved. |
| AccessPolicy and RLS | Existing policy approval/activation and database enforcement | No policy schema change is proposed. Each content grant still needs its own approved policy decision; the provisioner only verifies the approved reference. |
| Authorization generation and audit | Existing generation and audit semantics | Bind activation/revocation, generation, and immutable audit atomically; add a bounded operation ledger/outbox if needed for idempotency and retry. |
| SOURCE/RESTORE service bindings and credentials | Existing principal references only where consuming contracts enforce required scope and expiry | Add only a qualification-scoped binding where required. Target-specific operator, credential, and database-role configuration remains a run-specific prerequisite. No general service-principal registry is approved. |
| Hard expiry of identity and membership authority | Existing revocation and generation behavior; AccessPolicy grant intervals | Approved option A requires a canonical deadline schema representation and resolver enforcement using database time for human and service authority. This extends canonical authorization behavior beyond ADR-0021 and requires a separate ADR before implementation. Reaper-only, secret-expiry-only, or policy-grant-only approaches are insufficient. |
| Provisioning lifecycle and operator authorization | Existing authentication and authorization boundaries | Versioned profile validation, independent operator/change approval verification, idempotent provision/revoke/cleanup, incomplete-operation recovery, and the one-shot command/job are agreed bounded contract work. Concrete operator channel/reference values remain run-specific. |
| Credentials | External secret provider boundary | Provider selection, target/process-scoped read policy, rotation, expiry, and target references remain deployment/run-specific prerequisites; this contract selects no provider and stores no secret value. |

Hard expiry is an agreed change to canonical authorization behavior, not a
description of current behavior. A separate ADR must record this extension before
schema or resolver changes begin; no accepted ADR is silently amended here.

### Proposed implementation order (to be decomposed into tickets after merge)

1. **Canonical expiry contract:** record a separate ADR, then implement schema representation, database-time semantics, canonical human/service resolution, generation behavior, and durable-work revalidation.
2. **Provision/revoke application boundary:** one-shot command/use case using canonical records and approved policy references; independent operator/change approval; idempotent operation state; inactive-first recovery; atomic activation, generation, and audit.
3. **Credential and target adapter:** bind an externally approved secret provider and per-process SOURCE/RESTORE database-role/profile references. No provider or credential value is selected here.
4. **Contract and PostgreSQL verification:** deterministic lifecycle tests and PostgreSQL/RLS/AccessPolicy integration with isolated synthetic Workspaces/Environments. No deployment qualification claim.
5. **Run-specific qualification:** separate operator approval for concrete targets followed by live SOURCE/RESTORE checks and independent evidence review. This is not part of implementation acceptance and does not pass Issue #509 by itself.

## Decision Register S1–S8

S1–S8 were agreed by the project participant and recorded in PR #540. The table records those architectural decisions, not a formal GitHub Review from the Security Owner. Target-specific operational approvals remain separate.

| ID | Agreed architectural decision | Implementation boundary | Owner / operational prerequisite | Effect and testable acceptance criteria |
|---|---|---|---|---|
| **S1 Interface** | New one-shot, qualification-only operator command/job with explicit provision, revoke, and cleanup; no API or startup provisioning. | Versioned allowlisted target/profile and bounded mutation adapter; reuse canonical-purpose interfaces, not #508 CLI or migration/test helper. | Security owns the contract; Operations owns invocation controls and target-profile inventory. | Only the approved operation/profile can mutate; runtime startup cannot provision. |
| **S2 Operator authority** | Independently authenticate the eligible operator and verify an approval/change reference outside request-controlled selectors. | Keep operator authentication separate from operation authorization. No local bearer, request selector, or migration credential supplies this authority. | Concrete operator channel, approver reference, and evidence source are run-specific prerequisites. | Unapproved or selector-only caller is denied before mutation; approved invocation is constrained to its profile. |
| **S3 Canonical bindings/grants** | Reuse canonical human identities/aliases/memberships/environment roles and supported service-principal references; add only a qualification-scoped binding where needed. | Administrator is not content authority. `read_content` and `process_content` are distinct; the existing approved AccessPolicy workflow is the only source of content grants. | Exact principals, issuer/subject aliases, scopes, and policy references are run-specific. | No grant is inferred from claims; provisioner cannot create/activate content grants; least privilege is tested. |
| **S4 Expiry/revocation** | Hard expiry option A: canonical binding carries a deadline; every applicable human and service resolution checks it against database time. AccessPolicy `valid_until` separately bounds content grants. Reaper/secret expiry supplement only. | New schema and resolver behavior; revocation atomically denies, audits, and advances generation. Durable workflows revalidate before each protected continuation and cannot renew expired authority. A separate ADR must record this contract before implementation. | Run-specific deadline and cleanup window remain per operation. | At/after deadline and with cleanup unavailable, authority resolution denies; stale generations fail; service and human paths are covered; admitted work cannot continue protected actions. |
| **S5 SOURCE/RESTORE isolation** | Distinct identities, aliases, credentials, database roles, runtime bindings and operator scopes; verifier is independent/read-only; no SOURCE credential copying. | Reuse existing principal/role contracts only where they enforce this isolation; restore remains fenced/non-serving until separately qualified. | Concrete inventories, secret references, network scopes, and target approvals are run-specific. | SOURCE against RESTORE and verifier against SOURCE fail closed; restore writer cannot serve. |
| **S6 AccessPolicy/RLS** | Keep ADR-0011 AccessPolicy and PostgreSQL RLS enabled and independent. Content grants flow only through the approved AccessPolicy workflow. | Provisioner may verify approved grant references; it cannot create/activate content grants or bypass either enforcement layer. | Each synthetic source/grant requires policy-owner approval for its concrete scope. | Positive and independent negative AccessPolicy/RLS cases pass under restricted DB roles; admin alone cannot read content. |
| **S7 Secrets** | Use an external secret provider; keep values out of GitHub, logs, audit, command arguments, and reports. | Configure provider adapter/references per process and target; no provider is selected in this contract. | Provider, secret references, credentials, read policy, and rotation window are run-specific prerequisites. | Wrong/missing/expired secret denies; rotation revokes old use; no secret values leak. |
| **S8 Evidence/readiness** | Keep implementation evidence separate from live deployment and physical recovery qualification. | Deterministic and PostgreSQL checks qualify code/contracts; target-bound evidence plus independent review qualifies a live run. | Run owner/reviewer and exact target evidence bundle are required for each operation. | Fixtures never count as live PASS; #509 remains owner of physical backup/restore qualification. |

### Surface impact of S1–S8

| Decision | API | Worker / dispatcher | Recovery | #509 |
|---|---|---|---|---|
| S1 | API continues using the existing resolver; no startup provisioning. | Existing service identities remain pre-bound. | Recovery consumes approved bindings only. | Defines the approved one-shot provisioning interface; target-specific operation remains separately authorized. |
| S2 | No caller-selected authority or new public route. | Processes cannot self-provision at startup. | Operators need separate approved invocation authority. | Enables only the approved operator path, not a run. |
| S3 | API gets only route role and any separately approved canary human grant. | Worker and dispatcher identities/grants remain purpose-specific. | Backup, restore, verifier and serving principals stay separate. | Enables trusted application/canary scenarios after target binding. |
| S4 | Canonical resolution rejects expired human and service bindings at the database-time deadline; protected actions revalidate current authority. | Durable activities revalidate before every protected continuation and cannot extend expired authority. | Expired recovery bindings deny further operations; cleanup can retry independently. | Expiry/revocation evidence is required; no live readiness until verified. |
| S5 | API selects only its target's configured identity and database scope. | Worker/dispatcher cannot cross target bindings. | SOURCE, restore writer, verifier and restored serving identities are isolated. | Enables cross-target negative checks; no physical restore authorization follows. |
| S6 | Route authorization remains distinct from content disclosure. | Worker needs its own process_content policy and database scope. | Recovery operators do not gain content grants from backup/restore duties. | Enables dual AccessPolicy/RLS allow/deny evidence. |
| S7 | API receives only its process-scoped secret reference. | Worker/dispatcher each use their own secret access policy. | Restore/verifier and backup secrets are distinct from SOURCE. | Secret mechanism is runtime prerequisite, not grant approval. |
| S8 | API evidence is code/integration until target-bound. | Worker poller and dispatcher evidence remain separately identified. | Recovery evidence requires physical target and independent review. | Keeps implementation, deployment readiness and physical qualification separate. |

## Testing Decisions

Tests assert externally observable authorization outcomes and safe evidence, not private call order. IDs, time, secret provider and target adapters are injected. Fixtures are synthetic, immutable, versioned, and stored outside live developer databases.

### Deterministic tests

- Strict profile schema, version/digest validation, operator approval rejection, SOURCE/RESTORE target collision rejection, and forbidden selector/inline credential fields.
- Provision/revoke state transitions, idempotent replay, same operation ID with different digest conflict, inactive-first activation, partial secret failure, retry, cleanup-pending, audit failure, rollback/revoke behavior.
- Identity/scope allow and deny: wrong Workspace/Environment, disabled alias, revoked membership, insufficient role, stale generation, expired binding, missing read_content, missing process_content, selector authority, cross-SOURCE/RESTORE principal use, verifier-to-SOURCE attempt.
- Time-boundary tests with injected database time on every inventoried human and service authority path, including paths outside `PostgreSQLAuthorizationDirectory`; test immediately before, at, and after expiry, with cleanup unavailable.
- Temporal/durable-work tests admit work before expiry, advance database time past expiry, and prove every later protected read/action is denied at the activity/action boundary while only authority-free cancellation/cleanup may continue.
- Safe diagnostics and audit redaction for tokens, credentials, provider errors, SQL parameters, protected content and cross-tenant identifiers.
- Revocation after application admission: revalidation denies the next protected decision/operation; already delivered bytes are not represented as retractable.

### PostgreSQL integration

- Apply real migrations to disposable PostgreSQL and run under distinct migration, runtime, dispatcher, and any approved recovery roles.
- Prove two Workspaces and multiple Environments remain isolated; alias, memberships, roles, generation and RLS are resolved in consistent current scope.
- Prove identity/alias/membership/role effective activation and revocation update authorization generation atomically and preserve immutable history.
- Exercise AccessPolicy and RLS together for positive and negative read_content/process_content cases; test database-role privilege catalog, forced RLS, pool reuse, and no BYPASSRLS/superuser behavior.
- Inject transaction failure at each persistence boundary and prove there is no partial active grant; test audit failure and idempotent retry.
- Prove expiry denial comes from the approved expiry enforcement source even when cleanup/reaper is unavailable.

### Live deployment qualification — separate from tests

- Only after run-specific operator approval, verify distinct approved SOURCE and RESTORE target inventories, principals, credentials, PostgreSQL role scopes, current policy/generation, AccessPolicy allow/deny, and RLS behavior.
- Exercise SOURCE-to-RESTORE and verifier-to-SOURCE denials; confirm RESTORE is fenced/non-serving until recovery checks pass.
- Revoke credentials and canonical bindings, verify old credentials fail and new requests deny, verify generation and audit references, and scan outputs for secret/content leaks.
- Have an independent reviewer adjudicate metadata-only evidence tied to exact target/profile/commit. Live evidence is opt-in and cannot be produced by this proposal, a test fixture, a fake adapter, a health check, or #508's report.

### Acceptance matrix

| Scenario | Required setup | Expected outcome | Deterministic evidence | PostgreSQL evidence | Live qualification evidence |
|---|---|---|---|---|---|
| Allowed identity/scope | Active approved alias, memberships, role, current generation, required policy and DB scope | Allow only the approved target/action | Contract allow case | Current snapshot, policy and RLS allow | Per-target SOURCE and RESTORE allow evidence |
| Wrong Workspace | No active membership in requested Workspace | Safe denial with no tenant disclosure | Directory contract denial | Two-Workspace denial under runtime role | Verify on each isolated target |
| Wrong Environment | Missing membership/role or mismatched Environment owner | Deny before application operation | Contract/API denial | Scoped resolver and RLS denial | Verify target-specific denial |
| Revoked membership | Active binding then revoked successor | Subsequent authorization denied; history retained | State transition/generation test | Transactional revoke and generation update | Revoke a run binding and verify fresh request denial |
| Disabled alias | Alias status disabled/revoked | Authentication may identify alias, authorization denies | Alias status contract | Resolver denial without enumeration | Disabled alias fails against target |
| Expired qualification binding | Human or service deadline passes while cleanup/reaper is unavailable | Deny from canonical expiry enforcement | Injected database-time before/at/after test on both principal kinds | Database resolver rejects expired human and service binding | Verify deadline denial on isolated target |
| Stale authorization generation | Context captured before membership/role change | Next protected operation requires current authority and denies stale state | Generation contract | Concurrent/revocation integration | Observe target generation and denial |
| Missing read_content | Human has route role but no source grant | Protected content not disclosed | AccessPolicy denial | Policy and RLS composed denial | Canary denies without grant |
| Missing process_content | Worker lacks scoped service grant | Processing denied; no human authority inheritance | Application decision test | Service grant and DB role denial | Worker action denied on target |
| RLS denial | App identity has no matching DB tenant scope | PostgreSQL denies; no AccessPolicy bypass | Context contract/fake denial | Forced-RLS and restricted-role test | Live cross-scope DB denial |
| Cross-SOURCE/RESTORE identity use | Present one target's alias/credential to the other target | Deny; no shared identity binding | Target mapping contract test | Separate target record/role integration | Direct cross-target negative check |
| Verifier accesses SOURCE | Restore verifier credential used on SOURCE | Deny; verifier stays read-only and target-bound | Role binding contract | Verifier role cannot connect/read SOURCE | Live verifier-to-SOURCE denial |
| Operator lacks provisioning authority | Missing/invalid operator identity or approval reference | Reject before mutation; safe audit/diagnostic | Approval validation test | No identity rows/generation changes | Confirm denied invocation and no side effect |
| Duplicate provisioning request | Same operation ID and identical digest; then same ID/different digest | Retry replays stable result; changed request conflicts | Idempotency contract | Unique operation receipt; no duplicate logical rows | Re-run approved operation and compare references |
| Partial failure and rollback | Fail after DB prepare, secret creation, activation, or audit boundary | Binding remains inactive or is revoked; safe retry/cleanup; no half-active authority | Injected adapter failure matrix | Rollback/operation ledger recovery | Confirm interrupted target remains fenced |
| Secret exposure attempt | Secret-shaped values/errors in config or adapter failures | Reject inline secrets; no secret in logs, audit, output, or evidence | Negative leak corpus | Captured DB/diagnostic output scan | Redacted deployment evidence scan |
| Audit failure | Required activation/revocation audit unavailable | Activation is not usable; revocation remains denying; retry state is explicit | Audit fake failure | Audit transaction failure injection | Confirm target remains unavailable/denied with safe evidence |
| Revocation after application admission | Admit/queue work, revoke before protected content/action revalidation | Later protected decision denies; already emitted bytes are not claimed retractable | Application revalidation test | Current policy/generation checked on next decision | Exercise only on approved synthetic target |

### Prior art

Reuse the authorization directory contract and PostgreSQL tests from Issue #70; FastAPI and context tests from #72/#73; content grant tests from test_content_authorization and test_access_policy_activation; migration and forced-RLS catalog tests from #8/#74; and the safe issue-specific qualification report shape from #509. Add no separate general user-management test suite.

## Out of Scope

- General user, invitation, SCIM, administrator, identity federation, or RBAC management API.
- New authority from Workspace/Environment selectors, OIDC groups/roles/email, development local bearer, or identity data supplied by a request.
- Promoting PostgreSQLAuthorizationSeedHelper into runtime or operator provisioning; the approved contract keeps it migration/test-only.
- Expanding spine-qualification-bootstrap beyond the existing #508 disposable development qualification contract.
- Blanket administrator or content-superuser permissions; RLS/AccessPolicy disablement; cross-Workspace access; startup provisioning.
- Creating specific SOURCE/RESTORE identities, memberships, grants, credentials, database roles, provider secrets, or target resources under this spec.
- Physical backup, restore, projection recovery, canary, corpus ingestion, or deployment qualification. These remain separate run-specific operations.
- Changing Issues #5, #506, #507, #509, #529, #530, #532, or #533 or their dependencies as part of this proposal.

## Further Notes

### Security invariants

1. Authentication proves control of one configured alias; it does not grant Workspace/Environment authority.
2. Current canonical identity, membership, role, service authority, AccessPolicy, authorization generation, and RLS remain independently enforced.
3. No client-controlled selector, claim, command option, or profile field can create authority without an independently verified operator approval.
4. Administrator does not imply read_content; service process_content does not imply human disclosure.
5. SOURCE, RESTORE, backup coordinator, restore operator, verifier, serving API, worker, dispatcher, and canary are separate authority boundaries where used.
6. All mutations are explicit and idempotent; history is retained; stale or incomplete authority fails closed.
7. Expiry must deny at the approved deadline even when cleanup fails. Existing identity and membership contracts do not provide this today.
8. Audit evidence is immutable, attributable, bounded and secret/content free.
9. Architecture approval is not approval to provision any concrete target. Every live operation still needs separate target-specific authorization.
10. Test/contract PASS is not live runtime readiness or Issue #509 physical PASS.

### Dependency and readiness disposition

- #531 remains a native child of #5 and currently blocks #509. Do not edit either relationship in this proposal.
- #529 and #530 deterministic implementation can proceed independently. Their live authorization/runtime evidence has P3 as a runtime prerequisite, not an implementation blocker.
- #532 binding work can proceed separately, but physical backup/restore invocation requires approved Security bindings and target approvals.
- #533 is natively dependent on #530; its provider configuration work may proceed independently, while a provider-backed deployed qualification also needs approved principals and run-specific authorization.
- S1–S8 are agreed. After this contract is merged, decompose the bounded implementation work into tickets; implementation begins only through repository workflow. A separate expiry ADR must be recorded before schema/resolver changes.

### Approval and operational authorization boundary

The architecture agreement is recorded by the project participant in PR #540
and is not represented as a formal GitHub Review from the Security Owner. It
approves this bounded contract and recommendations S1–S8 for documentation and
implementation planning. It does not select concrete credentials, secret
provider, targets, operator identities, or approval references. A separate
operator authorization is required for each concrete SOURCE/RESTORE
provisioning and each physical backup, restore, or canary operation. Do not
include credentials, private source identifiers, policy payloads, or concrete
target secrets in GitHub.

### Source references

- ADR-0011, Canonical document access policy; ADR-0021, Provider-neutral OIDC
  and canonical request authorization.
- Issue #5 and implementation/qualification Issues #69–#74.
- Issue #447 and ADR-0039; Issue #508 and its development-only qualification
  bootstrap.
- Production composition D4 in the approved decision register and PR #528's
  architecture approval, which left provisioning mechanism approval to Security;
  the project-participant agreement to the bounded contract is recorded in PR
  #540 and does not represent a Security Owner GitHub Review.
- Issues #529, #530, #532, #533, and #509 for runtime, dispatcher, recovery,
  provider, and physical qualification boundaries.
