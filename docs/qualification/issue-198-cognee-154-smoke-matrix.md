# Issue #198: Cognee 1.5.4 public SDK smoke matrix

This qualification is deliberately bounded to the public `cognee` package
surface. It does not import `cognee.*` implementation modules and it does not
turn Cognee's graph/vector projection into Spine's source of truth.

## Offline API-shape matrix

`spine.memory.cognee.qualification.build_api_matrix(sdk)` checks the audited
version and the public callable signatures used by the ContextProjection seam:

| Operation | Public entrypoint | Required shape checked | Result |
| --- | --- | --- | --- |
| Add | `cognee.add` | `data`, `dataset_name`, `node_set` | supported by 1.5.4 |
| Build | `cognee.cognify` | `datasets`, `dry_run`, `raise_on_error` | supported by 1.5.4 |
| Update | `cognee.update` | `data_id`, `data`, `dataset_id`, `chunk_level_diff` | supported by 1.5.4 |
| Delete | `cognee.datasets.delete_data` | `dataset_id`, `data_id`, `mode` | supported by 1.5.4 |
| Legacy delete | `cognee.delete` | `data_id`, `dataset_id`, `mode` | public but deprecated |
| Forget | `cognee.forget` | `data_id`, `dataset`, `dataset_id`, `everything`, `memory_only` | supported by 1.5.4 |
| Structured search | `cognee.search` | explicit query type/scope, `verbose`, `include_references` | supported by 1.5.4 |

An unknown version, missing public callable, uninspectable signature or missing
parameter produces a visible `no-go` result. The offline tests use a public-shape
fake, so no LLM, database or network is required.

The important operational failure modes are recorded as follows:

| Operation | Failure mode to surface | Qualification implication |
| --- | --- | --- |
| `add` / `cognify` | missing local stores, provider configuration or LLM | live profile is not run and no capability is claimed |
| `update` | unknown `data_id`, permission failure, or destructive full-rebuild fallback | preserve the returned failure; do not treat a partial update as success |
| `datasets.delete_data` | inaccessible dataset or absent data | repeat-delete must return a safe idempotent result in the live profile |
| `forget` | invalid target combination or failed projection cleanup | keep raw-vs-memory-only semantics explicit; cleanup failure is visible |
| `search` | missing structured objects/references or changed result shape | `inspect_search_payload` returns `no-go`, never a completion-only pass |

Cognee's `update` and `forget` behavior is therefore a physical observation,
not a substitute for Spine's immutable Source Revision, Projection Snapshot or
publication lifecycle.

## Structured search lanes

The live runner calls `cognee.search(..., verbose=True,
include_references=True)` and records only shape metadata, not source content:

- `SearchType.CHUNKS` must produce chunk objects and `segment` references;
- `SearchType.GRAPH_COMPLETION` must produce graph relation objects and a
  `graph_edge` reference;
- `SearchType.HYBRID_COMPLETION` must expose `chunks`, `entities` and
  `facts`/`relations`/`triplets`, with segment, graph-node and graph-edge
  references.

Every call uses the server-selected dataset name and forwards the configured
`node_set` to `add`; the report records both values. `node_set` is treated as a
graph taxonomy only, not as an authorization boundary. An empty result or a
result missing required structured objects/references is a `no-go` outcome.

The qualification observes these JSON fields at the public serialized result
boundary. It does not accept a Cognee completion string as a structured Spine
answer.

## Physical profiles and decision

| Profile | Relational | Graph | Vector | Decision |
| --- | --- | --- | --- | --- |
| Local capability | SQLite | Ladybug/Kuzu | LanceDB | capability-only; useful for opt-in local smoke |
| Production candidate | PostgreSQL | Neo4j | PGVector | not approved by this ticket |
| `postgres_demo` | PostgreSQL | `postgres_demo` | PGVector | rejected for production |

The local profile proves SDK capability only. Production approval remains
blocked on workspace isolation, provenance round-trip, deletion, recovery and
retrieval gates. `postgres_demo` is explicitly rejected as a production graph
backend.

The exact dependency pin is version-sensitive: the matrix is audited only for
Cognee `1.5.4`. A different version, even if import-compatible, is a visible
`no-go` until the upgrade gates in `docs/cognee-upgrade-policy.md` are rerun.

## Commands

The offline contract is part of the normal suite:

```text
python -m pytest -q tests/memory/test_cognee_qualification.py
```

The physical local profile is opt-in and requires configured Cognee storage plus
an LLM/embedding provider:

```text
set SPINE_COGNEE_LIVE=1
python -m pytest -q tests/integration/cognee/test_public_sdk_smoke.py
```

It uses a unique dataset and cleans it through the public `forget` API. The
test is skipped unless `SPINE_COGNEE_LIVE=1`; it is never an offline CI gate.

## Verification record

- Offline qualification: 6 passed.
- Opt-in physical test: present and skipped without live prerequisites.
- Public API incompatibility: represented as a fail-closed `no-go` report.
- Production backend approval: intentionally not granted.
