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
| AC1 | `tests/integration/cognee/test_production_profile.py::test_public_cognee_profile_exercises_structured_sdk_lifecycle` | Not run on the real profile; public SDK smoke is present but cannot grant production approval. |
| AC2 | `tests/contracts/memory/test_conformance.py::test_retrieval_round_trips_chunks_entities_relations_and_provenance` runs for `cognee-composition`; the same real-backend round trip remains to be run | No-go until live evidence exists. |
| AC3 | `tests/contracts/memory/test_conformance.py::test_unsafe_evidence_never_leaks_projected_content` runs for `cognee-composition` | Offline behavior passes; real missing/ambiguous/stale/incomplete/tombstoned evidence is not qualified. |
| AC4 | The isolation contract runs for `cognee-composition` | Real two-workspace and active/shadow isolation are not qualified. |
| AC5 | Existing adapter disclosure-safe tests plus `tests/memory/test_cognee_production_qualification.py::test_report_shape_contains_only_bounded_safe_codes` | The decision/report boundary is safe; real provider error and diagnostic paths are not qualified. |
| AC6 | `src/spine/memory/cognee/qualification.py::build_backend_matrix` and `tests/memory/test_cognee_production_qualification.py::test_postgres_demo_is_rejected_even_when_observations_pass` | `postgres_demo` remains rejected. |
| AC7 | ADR 0018/0027 plus the `canonical_lifecycle_separation` live gate | Ownership is preserved by contract; real call timing outside PostgreSQL UnitOfWork is not yet observed. |
| AC8 | This report and its `not_run` live observations | No-go with owned fallback recommendation; no unexecuted gate is marked passed. |
| AC9 | Issue #211 report and `tests/memory/test_cognee_production_qualification.py::test_fake_backed_success_cannot_be_promoted_to_production_approval` | Issue #211 is explicitly not production evidence. |

## Executable gate

The default suite verifies the fail-closed decision model, report consistency,
traceability and collection of the live target. The operator-controlled live
command is documented in
`docs/runbooks/cognee-production-qualification.md`.

The live target verifies the public SDK lifecycle and a complete physical
project/retrieve round trip. Passing it alone must not be used to edit the report to `go`:
the report changes only from the complete gate inventory captured by one live
qualification run.
