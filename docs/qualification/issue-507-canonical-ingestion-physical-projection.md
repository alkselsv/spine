# Issue #507 canonical ingestion and physical projection qualification

Status: **blocked; production approval is not granted**.

This qualification composes the current canonical-ingestion evidence with a
fresh real-provider run. The five-format corpus is the repository's approved,
synthetic qualification corpus: it generates actual PDF, DOCX and XLSX bytes
and exercises Markdown and TXT bytes without retaining document content in the
report. It is not the private Golden Dataset and makes no complete R1 release
claim.

The latest coherent physical run is `issue507-resume-001`. It used Cognee 1.5.4,
PostgreSQL 17, Neo4j 5.26.32 Community with APOC, and PGVector 0.8.7.
The model bindings were `openai/gpt-5.4-nano` and
`openai/text-embedding-3-small`. The machine-readable result is
`docs/qualification/issue-212-cognee-production-report.json`; it contains only
the bounded backend profile, gate observations, decision and run identity.

## Acceptance traceability

| Criterion | Evidence | Result |
| --- | --- | --- |
| AC1 | `docs/qualification/issue-194-document-ingestion-report.json`; `tests/application/test_document_ingestion.py::test_document_ingestion_completes_each_r1_format_with_parser_metadata`; parser locator contract suites | Repository-qualified for PDF, DOCX, XLSX, Markdown and TXT. The R4 operator-approved deployed upload/UI run and its per-format metadata are unavailable, so this criterion remains blocked. |
| AC2 | Issue #194 AC2–AC5; canonical acceptance and source lifecycle contracts; `tests/application/test_document_ingestion.py::test_projection_request_is_not_visible_when_its_transaction_fails` | Repository-qualified for duplicate/retry, changed revision, tombstone and partial failure. The same lifecycle has not been observed through the required deployed R4 path, so this criterion remains blocked. |
| AC3 | ADR-0030; `tests/integration/cognee/test_production_profile.py::test_canonical_lifecycle_resolves_postgresql_before_physical_projection`; the same gate inside `test_production_qualification_records_one_coherent_run` | Issue #514, Issue #515 and Issue #516 are integrated. The gate now resolves PostgreSQL state, persists parsed artifacts through PostgreSQL and observes one physical Cognee call outside the UnitOfWork. Receipt mapping then fails closed: the real graph evidence uses the `stable-locator:r1` shape while the receipt builder expects `stable-locator:v1`; the report records `configuration_mismatch`. A narrowly scoped adapter follow-up is required. |
| AC4 | `docs/qualification/issue-212-cognee-production-report.json`; `structured_round_trip`, `unsafe_evidence`, `explicit_retrieval_context` and `update_remove_tombstone` observations | Passed: chunk/entity/relation evidence retains exact SourceRevision lineage; missing, ambiguous, stale, incomplete and tombstoned evidence fails closed. |
| AC5 | The same report's `workspace_isolation` and `namespace_isolation` observations | Passed for two workspaces and simultaneous active/shadow namespaces; normal retrieval excludes foreign, shadow and stale artifacts. |
| AC6 | `docs/qualification/issue-364-projection-recovery-report.json`; the physical report's `rebuild_recovery_validation_diagnostics` and `concurrency_failure_cleanup` observations | Deterministic PostgreSQL publication/recovery and live physical failure/rebuild checks pass separately. The required composed live activation, deletion propagation, stale-generation CAS and successor rollback evidence is absent, so R6 remains blocked. |
| AC7 | `docs/qualification/issue-212-cognee-production-report.json`; coherent run `issue507-resume-001`; `tests/integration/cognee/test_production_profile.py::test_production_qualification_records_one_coherent_run` | Failed closed: all ten gates were observed, nine passed, canonical receipt mapping failed with safe code `configuration_mismatch`, decision is `no_go`, and production approval is false. The report privacy scan passed. |
| AC8 | Closed report schemas, the qualification report scan, and `tests/qualification/test_issue507_qualification.py::test_issue507_traceability_names_exact_runtime_without_sensitive_values` | Passed: backend/model versions and bounded reason codes are retained; credentials, endpoints, filenames, document content and raw provider payloads are absent. |

## Reproduction

Run the deterministic ingestion, lifecycle and recovery evidence through their
existing #194 and #364 qualification suites. For the physical result, provision
the exact disposable profile from
`docs/runbooks/cognee-production-qualification.md`, set a new bounded run ID and
report path, and execute the opt-in production-profile target. Issues #514,
#515 and #516 are integrated. The latest report still fails the exact
`canonical_lifecycle_separation` gate because the production receipt builder
cannot map the observed graph locator to its persisted parsed segment. A narrow
adapter follow-up must resolve this without weakening evidence checks. Every ID
returned by `required_gate_ids()` must be present and `passed` in one fresh run.
R4 still requires an authorized deployed five-format upload/lifecycle record,
and R6 requires a composed live canonical publication/recovery exercise.

The live gate resolves real canonical PostgreSQL state and indexes only
synthetic content. PostgreSQL remains authoritative for revisions, boundaries,
configuration, candidates and publication. Cognee remains a replaceable
physical projection. The current NO-GO does not transfer canonical authority or
approve this or any other backend/model profile.
