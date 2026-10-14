# Issue #212 Cognee production profile qualification

Status: **no-go; production approval is not granted**.

The only candidate evaluated by this gate is Cognee 1.5.4 with PostgreSQL as
its relational store, Neo4j as its graph store and PGVector as its vector
store. The machine-readable decision is recorded in
`docs/qualification/issue-212-cognee-production-report.json`.

A coherent live run, `issue212-docker-006`, exercised Cognee 1.5.4 against a
disposable PostgreSQL 17 database with PGVector 0.8.7 and isolated Neo4j
5.26.32 Community + APOC dataset containers. Operator-supplied LLM and
embedding resources were used without retaining credentials, endpoints,
provider responses or source content in the report.

The run is a production **no-go**. Cognee's public chunk and hybrid search
responses did not carry the evidence kinds required to reconstruct stable
Spine provenance. The physical chunk payload also lacked a usable chunk
identity/provenance shape, so structured retrieval correctly abstained with
`incomplete_evidence`. An absent foreign-workspace dataset surfaced as a
bounded `physical_failure`, rather than the required isolated missing result.
Spine did not weaken any evidence or isolation check to obtain a pass.

The rebuild/recovery/physical-validation gate passed, proving that the pinned
backend profile can execute physical writes, injected recovery and content-free
artifact validation. That isolated success is not sufficient for approval.

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
| AC1 | `tests/integration/cognee/test_production_profile.py::test_production_qualification_records_one_coherent_run`; run `issue212-docker-006` | Failed: required public search evidence was absent. |
| AC2 | Live structured round trip plus the shared `cognee-composition` contract | Failed live: physical chunk shape could not reconstruct provenance. |
| AC3 | Live missing/ambiguous/stale/incomplete/tombstoned cases | Failed: physical status/provenance was not preserved; results remained fail-closed. |
| AC4 | Live foreign-workspace, explicit-context and simultaneous active/shadow scenarios | Failed; no protected content was returned. |
| AC5 | Bounded gate runner diagnostics plus injected provider failure | Passed for safe reporting; the combined concurrency gate failed retrieval completeness. |
| AC6 | `src/spine/memory/cognee/qualification.py::build_backend_matrix` and `tests/memory/test_cognee_production_qualification.py::test_postgres_demo_is_rejected_even_when_observations_pass` | `postgres_demo` remains rejected. |
| AC7 | ADR 0018/0027 plus the required `canonical_lifecycle_separation` gate | The repository has no PostgreSQL-backed projection-request resolver composition to observe yet, so the gate remains `not_run` and prevents a false production `go`. |
| AC8 | This report and coherent live run `issue212-docker-006` | No-go with owned fallback recommendation; only the observed rebuild/recovery gate is marked passed. |
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
concurrency/failure injection/cleanup. The required canonical-lifecycle gate
remains explicitly `not_run` until a PostgreSQL-backed resolver composition can
be observed; therefore the target cannot produce `go` prematurely. It writes
only bounded observations.
