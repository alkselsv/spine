# AGENTS.md — Spine

This file defines repository-wide instructions for coding agents. More specific
`AGENTS.md` files in subdirectories may refine these rules.

## Product direction

Spine is a context-aware operational AI platform that combines AI agents, code
steps and people in observable business workflows. It is not a single chatbot
and must not be designed around a fixed, small set of agents.

Read these documents before making architectural or cross-module changes:

- `docs/ARCHITECTURE.md` — system boundaries, invariants and target model;
- `docs/ROADMAP.md` — delivery order, release scope and exit criteria;
- `docs/cases/` — source requirements for the product cases.

The product cases have a fixed delivery priority:

1. Q&A over internal documents;
2. commercial proposal generation;
3. customer communication monitoring;
4. internal task execution control.

Build vertical product slices in this order. Do not move platform generalization,
an external Agent SDK, a marketplace, process mining or a universal visual builder
ahead of the four cases unless the roadmap is explicitly changed.

## Current state

- `src/spine/api/main.py`, `src/spine/ingest/` and
  `src/spine/memory/cognee_memory.py` contain the current Q&A prototype.
- Keep these paths working while their responsibilities are moved behind the new
  contracts. Do not delete compatibility code without a tested replacement.
- `src/spine/ingestion/` and `src/spine/memory/cognee/` are the target locations
  for the ingestion pipeline and Cognee adapter.
- `web/` is an intentional product-interface scaffold, not optional documentation.

## Technology stack

Treat `pyproject.toml` and `uv.lock` as the source of truth for installed Python
packages. Add a package to `pyproject.toml` before importing it directly; a
transitive Cognee dependency is not part of Spine's supported application stack.

### Active stack

- Python `>=3.10,<3.15`, managed with `uv`; Hatchling builds the package from
  `src/spine`.
- Pydantic v2 and `pydantic-settings` define typed contracts and environment
  configuration.
- FastAPI and Uvicorn expose the current HTTP API; `httpx` exercises ASGI routes
  in tests.
- Cognee is the current graph/vector memory adapter. Import it lazily after
  `configure_environment()` has assigned absolute project-local storage paths.
- `pypdf`, `python-docx` and `openpyxl` load PDF, DOCX and XLSX sources. Markdown
  and text files use the standard library.
- Pytest and pytest-asyncio are the test runner. The repository currently has no
  separate JavaScript test toolchain.

### Target stack, add only when a vertical slice needs it

- PostgreSQL is the canonical store; SQLAlchemy and Alembic are the intended
  persistence and migration tools.
- Temporal Python SDK owns durable workflows, retries, signals and long waits.
- The product interface is planned around TypeScript, React, generated OpenAPI
  types, TanStack Query and SSE. `web/` is still a scaffold: select and record the
  concrete framework, package manager and test runner before adding frontend code.
- Object storage, telemetry backends and external connectors remain behind ports;
  choose implementations in the consuming roadmap slice, not from transitive
  dependencies or empty directories.

## Architecture boundaries

Dependencies point inward:

```text
web / api / workers
        ↓
application / workflows / agents / evaluations / ingestion
        ↓
domain
```

- `src/spine/domain/` contains business models and invariants. It must not import
  FastAPI, Cognee, Temporal, SQLAlchemy or concrete infrastructure adapters.
- `src/spine/application/` coordinates use cases through ports and domain types.
  Keep transport schemas and persistence details out of application policies.
- `src/spine/infrastructure/` implements persistence, object storage, outbox and
  telemetry ports.
- `src/spine/connectors/` owns external-system ingestion and action adapters.
  Preserve raw payloads and map them to versioned canonical objects.
- `src/spine/memory/` exposes the Context Broker contract. Cognee is a replaceable,
  rebuildable graph/vector projection, never the source of operational truth.
- `src/spine/agents/` owns Spine's framework-independent runtime protocol,
  registry-facing adapters, tools and governance. Do not add Agno as a dependency.
- `src/spine/workflows/` owns durable execution adapters. Workflow definitions
  depend on capabilities and typed artifacts, not concrete agent implementations.
- `src/spine/evaluations/` contains executable evaluators and release gates;
  `src/spine/domain/evaluations/` contains their pure domain models.
- `src/spine/evals/` is reserved for datasets, fixtures and evaluation runners.
- `src/spine/api/` is the public control-plane boundary. New endpoints belong
  under `/api/v1`; keep legacy endpoints only for compatibility.

Do not introduce a framework merely to fill an empty scaffold directory. Add a
dependency only when a roadmap item needs it and an adapter boundary is defined.

## Domain and data rules

- PostgreSQL is the intended canonical store for Spine-owned state.
- External systems remain authoritative for their own objects. Store an immutable
  `SourceRevision` and provenance before producing canonical projections.
- Facts, source observations and probabilistic model conclusions are different
  records. Never overwrite a fact with an LLM inference.
- Every tenant-owned record, command, event and retrieval request carries a
  `workspace_id`; environment-specific execution also carries an environment.
- Definitions and runtime configuration are immutable and explicitly versioned.
- Prefer stable domain types over unstructured dictionaries at module boundaries.
  Use dictionaries only for intentionally extensible, versioned payloads.
- Any conclusion that can affect a person, customer, price or external action must
  retain evidence references and upstream artifact lineage.
- Ingestion, notifications and external actions are at-least-once and must be
  idempotent. Do not rely on exactly-once delivery.
- Deletions and source changes retain history through revisions or tombstones.
- Money uses decimal arithmetic plus explicit currency, tax and rounding rules.
  Never delegate financial arithmetic to an LLM.

## Agents, workflows and evaluations

- A capability is a stable business contract. An agent is one versioned
  implementation of one or more capabilities.
- Agent invocation and result types remain independent of the agent's internal
  library, model provider or deployment mechanism.
- Pass typed artifacts between steps. Do not use chat transcripts as workflow
  state or as the only handoff contract.
- Human steps are first-class workflow nodes with typed input/output, ownership,
  deadlines and audit history.
- Mutating or externally visible actions go through policy checks and, when
  required, explicit approval.
- Long-running state belongs to the durable workflow layer; do not hide durable
  loops, retries or approval waits inside an agent implementation.
- Add an evaluation with each material prompt, model, retrieval, detector or
  capability change. LLM-as-a-judge may supplement but not replace deterministic
  checks and human-labelled examples.
- Detectors require versioning, deduplication, historical replay and a feedback
  path before they can be enabled in production.

## Cognee and retrieval

- Access Cognee through the Context Broker or its adapter, not directly from API,
  domain or workflow code.
- Retrieval results are structured: chunks, entities, relations, references,
  strategy and index version. Do not collapse them to an untraceable answer string.
- Enforce access at retrieval time. Dataset or `node_set` selection alone is not
  sufficient authorization.
- Citations use stable locators such as page, sheet/row, message ID or character
  range and point to a specific source revision.
- Support abstention when evidence is insufficient or inaccessible.
- Any projection format change must define rebuild, rollback and regression-eval
  procedures.

## API and interface

- The interface ships with each vertical slice. A backend-only feature is not a
  completed product case.
- Generate frontend API types from OpenAPI. Do not hand-edit files under
  `web/api/generated/`.
- The server enforces RBAC/ABAC; hiding a control in the UI is not authorization.
- Preserve workspace and environment in route/request context.
- UI states must cover loading, empty, partial, stale, degraded,
  permission-denied and terminal failures.
- Do not expose hidden chain-of-thought. Show evidence, structured summaries,
  decisions, versions and evaluation results.
- Q&A requires a citation inspector and feedback. Proposal generation requires a
  structured editor and diff. Monitoring cases require a Findings Inbox and
  evidence timeline.

## Python conventions

- Target Python versions are defined in `pyproject.toml`; do not narrow them
  without a demonstrated dependency constraint.
- Use type hints on public functions and domain boundaries.
- Use Pydantic models for versioned external/domain contracts. Preserve
  `extra="forbid"`; configuration definitions should normally remain frozen.
- Keep I/O async from API through adapters where the underlying operation is async.
- Prefer small modules with explicit ownership over generic `utils.py` files.
- Avoid importing optional/heavy integrations at module import time when a lazy
  adapter import keeps basic commands and tests usable.
- Keep code identifiers and API fields in English. User-facing product copy and
  repository documentation may be in Russian.
- Never commit credentials, production payloads, customer correspondence or
  unredacted evaluation examples. Use synthetic or anonymized fixtures.

## Test design

Every behavior change carries a test at the lowest boundary that proves it. A bug
fix starts with a test that fails for the reported behavior and passes after the
fix. Tests assert public contracts and domain outcomes, not private call order or
the internal shape of an implementation.

### Test shape

- Name tests `test_<condition>_<outcome>` and keep one behavioral reason for
  failure per test. Parameterize examples that exercise the same rule.
- Use explicit factories for valid domain objects, then override only fields
  relevant to the scenario. Keep expected values visible in the test.
- Make tests deterministic. Inject clocks, identifiers, model clients and external
  adapters; do not depend on wall-clock time, random UUIDs, network availability or
  test execution order.
- Use `tmp_path` for files and databases. A test must not write to repository
  fixtures, `.spine/`, a developer database or another test's directory.
- Prefer a small fake that implements a port over patching internals of FastAPI,
  Cognee, Temporal, SQLAlchemy or an SDK. Assert recorded commands/events when the
  interaction itself is the contract.
- Keep committed fixtures synthetic or anonymized, minimal and immutable. Record
  the fixture schema/version and source assumptions beside replay or evaluation
  datasets.

### Test levels

- **Domain unit tests:** pure rules and invariants; no filesystem, network,
  database, FastAPI, Cognee, Temporal or concrete adapter imports.
- **Application tests:** execute a use case through ports with in-memory fakes;
  cover success, rejection, partial failure and retry policy.
- **Contract tests:** run the same behavior suite against every implementation of
  a port. Cover schema validation and mapping at connectors, tools, memory and API
  boundaries.
- **Integration tests:** use real serialization, temporary SQLite/PostgreSQL,
  Cognee projection or another adapter boundary. Keep external-service tests
  opt-in and skip them with a clear missing-prerequisite reason.
- **Replay and evaluation tests:** pin dataset, prompt/model, detector, projection
  and evaluator versions. A material retrieval, prompt, model or detector change
  updates or adds cases and reports metric deltas.
- **Interface tests:** when the web toolchain exists, cover component states and
  the critical user path. Generated OpenAPI files are verified by regeneration,
  not hand-written tests of generated implementation details.

### Required scenarios

- For workspace-owned behavior, prove allowed access, denied access and isolation
  between two workspaces. A denied result must not leak protected text in output,
  logs or errors.
- For an idempotent operation, repeat the same command/key and retry after a
  partial failure; assert one logical effect and a stable result.
- For revisions and projections, cover current, stale, deleted/tombstoned,
  rebuild failure and rollback to the last active version.
- For evidence-bearing output, reject missing, inaccessible, stale or fabricated
  references. Cover abstention when usable evidence is absent.
- For money, assert exact `Decimal` values, currency, tax order and rounding at
  boundary cases. Never use approximate float assertions for financial results.
- For FastAPI, call the app in-process with `httpx.ASGITransport`; cover response
  schema, validation errors, authorization, structured failures and trace/workspace
  context without binding a network port.
- For durable workflows, keep workflow code deterministic and test it with
  time-skipping plus fake activities. Cover retry exhaustion, cancellation,
  signals, queries and idempotent activity re-execution.
- For Cognee or an LLM, the default suite uses a port fake. A real-provider test
  is separate, opt-in and never required for the offline suite.

## Verification gates

During development run the narrowest affected test first. Before handoff install
the declared development dependencies and run the repository gates:

```bash
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

Completion means the new test is present, the full suite passes, and
`test_domain_layer_has_no_framework_imports` still proves the domain boundary. If
an external integration could not be exercised, its fake/contract suite must pass
and the handoff must name the exact unverified integration and prerequisite.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the standard mattpocock/skills triage labels. See
`docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository: use root `CONTEXT.md` and system-wide ADRs
under `docs/adr/`. See `docs/agents/domain.md`.

## Change discipline

- Preserve unrelated user changes and avoid broad mechanical rewrites.
- Before adding a new abstraction, identify the current or next roadmap case that
  consumes it. Prefer the smallest contract that supports that vertical slice.
- Update `docs/ROADMAP.md` checkboxes only when the implementation and its exit
  criteria are actually verified.
- Update `docs/ARCHITECTURE.md` when changing a system boundary or invariant.
- Add or update an ADR for canonical storage, tenancy, orchestration, capability
  resolution, artifact/handoff contracts, delegated authority or another decision
  listed in the architecture's ADR section.
- Keep README commands and project-tree descriptions accurate after moves.

## Definition of done

A change is complete when:

1. It serves the current prioritized case or an explicitly approved prerequisite.
2. Domain and adapter boundaries remain intact.
3. Tenant access, provenance, versioning and idempotency implications are handled.
4. API and UI contracts are aligned where the feature is user-facing.
5. Relevant automated tests and evaluations pass.
6. Operational failure, retry, rollback/rebuild and observability paths are clear.
7. Documentation and roadmap status reflect reality.
