# Cognee production profile qualification runbook

This runbook is for an isolated, disposable qualification environment. It does
not configure or modify a production deployment.

## Required profile

- Cognee 1.5.4 from `uv.lock`;
- PostgreSQL for Cognee relational state;
- Neo4j for graph state;
- PGVector for vector state;
- approved LLM and embedding resources required by the tested Cognee path;
- a dedicated database/schema and credentials that may be cleaned after the
  run.

Set Cognee's standard configuration variables for the exact profile:

```text
DB_PROVIDER=postgres
DB_HOST=...
DB_PORT=...
DB_NAME=...
DB_USERNAME=...
DB_PASSWORD=...
GRAPH_DATABASE_PROVIDER=neo4j
GRAPH_DATABASE_HOST=...
GRAPH_DATABASE_PORT=...
GRAPH_DATABASE_USERNAME=...
GRAPH_DATABASE_PASSWORD=...
VECTOR_DB_PROVIDER=pgvector
VECTOR_DB_HOST=...
VECTOR_DB_PORT=...
VECTOR_DB_NAME=...
VECTOR_DB_USERNAME=...
VECTOR_DB_PASSWORD=...
LLM_PROVIDER=...
LLM_MODEL=...
EMBEDDING_PROVIDER=...
EMBEDDING_MODEL=...
EMBEDDING_DIMENSIONS=...
SPINE_COGNEE_QUALIFICATION_RUN_ID=issue212-run-001
```

Supply provider keys/endpoints through the deployment secret mechanism. Do not
commit them or copy them into the qualification report.

`GRAPH_DATABASE_PROVIDER=postgres_demo` is forbidden. The preflight must fail
for that value; it is not an alternative way to satisfy the Neo4j requirement.

## Run

After installing the declared development dependencies, opt in explicitly:

```text
SPINE_COGNEE_PRODUCTION_QUALIFICATION=1 uv run pytest -q -m cognee_production tests/integration/cognee/test_production_profile.py
```

Set `SPINE_COGNEE_QUALIFICATION_REPORT` to an operator-controlled output path
to serialize the validated observations from that run. Do not point it at the
committed report until the complete gate inventory has been reviewed.

The target uses unique synthetic dataset names and public Cognee cleanup calls.
It contains no customer data. Retain content-free test results and backend
versions; do not retain raw provider errors in the report.

## Interpreting the result

The current committed result is **no-go** because no coherent live run has been
recorded. The complete `CogneeContextProjection` composition exists and runs
the shared contract suite offline, but only the real PostgreSQL + Neo4j +
PGVector profile can produce production evidence.

Do not convert a partial pass, skipped test, local Cognee profile or Issue #211
fake-backed result into production approval. A future `go` requires every gate
in `required_gate_ids()` to have a real-backend `passed` observation in one
coherent run, including isolation, provenance, explicit namespace context,
tombstones, rebuild/recovery, diagnostics, concurrency, failure injection and
cleanup.

If the complete profile fails those gates, retain the no-go decision and open a
separately approved implementation ticket for the owned PostgreSQL projection
mapping fallback. PostgreSQL remains canonical; physical Cognee calls must stay
outside its UnitOfWork.
