# Issue #509 runtime and recovery composition proposal

**Status:** draft for deployment, security, and product-owner review. This is not
an approved runtime profile or qualification evidence. It does not amend Issue
#509, #506, or the accepted ADRs.

## Problem

Spine has the production application contracts and adapters for durable Q&A,
authorization, projection delivery, coordinated backup, and isolated restore.
It does not have an executable deployment factory that assembles those
dependencies from the approved local profile. The Compose API service still
starts the legacy `spine.api.main:app`. The #513 PostgreSQL/Temporal integration
test assembles a graph inside the test and uses deterministic substitutes for
the provider and authorization edges; it is not a deployable application graph.

## Existing architecture and seams

- `ProductionAnswerQuestionCapability` owns the authorized application
  capability. `compose_durable_ask` binds it to PostgreSQL-backed Ask Admission,
  the canonical outbox consumers, Temporal workflow client, executor, capture,
  and API factory.
- `DurableAskComposition` now has a worker factory that registers its own
  executor, Temporal SDK client, workflow, activities, and dispatch task queue.
  The outbox remains a separate worker boundary.
- `PostgreSQLPersistence` / `PostgreSQLTenantUnitOfWorkFactory`, the dispatcher
  repository, and role-specific database settings are implemented.
- `compose_local_api_authorization` uses the local credential adapter and the
  canonical PostgreSQL authorization directory. OIDC has a separate adapter.
- `create_production_outbox_worker` composes the existing projection request
  and recovery route with the outbox worker. It expects all authority, storage,
  provider, and lifecycle dependencies from its caller.
- `CoordinatedBackupProfile`, PostgreSQL custom-format backup, finalized
  original-storage backup, protected artifact storage, the PostgreSQL recovery
  repository, and `IsolatedRestoreVerifier` are implemented and have bounded
  tests. They have no deployment-approved operator entrypoint.
- Existing recovery runbooks explicitly require an owner-reviewed entrypoint,
  approved destination, identities, encryption/retention, and a dedicated empty
  restore target. They prohibit direct `pg_dump`, `tar`, manual manifests, and
  restoring over SOURCE.

## Classification

| Component | Existing implementation | Missing binding | Class | Owner / next action |
|---|---|---|---|---|
| Temporal SDK worker | Workflow/activity definitions and `DurableAskComposition` worker factory | None at the registration boundary; no deployable process config/lifecycle | A completed in this branch | Runtime owner: consume the factory from the single approved application root |
| API and durable Q&A | `create_api_app`, `compose_durable_ask`, production capability and PostgreSQL adapters | Configuration-driven construction of the complete trusted dependency graph | C | Runtime owner: implement the already approved narrow #506 qualification factory after settings/authority are approved |
| Outbox dispatch | `OutboxDispatcher`, `OutboxWorker.dispatch_batch`, PostgreSQL delivery repository and production projection binding | A long-lived deployment runner and a combined production consumer registry; no new dispatcher is needed | C | Runtime owner: bind existing consumer routes, bounded poll cadence, shutdown, and role-specific context |
| Cognee / Context Projection | PostgreSQL/Cognee request and recovery consumers; configured provider adapter | Actual source/restore provider configuration, credentials and model budget permission; no deployment graph | C | Provider owner: supply pinned profile and allowed request/budget limits before runtime calls |
| Trusted API authorization | Local and OIDC composition, trusted request boundary, PostgreSQL authorization lookup, AccessPolicy/RLS contracts | No runtime canonical identity/grant provisioning interface was found; the available seed helper is explicitly test-only | C | Security owner: identify an existing provisioning path or approve a narrowly scoped operator-owned provisioning contract; do not bootstrap grants from selectors |
| Original storage | Filesystem original-object adapter and coordinated backup source | Process configuration tying canonical inventory, store root, immutable artifact destination, encryption and retention | C | Storage/deployment owner: approve paths/volume identity and protected destination profile |
| Coordinated backup | Fence/coordinator, PostgreSQL and original-storage component sources, immutable manifest and evidence | No reviewed operator entrypoint or configured backup/verifier identities | C | Recovery operator: approve exact local synthetic scope, identities, destination and protection settings |
| Isolated restore | `IsolatedRestoreVerifier`, PostgreSQL restore and reconciliation implementations | No separately provisioned empty target, restore/verifier identities, or deployment entrypoint | C | Recovery/security owners: approve target inventory and ensure it is non-serving and isolated |
| Projection recovery | Existing outbox recovery consumer, canonical resolver, lifecycle/result recorder | No configured physical provider and no deployed worker process using this route | C | Projection owner: approve provider profile and the same outbox worker binding |
| Canary / diagnostics | Safe diagnostics and audit contracts; no authorized recovery canary implementation found | No canary principal, approved target, or operator evidence binding | C | Security/operator owner: specify a bounded authorized canary and diagnostic correlation evidence |

## Proposed minimum integration boundary

Complete the narrow single-deployment composition already described by #506;
do not add a second application runtime or dispatcher:

1. A settings-backed runtime factory constructs the exact existing PostgreSQL,
   trusted context, API authorization, `ProductionAnswerQuestionCapability`,
   `DurableAskComposition`, and provider dependencies. Secret values remain
   process-provided and are never returned by readiness or diagnostics.
2. API and worker consume the same immutable composition. The API starts only
   after trusted configuration and authorization-store readiness; the worker
   registers the exact Temporal client and queue from that composition.
3. One bounded outbox worker process uses existing Ask and projection consumer
   bindings, PostgreSQL dispatcher role, safe diagnostics, retry/lease settings,
   and cooperative shutdown. It does not add a new queue or orchestration loop
   over Temporal-owned workflow state.
4. Source and restore have distinct PostgreSQL credentials, Temporal clients,
   provider configuration, original-storage roots, workload identities, and
   trusted-context issuers. No shared runtime identity or data volume is allowed.
5. Recovery operator commands, if approved, call the existing coordinator and
   verifier with validated deployment-specific settings. They do not create a
   second backup mechanism or persist hand-written receipts.

The application must fail closed when a required binding is absent or mismatched.
The factory must not create identities, memberships, role grants, AccessPolicy,
or provider permissions. It must not start paid provider calls during startup or
readiness.

## Acceptance criteria

- A real configured SOURCE API and worker are constructed from the same exact
  application graph and canonical PostgreSQL context; mismatched SDK client,
  queue, or persistence binding fails startup.
- A supported authenticated request passes positive and negative workspace /
  environment / AccessPolicy checks without selector-derived authority.
- An allowed synthetic ask is admitted through the API, committed to PostgreSQL,
  dispatched from the canonical outbox, executed by registered Temporal pollers,
  and reaches canonical terminal persistence. No fake adapter is represented as
  deployment qualification evidence.
- The runtime report separates configuration, reachability, authorization,
  worker registration, application execution, and provider readiness.
- Recovery can run only with an approved SOURCE scope, fence identity, protected
  destination, immutable manifest verification, separate empty RESTORE target,
  and independent verifier credentials.
- Restored canonical state remains fenced/untrusted until component digests,
  reconciliation, tenant/RLS isolation, and physical projection recovery pass.
- The approved authorized canary and safe diagnostic/audit correlation pass
  before any corresponding recovery criterion is marked PASS.

## Decisions needed before physical qualification

1. **Authorization provisioning:** Which security-owned canonical path creates
   the synthetic local identity, alias, workspace/environment membership, and
   least-privilege roles? Existing source only exposes a PostgreSQL seed helper
   marked for migration-owner tests; it is not a runtime provisioning API.
2. **Provider profile:** Which pinned local Cognee/Neo4j/PGVector and
   LLM/embedding settings are approved? Are any actual provider requests allowed,
   with what request cap and budget? Until approved, only no-provider startup and
   isolated deterministic tests may run.
3. **Backup protection:** Which local protected artifact destination, encryption
   mechanism/key owner, retention policy, PostgreSQL tool profile, backup
   identity, and immutable-manifest reader are approved for the synthetic run?
4. **Restore target:** Which separately provisioned empty PostgreSQL database and
   filesystem volume are approved, and which distinct restore and verifier
   identities own them? They must not be SOURCE volumes.
5. **Canary and review:** What exact synthetic operation is the authorized
   canary, which principal may execute it, and who independently reviews the
   content-free evidence?

## Verification and non-goals

Use deterministic composition tests, the existing PostgreSQL and Temporal
integration suites, positive/negative authorization tests, and then the actual
approved Docker profile. Report each evidence level separately. Unit tests,
Compose validation, `/health`, Temporal `SERVING`, and a test-environment
workflow do not establish deployed application or recovery readiness.

No production deployment, private corpus, Golden Dataset benchmark, paid
provider call, direct backup/restore command, live-source mutation, volume
deletion, new workflow engine, alternate persistence, or new authorization
system is proposed.
