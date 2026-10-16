# Issue #359 R1 regression gate

`spine-eval-gate` is the single R1 gate entry point and artifact source. It
emits `r1-gate-status:v1`, retaining the existing two-pass synthetic offline
checks and adding applicability, metric comparison, qualification evidence,
repository pytest/PostgreSQL status, and opt-in provider profile status.

The mandatory deterministic offline run is provider-free and runs on every
invocation. The report includes G01 production-contract seam evidence, G02
unauthorized-retrieval safety, G03 grounded-answer safety, G06 adapter parity,
G07 applicability, and G08 baseline comparison. Parser/revision evidence from
#194 and projection lifecycle evidence from #90 and #211 remain attached through
their versioned qualification evidence IDs.

Cognee and LLM profiles default to `skipped`. Selecting either profile through
`--provider-profile` records `unavailable` until an executor is configured, so
the aggregate status becomes `not-qualified`; no offline result can claim
provider qualification. A missing mandatory repository test result also
produces `not-qualified`. A failed offline or repository gate returns a non-zero
exit code.

The CI `r1-regression-gate` job runs the full repository pytest suite, including
mandatory PostgreSQL acceptance tests, then invokes the same CLI with the suite
outcome and uploads `r1-gate-status.json`. A PostgreSQL prerequisite failure is
recorded as `unavailable` and cannot qualify the aggregate gate.

The comparison selects the checked-in synthetic baseline at
`docs/qualification/r1-baseline-report.json`. It records versioned metric deltas
and failure clusters while the offline gate's deterministic safety assertions
remain authoritative. PostgreSQL prerequisite absence (missing test URL,
unavailable Testcontainers, or container startup failure) is recorded as
`unavailable`; test failures remain `failed`.
This comparison does not define product quality thresholds; those remain owned
by #17. Real Cognee and LLM production profiles are not selected by CI.

## Executable evidence

| Requirement | Evidence |
| --- | --- |
| Existing offline contract remains reproducible and provider-free | `tests/evals/test_regression_gate.py::test_offline_regression_gate_passes_reproducible_r1_contract`; `tests/evals/test_regression_gate.py::test_offline_regression_gate_default_is_provider_free` |
| Versioned applicability, comparison, source/revision and projection evidence are included | `tests/evals/test_regression_gate.py::test_offline_regression_gate_passes_reproducible_r1_contract` |
| Applicability evidence with a changed version pin is rejected | `tests/evals/test_regression_gate.py::test_stale_applicability_manifest_evidence_is_not_qualified` |
| Missing repository/PostgreSQL result cannot claim qualification | `tests/evals/test_regression_gate.py::test_r1_gate_is_not_qualified_without_repository_pytest_evidence` |
| PostgreSQL prerequisite absence is separated from mixed test failures | `tests/qualification/test_issue68_qualification.py::test_issue68_gate_runner_marks_only_postgresql_prerequisite_errors_unavailable` |
| Selected unavailable providers are not reported as passed | `tests/evals/test_regression_gate.py::test_selected_unavailable_provider_profile_cannot_be_reported_as_passed` |
| The CLI writes the unified report | `tests/evals/test_regression_gate.py::test_offline_regression_gate_cli_writes_machine_readable_artifact` |
