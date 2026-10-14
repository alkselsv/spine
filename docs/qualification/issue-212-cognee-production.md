# Issue #212 Cognee production profile qualification

Status: **no-go; production approval is not granted**.

The only candidate evaluated by this gate is Cognee 1.5.4 with PostgreSQL as
its relational store, Neo4j as its graph store and PGVector as its vector
store. The machine-readable decision is recorded in
`docs/qualification/issue-212-cognee-production-report.json`.

No live service endpoints or model resources were available in the execution
environment. The repository now exposes a complete `CogneeContextProjection`
composition over the chunk, graph and structured-retrieval adapters, and that
composition runs the unchanged suite in `tests/contracts/memory`. This closes
the composition gap, but it does not turn offline evidence into a production
qualification: every live observation remains `not_run` until one coherent
PostgreSQL + Neo4j + PGVector execution records it.

The machine-readable report uses schema `cognee-production-qualification:v2`.
Its `canonical_lifecycle_separation` field is `not_verified` unless that exact
gate passes; ownership by PostgreSQL is an architectural invariant, not proof
that physical call timing was observed.

This is the explicit failure outcome allowed by Issue #212. It is not a
conditional approval, a local-profile approval or a deployment recommendation.

## Decision and fallback

- Decision: `no_go`.
- Production approval: `false`.
- `postgres_demo`: rejected as a production graph backend.
- Canonical source, candidate, snapshot, activation and rollback state remains
  owned by PostgreSQL.
- Physical Cognee calls remain outside the PostgreSQL UnitOfWork.
- Recommended fallback: an owned PostgreSQL projection-mapping layer that can
  enforce source-to-chunk/entity/relation provenance, namespace routing,
  tombstone fencing and cleanup independently of Cognee backend behavior.

The fallback is a recommendation, not an approved implementation. It requires
its own ticket and architecture review before it can become production code.

## AC1–AC9 traceability

| Criterion | Evidence | Result |
| --- | --- | --- |
| AC1 | `tests/integration/cognee/test_production_profile.py::test_production_qualification_records_one_coherent_run` | Not run on the real profile; public SDK smoke is present but cannot grant production approval. |
| AC2 | The shared contract runs for `cognee-composition`; the live target performs a physical chunk/entity/relation round trip and verifies provenance | Harness implemented; no live observation is committed. |
| AC3 | The live target executes missing/ambiguous/stale/incomplete/tombstoned fail-closed cases and checks disclosure-safe results | Harness implemented; no live observation is committed. |
| AC4 | The live target executes foreign-workspace isolation, rejects mismatched snapshot/boundary/namespace context before retrieval and keeps distinct active/shadow datasets from leaking into each other | Harness implemented; no live observation is committed. |
| AC5 | Bounded gate runner tests plus injected provider failure in the live target | Harness emits only typed safe codes; no live observation is committed. |
| AC6 | `src/spine/memory/cognee/qualification.py::build_backend_matrix` and `tests/memory/test_cognee_production_qualification.py::test_postgres_demo_is_rejected_even_when_observations_pass` | `postgres_demo` remains rejected. |
| AC7 | ADR 0018/0027 plus the required `canonical_lifecycle_separation` gate | The repository has no PostgreSQL-backed projection-request resolver composition to observe yet, so the gate remains `not_run` and prevents a false production `go`. |
| AC8 | This report and its `not_run` live observations | No-go with owned fallback recommendation; no unexecuted gate is marked passed. |
| AC9 | Issue #211 report and `tests/memory/test_cognee_production_qualification.py::test_fake_backed_success_cannot_be_promoted_to_production_approval` | Issue #211 is explicitly not production evidence. |

## Executable gate

The default suite verifies the fail-closed decision model, report consistency,
traceability and collection of the live target. The operator-controlled live
command is documented in
`docs/runbooks/cognee-production-qualification.md`.

The live target executes the available physical gate inventory in one run: public SDK,
structured round trip, unsafe evidence, workspace isolation, simultaneous active/shadow
isolation, explicit snapshot/boundary/namespace context, update/remove/tombstone,
shadow rebuild/recovery/diagnostics,
and concurrency/failure injection/cleanup. The required canonical-lifecycle
gate remains explicitly `not_run` until a PostgreSQL-backed resolver composition
can be observed; therefore the target cannot produce `go` prematurely. It writes
only bounded observations. The committed report changes only after that
operator-controlled output has been reviewed.
