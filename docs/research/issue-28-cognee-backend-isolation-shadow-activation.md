# Issue #28: Cognee Backend Isolation and Shadow Activation

Status: throwaway prototype decision note for `prototype/cognee-backend-isolation-shadow-activation`.

Warning: this branch and the prototype artifact are primary-source evidence for the architecture question. They must not be merged as production code. Production work should extract contract tests and a real `ContextProjection` implementation ticket set.

## Question

Which concrete Cognee 1.5.4 relational/graph/vector backend profile can satisfy R1 production semantics for two-workspace isolation, provenance round-trip, concurrency, update/delete, full shadow rebuild, validation, atomic active-version switching, and rollback?

## Sources Read

- `gh issue view 28 --comments` was attempted first, but GitHub CLI failed on the deprecated classic Projects GraphQL field `repository.issue.projectCards`. The issue was then read with `gh issue view 28 --json title,body,comments,state,labels,author,url`; it has no comments.
- `AGENTS.md`
- `docs/adr/0001-canonical-store-and-context-graph.md`
- `docs/ARCHITECTURE.md`
- `docs/ROADMAP.md`
- `docs/INTERFACE.md`
- `docs/research/cognee-examples-platform-fit.md`
- `pyproject.toml`
- `uv.lock`
- `src/spine/memory/contracts.py`
- `src/spine/memory/cognee_memory.py`
- `src/spine/memory/cognee/`
- Installed Cognee 1.5.4 package files under `.venv/lib/python3.12/site-packages/cognee/`

## Executable Prototype

Open `src/spine/memory/cognee/prototype_backend_isolation_shadow_activation.html` in a browser.

It is a self-contained logic/state model. It uses no persistence and no production code paths. It shows the complete state after every scenario:

- two-workspace deterministic dataset isolation;
- node-set as retrieval scope, not authorization;
- provenance references that round-trip to source revision, locator, projection version, workspace, and security domain;
- append-only update and tombstone fencing;
- shadow rebuild, validation, atomic active pointer switch, failed validation, rollback successor;
- concurrent activation attempts with compare-and-swap preventing split brain.

## Facts Confirmed

- `uv.lock` pins `cognee==1.5.4`; `uv sync --extra dev` installed `cognee==1.5.4`.
- Cognee 1.5.4 top-level import logs `cognee_version=1.5.4`.
- Calling Spine's `configure_environment()` before Cognee import redirects Cognee storage to project-local `.spine/system`, `.spine/data`, `.spine/cache`, and `.spine/logs`. Without that timing, Cognee can initialize storage under the installed package path.
- Cognee 1.5.4 includes relational, graph, vector, and unified-store implementation surfaces, including:
  - relational: SQLite/Postgres/Turso config via SQLAlchemy;
  - graph: Ladybug/Kuzu, Neo4j, Neptune, Turso, and `postgres_demo`;
  - vector: LanceDB, PGVector, Turso;
  - hybrid: Postgres hybrid adapter that composes `PostgresDemoAdapter` with `PGVectorAdapter`.
- Cognee's `postgres_demo` adapter source says it is a demo feature and is not production-ready, and recommends graph-native Kuzu or Neo4j for production workloads. That is enough to explicitly reject `postgres_demo` as the R1 production graph backend.
- Cognee's public examples/research remain useful only behind Spine's Context Broker and ContextProjection boundary. Direct `remember`, `recall`, `forget`, completion strings, node sets, and Cognee ACL do not replace Spine's authorization, provenance, active snapshot, or audit model.

## Backend Profile Comparison

| Criterion | Local test profile | Production candidate | `postgres_demo` graph |
|---|---|---|---|
| Concrete profile | Cognee 1.5.4 with SQLite relational, Kuzu/Ladybug graph, LanceDB vector | Cognee 1.5.4 with Postgres relational, Neo4j graph, PGVector vector | Cognee 1.5.4 with Postgres relational, `postgres_demo` graph, PGVector vector |
| Intended use | Local/dev contract prototype and deterministic fake-backed tests | R1 production candidate, pending real integration and load tests | Reject for production graph |
| Two-workspace isolation | Pass as state model using deterministic datasets; real Cognee ACL requires contract test | Unknown until tested against real Neo4j/PGVector/Postgres deployment | Fail/no-go as production graph choice |
| Provenance round-trip | Pass as Spine-owned mapping model; Cognee references alone insufficient | Unknown until adapter proves graph/vector IDs map to SourceRevision locators | Unknown, but graph backend rejected |
| Update/delete | Pass as state model with append-only revision and tombstone fence | Unknown until delete/reindex/tombstone tests prove physical stores respect manifest | Unknown, but graph backend rejected |
| Full shadow rebuild | Pass as state model using separate candidate scope | Unknown until candidate artifacts can be addressed, validated, retained, and cleaned | Unknown, but graph backend rejected |
| Atomic activation | Pass only because Postgres-owned active mapping is outside Cognee | Pass requirement belongs to Spine Postgres transaction, not Cognee | Same ownership rule, but graph backend rejected |
| Rollback | Pass as successor snapshot, not pointer rewind | Unknown until artifact reuse/integrity rules are tested | Unknown, but graph backend rejected |
| Concurrency | Pass as state model with Postgres compare-and-swap | Unknown until activation table locks/advisory locks are implemented and tested | Unknown, but graph backend rejected |
| Production recommendation | Yes for local tests only | Conditional go for feasibility spike | No-go |

## Recommendation

Use the following profile split:

1. Local test profile: `sqlite + kuzu/ladybug + lancedb`.
2. Production candidate: `postgres + neo4j + pgvector`.
3. Explicitly reject `postgres_demo` as production graph, including Postgres hybrid if it relies on `PostgresDemoAdapter` for graph semantics.

This is not a full go for production. It is a conditional go to implement `ContextProjection` contract tests against the production candidate. The prototype proves the required ownership split:

- Postgres owns canonical source revisions, tombstones, access policies, projection manifests, validation results, active pointer, activation generation, audit, and outbox.
- Cognee owns rebuildable graph/vector/relational projection artifacts behind deterministic dataset/snapshot scopes.
- Context Broker reads only the Postgres-selected active projection and rechecks current authorization, tombstone, revision validity, evidence availability, and `ContextProfile` before returning content.

If Neo4j/PGVector/Postgres cannot pass the contract tests, Spine must implement an owned fallback where `ContextProjection` keeps enough source-to-chunk/entity/relation/vector mapping in Spine-owned Postgres tables to enforce isolation, active snapshot routing, tombstone fencing, provenance, and cleanup independently of Cognee backend behavior.

## Projection Lifecycle State Table

| State | Owner | Ordinary retrieval? | Exit |
|---|---|---:|---|
| `unpublished` | Postgres | No | first validated snapshot activation |
| `building` | Projection worker + Cognee | No | receipt complete or failed |
| `validating` | Evaluation worker + Postgres records | No | pass/fail evidence, retrieval regression, ACL leakage, manifest completeness |
| `complete_candidate` | Postgres manifest + Cognee artifacts | No | atomic activation CAS |
| `active` | Postgres active pointer | Yes, only through Context Broker | successor activation |
| `superseded` | Postgres activation history | No ordinary retrieval | retention cleanup or authorized history inspection |
| `failed` | Postgres run/validation records | No | retry same idempotent operation or create successor |
| `rollback_successor` | Postgres + projection worker | Only after activation | becomes active through same validation and CAS path |

```text
canonical boundary
  -> build shadow candidate
  -> validate evidence/retrieval/ACL/manifest
  -> Postgres compare-and-swap active pointer
  -> Context Broker reads active generation
  -> successor catch-up or rollback candidate
```

## Requirements for Future `ContextProjection`

- Deterministic dataset naming: `workspace:{workspace_id}:environment:{environment}:security-domain:{domain}:snapshot:{snapshot_id}` or equivalent opaque mapping stored in Postgres.
- `node_set` may narrow source type, project, or retrieval strategy; it must never be the only security boundary.
- Public methods: `project(ProjectionCommand) -> ProjectionReceipt`, `remove(RemoveProjectionCommand) -> ProjectionReceipt`, `retrieve(RetrievalRequest) -> RetrievalResult`.
- `ProjectionReceipt.source_to_projection_refs` must map Cognee graph/vector/chunk identifiers to `SourceRevision`, stable locator, projection config version, workspace, environment, and security domain.
- Retrieval must return structured chunks/entities/relations/references and never parse provenance from final answer strings.
- Tombstones must fence previous content at Context Broker time even if physical Cognee cleanup is asynchronous.
- Candidate scopes must be invisible to ordinary retrieval until Postgres activation.
- Activation must be one Postgres transaction: verify expected predecessor, candidate eligibility, monotonic boundary, switch pointer, increment activation generation, append activation record, enqueue invalidation/catch-up outbox.
- Rollback must create a new successor snapshot with non-regressing boundary; it must not rewind the pointer to an old snapshot.
- Delayed Cognee aliases, caches, or cleanup jobs must never be second authorities.
- Denied retrieval must not leak protected text in result text, references, logs, diagnostics, raw errors, or provider prompts.

## Contract Tests to Create Production Tickets For

- `test_cognee_version_is_1_5_4_from_lock`
- `test_context_projection_configures_project_local_cognee_roots_before_import`
- `test_workspace_a_retrieval_never_returns_workspace_b_text`
- `test_node_set_scope_is_not_authorization_boundary`
- `test_denied_retrieval_redacts_text_references_logs_and_errors`
- `test_retrieval_references_round_trip_to_source_revision_locator_snapshot_workspace_domain`
- `test_provenance_is_not_parsed_from_answer_string`
- `test_new_source_revision_does_not_overwrite_prior_revision`
- `test_tombstone_fences_prior_content_before_physical_cleanup`
- `test_shadow_candidate_is_not_visible_to_context_broker`
- `test_validation_blocks_activation_on_evidence_regression_or_acl_leakage_failure`
- `test_atomic_activation_compare_and_swap_allows_only_one_concurrent_success`
- `test_context_broker_reads_only_postgres_active_generation`
- `test_operator_rollback_creates_successor_snapshot_not_pointer_rewind`
- `test_stale_cognee_cache_or_alias_cannot_override_postgres_active_pointer`
- `test_postgres_demo_profile_is_rejected_for_production_graph`
- `test_prod_candidate_profile_passes_acl_delete_rebuild_backup_restore_load_requirements`

## Prerequisites Still Needed

The following remain `unknown`, not passed:

- Real Neo4j credentials/service for Cognee graph backend.
- Real Postgres with PGVector extension for Cognee vector backend and Spine active mapping tables.
- LLM/embedding configuration or local embedding profile that lets Cognee execute `remember/recall` without external provider ambiguity.
- Contract-test fixture that can inspect/clean Cognee physical artifacts by deterministic snapshot scope.
- Load/concurrency test environment to run simultaneous projection rebuilds and activation attempts.

## Go / No-Go

No-go for using Cognee alone as the authority for R1 production semantics.

Conditional go for Cognee 1.5.4 as the projection engine behind a Spine-owned `ContextProjection`, with `postgres + neo4j + pgvector` as the concrete production candidate. The next production work must start with contract tests and Postgres-owned activation tables before any adapter is treated as production-ready.
