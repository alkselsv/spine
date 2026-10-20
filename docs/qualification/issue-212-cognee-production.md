# Issue #212 Cognee production profile qualification

Status: **no-go; production approval is not granted**.

The only candidate evaluated by this gate is Cognee 1.5.4 with PostgreSQL as
its relational store, Neo4j as its graph store and PGVector as its vector
store. The machine-readable decision is recorded in
`docs/qualification/issue-212-cognee-production-report.json`.

A coherent live rerun for Issue #507, `issue507-blocked-001`, exercised Cognee 1.5.4 against a
disposable PostgreSQL 17 database with PGVector 0.8.7 and isolated Neo4j
5.26.32 Community + APOC dataset containers. Operator-supplied LLM and
embedding resources were used without retaining credentials, endpoints,
provider responses or source content in the report.

Nine physical real-backend gates passed in that run: the audited public SDK surface,
structured chunk/entity/relation round trip, unsafe-evidence handling,
two-workspace and active/shadow isolation, explicit retrieval context,
update/remove/tombstone behavior, rebuild/recovery/physical diagnostics, and
concurrency/failure-injection cleanup. The tenth gate exercised the production
PostgreSQL resolver, but failed closed on the current ingestion-binding schema before the
physical call. The bounded report records only `configuration_mismatch`; the
specific defects are tracked by Issues #514 and #515. Issue #516 tracks the
production PostgreSQL parsed-artifact and observed Cognee receipt composition
required before the gate may pass. Stable provenance is reconstructed by a
strict public join from CHUNKS `document_id` to `datasets.list_data` metadata;
provider-generated hybrid lineage is not trusted. Spine did not weaken any
evidence or isolation check to obtain these passes.

The machine-readable report uses schema `cognee-production-qualification:v2`.
Its `canonical_lifecycle_separation` field remains `not_verified` because the
exact production composition did not complete. PostgreSQL ownership is an
architectural invariant, not a substitute for observed handoff evidence.

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
| AC1 | `tests/integration/cognee/test_production_profile.py::test_production_qualification_records_one_coherent_run`; run `issue507-blocked-001` | Passed: required public operations and the public dataset-metadata identity join were observed. |
| AC2 | Live structured round trip plus the shared `cognee-composition` contract | Passed for chunks, typed entities, typed relations and stable provenance. |
| AC3 | Live missing/ambiguous/stale/incomplete/tombstoned cases | Passed fail-closed with the expected classifications and no content leakage. |
| AC4 | Live foreign-workspace, explicit-context and simultaneous active/shadow scenarios | Passed. |
| AC5 | Bounded gate runner diagnostics plus injected provider failure | Passed, including recovery, concurrency and cleanup. |
| AC6 | `src/spine/memory/cognee/qualification.py::build_backend_matrix` and `tests/memory/test_cognee_production_qualification.py::test_postgres_demo_is_rejected_even_when_observations_pass` | `postgres_demo` remains rejected. |
| AC7 | ADR 0018/0027/0030 plus the required `canonical_lifecycle_separation` gate | Failed closed with `configuration_mismatch` in the current PostgreSQL ingestion-binding schema; Issues #514, #515 and #516 retain the discovered defects and missing production composition. |
| AC8 | This report and coherent live run `issue507-blocked-001` | Explicit no-go: nine physical gates passed, one mandatory canonical-lifecycle gate failed, and production approval is false. |
| AC9 | Issue #211 report and `tests/memory/test_cognee_production_qualification.py::test_fake_backed_success_cannot_be_promoted_to_production_approval` | Issue #211 is explicitly not production evidence. |

## Executable gate

The default suite verifies the fail-closed decision model, report consistency,
traceability and collection of the live target. The operator-controlled live
command is documented in
`docs/runbooks/cognee-production-qualification.md`.

The live target executed the available physical gate inventory in one run:
public SDK, structured round trip, unsafe evidence, workspace isolation,
simultaneous active/shadow isolation, explicit snapshot/boundary/namespace
context, update/remove/tombstone, shadow rebuild/recovery/diagnostics, and
concurrency/failure injection/cleanup, and the PostgreSQL-backed canonical
resolver. The target cannot produce `go` unless every required
observation is `passed`; it writes only bounded observations.

The canonical-lifecycle gate deliberately fails closed until Issues #514, #515
and #516 provide the remaining production composition. It does not substitute
an in-memory parsed-artifact repository or fabricate a Cognee adapter receipt
from fixture input.
