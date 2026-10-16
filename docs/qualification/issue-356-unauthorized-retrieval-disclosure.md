# Issue #356 unauthorized retrieval and evidence disclosure

Status: **qualified offline against deterministic application-flow observations;
real Cognee, LLM and production-provider qualification is not claimed**.

The gate uses the G01 `r1-production-contract-observation:v1` seam. It validates
only typed values emitted by the executed application flow and records a
disclosure-safe `ProductionContractSafetyResult`; it does not create a second
authorization or retrieval implementation.

## Safety matrix

The deterministic matrix covers:

- an authorized current bundle with exact SourceRevision, locator, provenance,
  policy-generation and projection-pin identity;
- revoked access and stale access-generation rejection;
- mixed authorized/unauthorized evidence and cross-workspace physical reads;
- provider output before authorization;
- invalid, stale, tombstoned, superseded or fabricated evidence lineage;
- source existence/count disclosure on denied outcomes.

The versioned case identifiers are `authorized-current-evidence`,
`revoked-access`, `stale-access-generation`,
`mixed-authorized-unauthorized-evidence`, `cross-tenant-physical-lookup`,
`provider-output-before-authorization`, `invalid-evidence-lineage` and
`unauthorized-source-existence-disclosure`.

Rejected observations contain no bundle, retrieval references, projection data,
accepted answer or protected diagnostic. Safety failures are recorded separately
from quality evaluators and fail the production-contract run independently of
quality metrics. Reports contain only case identity, bounded violation codes and
boolean disclosure status; protected text is never copied into a failure.

## Verification

The focused suite is:

```text
uv run pytest -q tests/evals/test_unauthorized_retrieval_safety.py tests/evals/test_production_contract.py
uv run python -m compileall -q src tests
```

The mandatory offline gate remains deterministic and provider-credential-free.
Live Cognee, LLM and PostgreSQL qualification are outside this ticket.
