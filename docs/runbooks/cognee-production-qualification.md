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

Neo4j Community does not support the per-dataset databases used by Cognee's
default Neo4j handler. For a disposable local qualification, build the pinned
Neo4j 5.26.32 Community image with its matching bundled APOC Core plugin:

```text
docker build --tag spine/neo4j-cognee:5-community --file docker/neo4j-cognee/Dockerfile .
```

The `neo4j_community` handler then provides physical dataset isolation by
creating one temporary container and volume per dataset. It requires access to
the Docker daemon. Production Neo4j Enterprise may instead use Cognee's
multi-database `neo4j` handler. The qualification image also protects the
official entrypoint from treating a generated password that begins with `-` as
a `neo4j-admin` option.

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
GRAPH_DATASET_DATABASE_HANDLER=neo4j_community
NEO4J_COMMUNITY_IMAGE=spine/neo4j-cognee:5-community
NEO4J_ENCRYPTION_KEY=... # dedicated qualification secret, never use test_key
NEO4J_COMMUNITY_MAX_CONTAINERS=4
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
Do not set `ENABLE_BACKEND_ACCESS_CONTROL=false`: a run without backend access
control is not production evidence.

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

After an interrupted Community run, inspect containers carrying label
`ai.cognee.neo4j_community=true` and remove only those qualification containers
and their `cognee-neo4j-data-*` volumes. A completed run removes its datasets
through Cognee's public `forget` API.

The orchestrated target continues through independent failures so the report
contains one observation for every required gate. The canonical-lifecycle gate
is intentionally emitted as `not_run` until a PostgreSQL-backed projection
request resolver composition exists and can be instrumented. The target fails
at the end if any gate failed or was not run; provider exception text is never
serialized.

## Interpreting the result

The current committed result is **no-go** from coherent live run
`issue212-docker-006`. The pinned PostgreSQL + Neo4j + PGVector profile failed
the public search evidence, structured provenance, isolation, update/remove and
concurrency retrieval gates. The rebuild/recovery/validation gate passed, but
one passing gate cannot grant production approval.

Do not convert a partial pass, skipped test, local Cognee profile or Issue #211
fake-backed result into production approval. A future `go` requires every gate
in `required_gate_ids()` to have a real-backend `passed` observation in one
coherent run, including two simultaneously present active/shadow datasets,
provenance, explicit snapshot/boundary/namespace context, tombstones,
rebuild/recovery, diagnostics, concurrency, failure injection and cleanup.

If the complete profile fails those gates, retain the no-go decision and open a
separately approved implementation ticket for the owned PostgreSQL projection
mapping fallback. PostgreSQL remains canonical; physical Cognee calls must stay
outside its UnitOfWork.
