# Issue #16 — R0/R1 end-to-end release qualification

Status: implementation-ready qualification specification; not a release approval.
This document is a local draft for review and later ticket decomposition. It has
not been published to GitHub, and Issue #16 has not been changed.

## Problem Statement

Spine has implemented the contracts and many component-level paths for its R0
foundation and R1 document Q&A. Those results do not yet establish that an
administrator can use one real, production-capable single-deployment profile to
upload corporate documents, ingest canonical revisions, publish an authorized
Context Projection, ask a question through the Web UI, and inspect a grounded
answer or safe terminal outcome with its evidence and diagnostics.

Several key release boundaries remain unverified: the approved Cognee physical
profile has an explicit NO-GO report, the configured deployment and trusted
runtime bindings have not been qualified, the real Golden Dataset has not run
through the production application flow, numerical product thresholds lack
Product Owner approval, and backup/restore exercises are not production
evidence. Existing synthetic tests, fake adapters, closed issues and
documentation cannot substitute for that evidence.

The user-facing administrator journey must operate through the real Web UI and
API without CLI actions. CLI and runbooks may prepare the environment, migrate
the database, recover a deployment, or execute the locally authorized
benchmark.

## Solution

Qualify the already selected contracts as one deployed path. Reuse canonical
PostgreSQL persistence, SourceObject/SourceRevision lifecycle, original storage,
transactional outbox, Temporal, the production AnswerQuestionCapability, the
authorized Context Broker, Context Projection, the existing disclosure gate,
the `/api/v1` Control Plane, and the administrator UI. Add implementation only
when an observed integrated gate identifies a concrete missing behavior; do
not rebuild capabilities that already satisfy their contracts.

The release record must be reproducible, correlate all evidence by deployment,
workspace, environment, run and version identities, and contain metadata only.
Production approval is possible only after every mandatory gate is PASS and the
Product Owner has explicitly approved the active numerical quality policy.

## User Stories

1. As a workspace administrator, I want to upload PDF, DOCX, XLSX, Markdown and
   TXT documents in the browser, so that I can prepare the corpus without CLI.
2. As a workspace administrator, I want duplicate uploads and retries to be
   idempotent, so that network retries do not create duplicate logical sources.
3. As a workspace administrator, I want document changes and deletions to
   create immutable revisions or tombstones, so that history and provenance are
   retained.
4. As a workspace administrator, I want ingestion progress and partial failures
   visible in the Knowledge UI, so that I can identify and retry failed files.
5. As a workspace administrator, I want to see when the exact current revisions
   are accepted and included in a healthy active projection, so that I know
   whether Q&A can use them.
6. As a workspace administrator, I want failed shadow builds and recovery
   attempts to preserve the current active publication, so that a rebuild cannot
   silently damage the available corpus.
7. As an authorized administrator, I want to ask questions through the Chat UI,
   so that Q&A uses the same durable governed application path as production.
8. As an authorized administrator, I want each answer to cite accessible
   current revisions with stable locators, so that I can inspect its evidence.
9. As an administrator, I want insufficient, stale or incomplete evidence to
   produce abstention or clarification, so that unsupported answers are not
   disclosed.
10. As a workspace administrator, I want denied content hidden from answers,
    citations, diagnostics and reports, so that authorization applies at both
    retrieval and disclosure.
11. As an administrator, I want a run to survive browser reconnect and expose a
    single terminal result, so that a dropped SSE connection does not create a
    second answer or lose the original run.
12. As an administrator, I want to cancel an admitted Q&A run and see its
    terminal cancellation, so that the durable workflow remains observable.
13. As an administrator, I want to provide feedback on an answer, so that the
    approved feedback contract can be exercised and correlated with its run.
14. As an operator, I want trace-based diagnosis to show safe, bounded failure
    categories and timing, so that I can troubleshoot without exposing private
    content.
15. As an operator, I want to back up PostgreSQL and original storage as one
    coherent recovery point, so that restored canonical references still resolve
    to their originals.
16. As an operator, I want to restore into an isolated target and reconcile it
    before service, so that recovery does not trust incomplete or stale evidence.
17. As a Product Owner, I want a real approved-dataset baseline with its
    denominators and coverage, so that release thresholds reflect observed use.
18. As a Product Owner, I want to explicitly approve versioned numerical
    thresholds after reviewing the baseline, so that an executable gate uses an
    authorized release policy.
19. As a release owner, I want a metadata-only GO/NO-GO report, so that the
    decision is auditable and does not disclose protected corpus content.
20. As a maintainer, I want legacy `/ask`, `/ingest` and CLI paths either tested
    as compatibility behavior or replaced with approved migration coverage, so
    that users are not silently broken.

## Implementation Decisions

### Scope and non-goals

In scope: qualification of the current R0/R1 implementation in one
production-capable single-deployment profile; documented runtime configuration
and operator preparation; composition verification; physical-provider and
application-flow exercises; a real browser journey; backup/isolated restore;
the approved real dataset and release thresholds; metadata-only evidence; and a
final release decision.

Out of scope: implementing existing domain and transport contracts again;
creating identities, grants, source mappings or data permissions to manufacture
readiness; changing production credentials or AccessPolicy; weakening safety
invariants; selecting a new cloud/H/A platform; adding multi-region or
autoscaling; changing Cognee/provider architecture without a separate decision;
running a private benchmark in this specification task; publishing this spec;
creating issues; changing Issue #16; or starting implementation before
`$to-tickets` and a valid GitHub claim.

### Current-state evidence matrix

`implemented`, `integrated` and `qualified` follow the scope-specific
definitions in `docs/ROADMAP.md`: implemented means tested at the component
boundary; integrated means connected to the target release user path rather than
only a fake, prototype or compatibility path; qualified means acceptance
evidence exists and the named boundary's required gates passed. Qualification
at one boundary does not qualify a production provider or the release.

| Capability | Implemented | Integrated | Qualified | Real evidence currently available | Remaining gap | Owning issue(s) |
|---|---|---|---|---|---|---|
| PostgreSQL persistence, migrations, tenancy and outbox | Yes; contracts and persistence adapters | Yes, in API/worker composition boundaries | Yes, repository and disposable PostgreSQL/Temporal boundaries | Migration and PostgreSQL integration tests in `tests/infrastructure/db/`, `tests/integration/postgresql/`, application persistence tests; release-specific live deployment report absent | Run required migration graph and gates against the declared release profile; record deployment identity and actual service reachability | #8, #4, #16 |
| Original document storage and recovery | Yes; filesystem adapter, backup fence, manifests, reconciliation and isolated restore contracts | Partly; upload and restore paths use the contracts | Contract and deterministic qualification exist; deployment backup/restore not qualified | `tests/application/storage/`, `tests/infrastructure/test_backup_artifacts.py`, #368 runbook records; #368 explicitly records production qualification false and several exercises not run | Execute coordinated PostgreSQL/original-storage backup and isolated restore against designated targets, then reconcile | #6, #18 / #362 / #363 / #368 / #369, #16 |
| Canonical ingestion and five supported formats | Yes; parser, revision, checksum, locator, retry and lifecycle contracts | Yes; supported local PostgreSQL/filesystem profile and Knowledge resources | Yes for supported local profile; not full deployment | #154/#194 qualification records and application/integration tests for PDF/DOCX/XLSX/MD/TXT | Exercise upload, update, delete, partial failure and retry through the actual deployed UI/API and retain safe run evidence | #3 and ingestion children, #16 |
| Context Projection lifecycle | Yes; immutable snapshots, shadow build, activation/CAS, failed rebuild, recovery and successor rollback | Yes at canonical request, worker and publication boundaries | Deterministic/offline qualification exists; physical profile is not qualified | #7 lifecycle specification, #90 qualification, #364 recovery evidence; #212 actual Cognee run passed 9 physical gates but overall NO-GO | Run current canonical lifecycle resolver with real Cognee provider; verify current/shadow membership, physical provenance, deletion, recovery and rollback; re-evaluate #212's unrun gate | #7, #15, #212, #16 |
| Cognee/Neo4j/PGVector profile | Yes; adapter and bounded live qualification runner | Physical libraries were composed in the isolated #212 qualification run | **No**; report is explicit NO-GO | `issue212-docker-013`: Cognee 1.5.4, disposable PostgreSQL 17/PGVector 0.8.7, Neo4j 5.26.32 and operator-supplied LLM/embedding; public SDK, structured data, evidence, namespace isolation, update/delete, rebuild and failure gates passed | `canonical_lifecycle_separation` is `not_run`; production approval false. Repeat after the production canonical composition can be observed. If it still fails, choose a separately reviewed provider/profile or owned mapping solution; fallback is only a recommendation | #212, #15, #16 |
| Workspace isolation, identity and AccessPolicy | Yes; trusted context, policy, human/service intersection and retrieval/disclosure checks | Yes in governed API and Q&A contracts | Contracts and PostgreSQL tests qualify named boundaries; production identities/policy bindings do not | ADR 0011, 0021; authorization and persistence tests | Verify existing trusted principal, workspace/environment, grants, policy generation and revocation in the chosen runtime. Do not provision or infer authority during readiness | #5, #27, #16 |
| `answer_question` grounded response and disclosure | Yes; capability contract, bundle validation, citations, abstention and fail-closed gate | Yes at deterministic/provider-shaped and disposable PostgreSQL boundaries | Offline and disposable PostgreSQL contracts qualify; live provider and deployed composition do not | #25/#285/#357 qualification records; ADR 0033/0034; application tests for citation/disclosure/revalidation | Exercise the production capability against the active physical projection and current policy; prove grounded answer, abstention and denial with protected content absent | #25, #27, #469, #16 |
| Durable Ask, cancellation and SSE | Yes; PostgreSQL admission/outbox, Temporal workflow, idempotency, cancellation, terminal state and resumable event contracts | API and worker composition exists in code | ASGI, PostgreSQL and Temporal test boundaries pass; deployed service path not qualified | #26 and #430 deterministic/integration evidence; ADR 0035/0038/0040 | Verify one deployed API/outbox/Temporal path, terminal correlation, reconnect, cancellation and retry/replay behavior in the actual runtime | #26, #469–#478, #16 |
| Production Q&A composition and observer handoff | Yes; production capability and authenticated capture contracts were implemented; #469 and #478 are closed | Code composition points are present; not yet evidenced as one configured deployment | Fake/contract qualification only for release purposes; real runtime qualification absent | `ProductionAnswerQuestionCapability`, composition evidence types, #430 and #453 contracts; ADR 0040 | Prove API, Temporal worker and observer share the same production composition and same execution capture; current runbook says no local production qualification factory was found | #469–#480, #453, #16 |
| API and web client | Yes; generated OpenAPI API, routes, client, sessions, feedback, run and diagnostics resources | Yes within app and controlled API paths | In-process ASGI/client contracts qualify; not an actual deployed application | API tests, generated OpenAPI checks, qualification report #305 | Verify deployed authentication, workspace/environment routing and real service composition; no endpoint fake may stand in for browser acceptance | #22, #23, #26, #16 |
| Administrator Knowledge/Chat/Runs UI | Yes; document, chat, citations, feedback, runs and diagnostics screens | Yes against controlled/synthetic API | Component and controlled Playwright/API tests qualify those boundaries; production browser path not qualified | `web/` tests and #338/#422 records; controlled browser evidence is explicitly not production evidence | Run Playwright through a real browser against the deployed API, worker, PostgreSQL, Temporal and physical provider; capture all required terminal and recovery UI states | #22/#23/#26, #16 |
| Synthetic regression and golden dataset | Yes; synthetic evaluator and real-dataset mapping/preparation/evaluation contracts | Synthetic gate integrated in repository qualification; real dataset has not reached production flow | Synthetic deterministic gate qualifies offline; real evaluation is `not_run` | `issue-359-r1-regression-gate.md`; dataset contract records version `0.3.9-user-approved`, digest `7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62`, 30 docs, 149 cases (136 answered, 13 abstained, 0 denied) | #453 says `not_exercised`; complete trusted preflight, run only locally through the real flow, preserve zero-denial as `not_applicable` | #19, #20, #443–#453, #501, #16 |
| Baseline aggregates and executable thresholds | Yes; #501 aggregates and #502 pure versioned threshold gate are closed/merged | Integrated with benchmark evidence contracts, not with actual real observations | Deterministic policy behavior qualified; no real baseline and no approved numerical policy | `issue-502-versioned-threshold-release-gate.md` explicitly says no Product Owner values approved; #501 contract says real execution remains operator opt-in | Obtain real aggregates after preflight, review metric denominators/coverage, and get separate explicit Product Owner approval for actual numerical thresholds | #17, #501, #502, #16 |
| Diagnostics, audit and operational metrics | Yes; structured errors, safe telemetry, traces, audit and correlation models | API, outbox, worker, projection and Q&A contracts share trace/correlation | Offline and deterministic read-model qualification exists; live privacy behavior in deployed journey absent | ADR 0020/0036/0037, #68/#369 records, tests in diagnostics/telemetry | Confirm trace lineage from upload/request through Temporal/provider/UI; inspect safe errors and report output under injected failures without relying on log silence | #4, #18, #16 |
| Legacy compatibility | Yes; `/ask`, `/ingest` and CLI prototype remain in repository | Kept as compatibility path; not integrated into governed R1 | Local compatibility path exists; migration/retirement qualification for this release not evidenced | README documents compatibility path; no tested replacement/retirement decision attached to #16 | Exercise documented compatibility surface or approve and test a replacement before retirement | #16 |

### Architecture and integration boundaries

The single target path is:

```text
Administrator browser
  → authenticated /api/v1
  → canonical PostgreSQL SourceObject / SourceRevision and upload metadata
  → original storage and idempotent ingestion
  → transactional outbox → Temporal workers
  → canonical projection request resolver and immutable candidate/snapshot
  → physical Context Projection (approved Cognee profile)
  → PostgreSQL validation, activation generation and active publication
  → trusted request context + AccessPolicy
  → authorized Context Broker → immutable ContextBundle
  → production AnswerQuestionCapability → disclosure gate
  → persisted terminal result, resumable SSE and protected citation inspection
  → browser feedback, Runs/trace diagnostics
```

PostgreSQL owns sources, revisions, policies, requests, runs, snapshots,
activation, audit and recovery records. Original storage retains immutable
uploaded bytes. Cognee/Neo4j/PGVector is replaceable physical projection state,
never source lifecycle, publication authority or AccessPolicy. Temporal owns
durable retry, cancellation and terminal arbitration; no user flow bypasses the
transactional outbox or invokes an alternate Q&A pipeline. Observation and
benchmark capture the same in-flight authorized execution and never retrieve,
generate, authorize or disclose a second time.

Only server-composed trusted runtime context can establish the actor, workspace,
environment and policy scope. UI selection and environment variables can select
configuration but cannot grant authority. Check authorization at retrieval and
again at every protected citation/history/SSE disclosure boundary. Preserve
answered, abstained, clarification-required, denied, failed, cancelled and
unavailable as distinct safe terminal states. Do not return candidate answer
text for rejected or non-answer outcomes.

All source revisions and projection snapshots remain immutable/versioned.
Activation uses current canonical state and generation/CAS; rollback publishes
a valid successor and never rewrites a snapshot or pointer history. Failed
physical projection or backup recovery cannot mutate canonical state or make a
shadow snapshot ordinarily retrievable.

### User journey to qualify

1. Operator prepares the declared single-deployment profile through the
   operator runbook: secrets/configuration, supported migrations, services and
   trusted auth. Readiness checks report facts without creating principals,
   grants, permissions or source mappings.
2. Administrator signs in through the configured trusted authentication path
   and enters an explicitly authorized workspace/environment.
3. Administrator uploads representative files in each supported format in the
   Knowledge UI; observes acceptance, partial failures and retry results.
4. The canonical lifecycle stores original bytes, checksum, immutable
   SourceRevision and stable locators; outbox/workers process idempotently.
5. The real projection provider builds a shadow candidate; validation verifies
   lineage and physical artifacts; PostgreSQL activates the exact current
   revisions. Administrator sees healthy active readiness.
6. Administrator asks an answerable question in Chat. Durable Ask admission,
   outbox and Temporal execute the one production Q&A composition. Browser
   reconnect resumes the same run and terminal result.
7. The accepted answer has citations that resolve through the inspector to
   accessible exact revisions and locators. Feedback is stored against that
   message/run.
8. Administrator asks an unanswerable question and sees abstention or
   clarification with no fabricated citation. A denied and cross-workspace
   question discloses no protected content.
9. Administrator inspects safe run timeline, trace correlation, versions,
   retrieval outcome and structured diagnosis without CLI or hidden
   chain-of-thought.
10. Operator runs approved backup, isolated restore and reconciliation, then
    demonstrates the restored deployment preserves canonical revisions and
    can recover/rebuild the projection before re-entering service.

### Qualification directions and proposed ticket decomposition

The nine proposed Q16 directions are qualification activities unless a concrete
missing behavior is demonstrated. They must not become nine implementation
projects by default.

| Direction | Nature and independently checkable outcome | Parallelism / real blockers | New code or reuse | Deterministic vs real-only evidence | Decision |
|---|---|---|---|---|---|
| Q16-01 deployment/runtime readiness | Qualification plus operator setup; one reproducible profile manifest and trusted readiness report | First prerequisite for live work; independent deterministic suite can continue | Reuse database readiness, auth composition, worker composition and runbooks. Add only the missing deploy composition/readiness seam if operator cannot compose it | Config parsing/readiness contracts deterministic; PostgreSQL, Temporal, auth grants and real services need live verification | Keep as one runtime-preflight slice; checkout `.env` absence is not proof that the service is absent |
| Q16-02 physical provider/LLM qualification | Provider qualification with NO-GO/GO report | Requires Q16-01 and canonical resolver/runtime; can overlap offline UI and lifecycle tests | Reuse #212 runner; no approved fallback exists. Code change only if the canonical-separation gate or another physical criterion reproducibly fails | Offline report contract deterministic; Cognee, Neo4j, PGVector, embeddings and LLM are real-only | Keep as physical-provider slice. Current result is NO-GO; do not assert GO from the nine passed observations |
| Q16-03 real ingestion lifecycle | User-path qualification for upload/update/delete/retry and all five formats | Requires Q16-01; can run alongside projection adapter setup, then feeds Q16-04 | Reuse canonical ingestion and local-profile tests; add integrated test only where the upload-to-accepted-revision path lacks coverage | Parser/contract tests deterministic; deployed storage, PostgreSQL and UI/API lifecycle real-only | Keep as a qualification slice, not a new parser implementation |
| Q16-04 projection/rebuild/rollback | Lifecycle qualification through canonical requests and physical publication | Depends on Q16-01/Q16-02 and accepted revisions from Q16-03 | Reuse #7/#90/#364 contracts; only fix a concrete gap. Cover failure injection, CAS, successor rollback and tombstone propagation | Deterministic state/failure suite exists; real physical artifacts and provider rollback are real-only | Keep as one independently evidenced lifecycle slice |
| Q16-05 governed Q&A/security | Application-flow qualification for trusted identity, authorized retrieval/disclosure, durable outcomes and run history | Depends on runtime, active projection and physical profile; can prepare deterministic checks in parallel | Reuse production AnswerQuestionCapability, AccessPolicy and disclosure gate. No alternate/fake authorization success allowed | Unit/ASGI/PG/Temporal contracts exist; real principal/policy and provider-backed answer are real-only | Keep as application qualification, with explicit workspace A/B and revocation scenarios |
| Q16-06 browser journey | Production browser acceptance for uploads, Chat, SSE, citations, feedback, runs and errors | Depends on Q16-01 and Q16-03–05 | Reuse React app, generated client and Playwright; do not count controlled API fixtures as release evidence | Component/controlled Playwright deterministic tests exist; browser against deployed services/provider is real-only | Keep as one end-to-end UI slice; UI defects found become separately scoped implementation follow-ups |
| Q16-07 backup/restore | Operator qualification task for this deployment | Can run in parallel with UI/benchmark once Q16-01 names real targets; backup targets and authorization are native prerequisites | Reuse #18/#362/#363/#368/#369 artifacts; do not rebuild backup mechanisms | Manifest and fake/temporary tests deterministic; coherent production-like source backup, isolated restore and reconciliation real-only | Keep as operational qualification evidence; no new backup code assumed |
| Q16-08 real benchmark/thresholds | Operator benchmark plus explicit Product Owner policy decision | Requires full trusted #453 preflight, approved dataset use, production Q&A path; threshold approval follows valid baseline | Reuse #443–#453, #501 and #502; no dataset or evaluator duplication | Synthetic evaluator/policy tests deterministic; private 149-case execution and metrics are local real-only | Keep as benchmark/approval slice. Do not run until preflight passes; denied quality remains N/A |
| Q16-09 final release gate | Release qualification and decision | Blocks on all mandatory slices above plus repository migrations/gates and compatibility disposition | Aggregate signed/identifiable evidence; documentation edits only for proven state | Gate/report semantics deterministic; final PASS requires all live evidence | Keep as final ticket/decision; no code gap assumed unless an aggregate gate is missing |

Recommended minimum tickets after `$to-tickets` are six independently
reviewable slices:

1. **Production runtime profile and trusted readiness** — operator/configuration
   task plus only necessary composition work. No blocker. Produces a redacted
   readiness report and a repeatable deployment profile.
2. **Canonical ingestion and physical projection lifecycle qualification** —
   qualification; blocked by 1. Covers real five-format upload through exact
   active snapshot, update/delete, failure, rebuild and rollback; includes a
   rerun of the missing #212 canonical-lifecycle gate. Keep separate provider
   architecture follow-up only if evidence remains NO-GO.
3. **Governed durable Q&A through deployed API and browser** — qualification;
   blocked by 1 and 2. Covers trusted scope, two-workspace isolation, protected
   citations, abstention/denial, cancellation/retry/reconnect, feedback and
   diagnostics.
4. **Deployment backup, isolated restore and operational recovery** — operator
   qualification; blocked by 1 and 2 because the exercise must recover the
   selected physical projection. May run in parallel with 3 once approved
   targets exist.
5. **Real Golden Dataset baseline and approved quality gate** — operator
   benchmark plus Product Owner decision; blocked by 1 and 2. Reuses
   #453/#501/#502; use the immutable dataset and only run locally after trusted
   preflight. It uses the durable production Q&A/capture path and does not
   require the Q16-03 browser journey, so it can run in parallel with 3 and 4.
6. **R0/R1 final release evidence and GO/NO-GO** — release qualification;
   blocked by 2–5 and required repository gates. Includes compatibility and
   documentation disposition.

The graph is `1 → 2`, `2 → 3`, `2 → 4`, `2 → 5`, and `3,4,5 → 6` (with 1
transitively required by 3–6). Q16-05's real benchmark has its own fresh #453
preflight; it does not depend on browser qualification. Offline regression,
migrations and deterministic tests may be run throughout.
Q16-02 provider choice becomes a separate architecture decision only if the
existing candidate still cannot satisfy #212 after the canonical production
composition is present. The #212 owned-PostgreSQL mapping fallback is a
recommendation, not accepted architecture. Any provider/profile or authority
change requires the applicable architecture review/ADR before implementation.

### Current GitHub and documentation state

- Issue #16 is open, labeled `wayfinder:task`, has no comments, and is a child
  of wayfinding Issue #1. Its current native blockers are #17, #18, #19, #22,
  #23 and #26; all issue states must not be used as evidence of qualification.
- Issues #469 and #478 are closed and their production capability / observer
  contracts exist in current `main`. The release blocker is real runtime
  composition and qualification, not reimplementing those contracts.
- Issues #501 and #502 are closed; the deterministic aggregates and threshold
  gate exist. The #453 report and checked-in decision package still record no
  real evaluation and no Product Owner-approved numerical values.
- Issue #17 is currently closed, but its issue body and published decision
  package still describe PR #500 as open even though GitHub records it merged
  at 2026-10-19 18:07 UTC. The package's substantive evidence remains current:
  thresholds are `decision_required`, no release approval exists, and the real
  benchmark is `not_run`. This is an inconsistent release state. Recommend
  reopening #17 or establishing an explicitly open approval gate that retains
  its native blocking relationship; do not make that tracker change here.
- `docs/ROADMAP.md` and `README.md` still describe #469/#478 production
  composition as outstanding. That is documentation drift against the closed
  issues and merged code. The final gate should reconcile these statements with
  the actual deploy composition and evidence; do not prematurely mark R1 done.

### Native dependency graph

Current Issue #16 parent/blocking metadata is recorded above. The issues already
completed for contracts and evidence include #3/#7/#15/#18/#19/#20/#25/#26/#27,
#443–#453, #469–#480, #501 and #502 (closed states vary by issue; validate
current state before claiming new work). The new tickets should depend on the
small runtime/physical/ingestion/user-path/operations/baseline slices described
above, not recreate the completed feature issues. Do not modify Issue #16's
relationships at this stage. The release gate must treat #17 as an outstanding
policy prerequisite despite its closed tracker state until a valid baseline and
explicit approval evidence exist.

## Qualification Scenarios

All scenarios report one of `PASS`, `FAIL`, `BLOCKED`, or `NOT_VERIFIED`.
`PASS` requires the named evidence artifact and exact tested boundary;
`FAIL` means an executed assertion violated its expected result; `BLOCKED`
means a trusted prerequisite or required decision was unavailable and the
scenario did not run; `NOT_VERIFIED` means evidence is absent, stale, incomplete
or cannot be bound to the target release. An absence of exceptions, log errors
or alerts never implies PASS. A blocked mandatory criterion blocks release.

Owners below are accountable functions, not newly created identities or grants.
Each qualification record carries the actual responsible principal where
authorized, the deployment/runtime identity, timestamps, software/config
digests, evidence references and safe reason codes.

### Mandatory release criteria

| ID / trace | Preconditions | Test scenario | Expected result | Evidence artifact | Owner | Result semantics |
|---|---|---|---|---|---|---|
| R1 — required migrations and automated gates / ROADMAP R0, #8/#16 | Clean disposable PostgreSQL 17 target; declared migration configuration; dev dependencies available | Apply full migration graph from empty database and supported upgrade path; run full pytest, compileall and domain-boundary gate | One valid migration head; all required commands exit successfully; no missing/skipped required test silently counted as pass | Migration revision inventory and command result manifest bound to commit/runtime | Release engineering | PASS only on full recorded results; database/test prerequisite absent = BLOCKED; any failure = FAIL |
| R2 — reproducible production-capable single-deployment profile / ROADMAP R1, Q16-01 | Approved profile and operator-provided safe configuration; no secrets in evidence | Start API, workers, PostgreSQL, Temporal, original storage and selected provider; query readiness and perform authenticated canary | Every required component is configured, reachable, correctly composed and reports scoped readiness; no fallback/legacy path selected silently | Redacted deployment manifest, component/readiness probe record and actual service versions/digests | Runtime/operator owner | Missing configuration/service = BLOCKED; mismatch/failure = FAIL; no linked live report = NOT_VERIFIED |
| R3 — trusted identity, workspace/environment and policy / INTERFACE access model, ADR 0011/0021 | Existing authorized principal and current policy; authorized test workspaces A/B | Sign in via configured trusted auth; exercise allowed route, denied route, cross-workspace source and service/human scope intersection | Scope is derived server-side; allowed action works; denied/cross-scope results reveal no source existence or protected text; no setup action manufactures rights | Safe authorization decision evidence and two-workspace integration record | Security/runtime owner | PASS requires real current bindings; missing principal/grant/policy verification = BLOCKED; leakage or bypass = FAIL |
| R4 — canonical lifecycle for PDF/DOCX/XLSX/MD/TXT / ROADMAP ingestion, #3/#7 | Runtime R2; source-use permission; actual upload authority | Upload representative real approved documents in all five formats via UI; repeat upload; change revision; tombstone; inject parser/storage partial failure and retry | Stable checksums/locators; immutable SourceRevision and originals; idempotent duplicate/retry; new head only after acceptance; delete is tombstone; partial outcome safely recoverable | Per-format source/revision metadata, checksums, bounded event IDs and UI/API run references, no file content | Ingestion owner | PASS only all five format paths and lifecycle edges; unavailable approved corpus/permission = BLOCKED; incorrect canonical state = FAIL |
| R5 — physical provider and structured provenance / ADR 0018/0026/0028, #212 | R2/R4; selected provider profile; approved LLM/embedding resources; safe disposable target | Run full real provider gate with structured chunk/entity/relation projection and retrieval; inspect physical provenance, active/shadow namespace, workspace routing, update/remove/tombstone and failure cleanup | All required provider observations pass; locators and lineage point to exact revisions; no shadow or foreign workspace data appears in normal retrieval; canonical state remains PostgreSQL-owned | New coherent #212-compatible physical qualification report with provider versions and bounded safe observations | Context Projection owner | Existing 9/10 #212 run is NO-GO, not PASS; any required not-run = BLOCKED/NOT_VERIFIED; invariant break = FAIL |
| R6 — projection activation, rebuild, failure and rollback / ROADMAP projection, #7/#15 | R4 accepted revisions; R5 provider ready; current generation known | Build and activate shadow snapshot; inject failure before validation and before activation; change source head; delete source; rebuild and rollback via successor | Failed candidate cannot change canonical state or active publication; CAS rejects stale generation; exact current revisions only become active; tombstones propagate; rollback creates valid successor; stale/shadow excluded | Canonical publication/snapshot IDs, generation transitions, physical validation evidence and recovery record | Projection owner | PASS needs live plus deterministic injection evidence; provider missing = BLOCKED; wrong pointer/lineage/access = FAIL |
| R7 — authorized grounded Q&A/citation correctness / INTERFACE Q&A, ADR 0033/0034/0040 | R2/R3/R5/R6; current active projection; authorized questions and approved source use | Ask an answerable question and inspect each citation; change/revoke source/policy and read history; query foreign workspace and inaccessible content | Only the existing production AnswerQuestionCapability and authorized Context Broker are used; accepted answer is grounded; each citation points to accessible current revision and stable locator; revoked/stale content is withheld | Correlated Q&A run, ContextBundle/projection pins, disclosure decision and citation locator-validation summary without excerpts | Q&A/security owner | PASS requires real current disclosure validation; no production path = BLOCKED; fabricated/stale/inaccessible disclosure = FAIL |
| R8 — abstention, clarification and safe terminal outcomes / ROADMAP Q&A exit criteria | Same as R7; known insufficient-evidence questions; injected safe failures | Ask unanswerable and partially supported questions; inject unavailable provider, worker failure and authorization change | Correct abstention/clarification/denied/failed/unavailable outcomes remain distinct; no candidate content/citation leaks for non-answer outcome | Terminal outcome records and metadata-only UI capture | Q&A owner | PASS per outcome contract and no leakage; missing test source/runtime = BLOCKED; collapsed outcome or leakage = FAIL |
| R9 — durable retry, cancellation, SSE reconnect and session restoration / INTERFACE streaming, ADR 0035/0038/0040 | R2; live Temporal worker; authenticated browser session | Submit once; reconnect after Last-Event-ID; reload session; cancel active run; replay same idempotency key after partial delivery | One logical run/effect; ordered/deduplicated event stream resumes; cancellation reaches durable terminal state; retries do not fork; identity/scope revalidated on protected emission and history | Run/admission/outbox/Temporal correlation plus browser interaction record | Workflow/API owner | PASS needs same-run identity and browser evidence; Temporal unavailable = BLOCKED; duplicate or disclosure race = FAIL |
| R10 — real administrator browser journey / INTERFACE Knowledge/Chat/Runs | R2–R9; supported browser and real deployed backend | Playwright/browser performs upload, status inspection, session creation, streamed answer, citation inspection, abstention, permission denial, feedback, reconnect, cancel and run diagnostics with no CLI | All critical UI states and expected user outcomes are visible and correspond to server results; no controlled/fake API boundary in release run | Browser report with UI/API/run IDs, deployment fingerprint and safe screenshots/artifacts with no private text | Product/UI owner | PASS only real-backend journey; controlled fixtures are deterministic evidence only; failed step = FAIL; deployment not available = BLOCKED |
| R11 — feedback and trace-based diagnosis / INTERFACE feedback/runs, ADR 0020/0037 | Completed live Q&A run from R10; diagnostic principal | Submit feedback; navigate run/trace from answer; inject ingestion, provider, retry and terminal errors; inspect UI and qualification output | Feedback binds to correct workspace/message/run; trace links API→outbox→Temporal→provider→terminal UI; errors explain state with bounded codes and no hidden chain-of-thought/private content | Feedback persistence receipt, trace lineage graph/IDs and content-free diagnostic report | Observability/product owner | PASS requires correlated actual run; missing lineage = NOT_VERIFIED; protected content in output = FAIL |
| R12 — private-data report safety / ADR 0020/#453/#501/#502 | Real qualification data permitted; report serializer configured | Scan generated reports, logs/artifacts and screenshots for document names, source/case IDs, questions, answers, citations/excerpts, credentials and raw provider payloads | Only approved metadata, aggregates, hashes, versions, safe error codes and evidence IDs are emitted; no protected content or identifying opaque IDs | Automated leak scan result plus reviewed metadata-only artifact digest | Security/privacy owner | Any leak = FAIL and artifact not publishable; scan unavailable = BLOCKED; no report = NOT_VERIFIED |
| R13 — backup and isolated restore / #18, #362/#363/#368/#369 | Approved PostgreSQL and original-storage backup/restore targets; recovery authorization | Take coordinated backup while writes are fenced; restore to isolated target; validate manifest and object/revision references; reconcile; rebuild/recover projection; execute canary query before cutover | Coherent recoverable point; source bytes/checksums/revisions match; restore is fenced until verification; projection can be rebuilt from canonical state; failure leaves original deployment unaffected | Immutable manifest, fence/recovery/reconciliation events, isolated target fingerprint, restored revision counts and safe canary result | Operations/storage owner | PASS needs actual isolated restore exercise; targets/auth absent = BLOCKED; coherence or reconciliation failure = FAIL |
| R14 — approved Golden Dataset baseline / #20/#443–#453/#501 | Exact dataset version/digest; every trusted #453 preflight prerequisite passes; source use locally authorized; R2–R7 path available | Locally prepare canonical corpus and verify readiness; execute all 149 cases through same production durable Q&A path; evaluate only frozen, correlated observations | Baseline includes 30 docs, 149 cases, 136 answered, 13 abstained, 0 denied; coverage, denominators, strata and failures are accurate; no second retrieval/generation; denial quality is `not_applicable`, eligible_count 0 | Local private run plus exportable metadata-only report, exact dataset digest and correlation manifest; no private corpus committed | Evaluation owner | Do not run without all preflight; missing trusted condition = BLOCKED; incomplete run = NOT_VERIFIED; synthetic/fake data cannot PASS |
| R15 — Product Owner approval and executable thresholds / #17/#501/#502 | Valid R14 baseline and complete aggregate denominators; explicit Product Owner review | Present real metric values/coverage and safe clusters; record Product Owner's separate explicit values/version/digest approval; run executable gate | Active approved versioned policy pins dataset/evaluator/runtime and thresholds; mandatory safety gates pass; all required metrics meet values and coverage; no number is inferred or invented | Product Owner approval record bound to policy digest and #502 gate report | Product Owner for R1 thresholds; release owner for execution | Without explicit approval = BLOCKED; any required threshold `decision_required` or failed = FAIL/BLOCKED; zero-denial remains N/A, never PASS |
| R16 — legacy compatibility or approved replacement / README compatibility | Supported legacy clients/fixtures or approved migration plan | Exercise documented `/ask`, `/ingest` and CLI compatibility behavior; if retiring, verify replacement route/CLI and migration coverage | No silent break; compatibility remains or retirement has an approved replacement and tested migration | Versioned compatibility report or replacement approval and contract evidence | API/product owner | Untested and no replacement = NOT_VERIFIED and blocks release; known regression = FAIL |
| R17 — final R0/R1 GO/NO-GO / ROADMAP all release criteria, Issue #16 | R1–R16 and mandatory repository gates; current issue/policy status reconciled | Evaluate all criterion records and dependencies against exact commit, deployment and evidence digests; independently reproduce aggregate gate | GO only if every mandatory criterion is PASS, every decision approved, no provider integration unverified, evidence current and compatible, and legacy disposition proven; otherwise NO-GO with blocker IDs | Signed/attributable metadata-only final qualification report, evidence index and reproducibility instructions | Release owner, with Product Owner policy approval | Any FAIL => NO-GO; BLOCKED/NOT_VERIFIED mandatory gate => NO-GO; report absence cannot be PASS |

### Traceability

The report must carry a stable map for every item: `ROADMAP/INTERFACE/ADR
criterion → scenario ID above → delivery/qualification ticket → evidence record
and digest → final decision row`. Qualification evidence cannot be inherited
from a closed issue unless that artifact exists, is in scope, and proves the
same current runtime boundary. Update roadmap checkboxes only when their exact
exit criteria have the required evidence.

## Testing Decisions

### Verification levels

| Level | Scope | Existing prior art | Required addition / absence |
|---|---|---|---|
| A — deterministic contracts | Domain/application unit tests, port contracts, synthetic R1 evaluator and versioned release gate; no production claim | Broad application/domain suites; `tests/evaluations/regression/test_r1_scenarios.py`; `tests/evals/test_regression_gate.py`; `tests/evals/test_release_threshold_gate.py`; `tests/qualification/` | Keep as baseline. Add tests only for a discovered contract gap. Include domain import-boundary test and safe report serialization. |
| B — integration | PostgreSQL migrations/tenancy/outbox, original storage, API/worker composition and Temporal lifecycle | `tests/integration/postgresql/`, `tests/infrastructure/db/`, persistence application tests, `tests/api/test_durable_ask_composition.py`, `tests/workers/`, Temporal tests; issue #430 evidence | Release deployment composition still needs a run against configured services. Testcontainers/disposable PostgreSQL and Temporal test server do not prove deployment configuration. |
| C — physical provider | Actual Cognee, Neo4j, PGVector, embeddings and LLM; physical indexing/retrieval, provenance, isolation, update/delete and recovery | `tests/memory/cognee/test_live_smoke.py`, `tests/memory/test_cognee_production_qualification.py`, `tests/integration/cognee/test_production_profile.py`; #212 coherent run | Rerun the full #212 gate after canonical resolver is wired. Present report says 9 pass, 1 `not_run`, decision NO-GO. No fake may produce this evidence. |
| D — end-to-end | Real browser, authenticated API, durable Q&A, projection, actual citations and diagnostics | Component tests and controlled Playwright in `web/e2e`; in-process ASGI API and #338/#422 records | Missing production browser/backend/provider journey. Add one composed Playwright scenario at the highest seam; do not duplicate unit behavior in E2E. Controlled fixtures remain Level A/D UI contract evidence only. |
| E — release qualification | Real approved dataset, approved thresholds, backup/restore, mandatory safety, legacy and final evidence | #453 qualification contract; #501 aggregate and #502 deterministic gate; #368 runbook evidence schema | No real benchmark result, numerical approval or production backup/restore report. Add/reuse only a release aggregator that consumes current criterion artifacts and fails closed. |

Good tests assert observable outcomes and domain records, not private call
ordering. Use fakes for default offline tests, but label their boundary
explicitly. New E2E scenarios must cross multiple existing seams and prove the
administrator path; they must not repeat component assertions. Real-provider
and private-data runs are opt-in, local/operator-controlled, run only after a
complete trusted preflight, and never become CI prerequisites. Inject time,
identifiers and failures in deterministic suites. For real runs, bind evidence
to exact immutable input/version digests and preserve bounded metadata only.

### Runtime prerequisite matrix

Status vocabulary is intentionally separate: `implemented` is a repository
contract; `configured` means a trusted operator binding exists; `reachable`
means a live health/action probe succeeded; `authorized` means current trusted
principal/scope/policy permits the action; `verified` means exact expected
identity, revision or membership was observed; `qualified` means all acceptance
evidence for the named release boundary passed. A repository contract can be
implemented while every runtime status remains unknown. No `.env` in one
checkout does not prove a service is absent. Readiness must not provision
principals, grants, policies, permissions or mappings.

| Prerequisite | Implemented | Configured | Reachable | Authorized | Verified | Qualified |
|---|---|---|---|---|---|---|
| PostgreSQL 17, schema/migrations and canonical persistence | Yes | Unknown for release profile | Unknown | Unknown operator identity | Current migration/revision state not yet evidenced in release profile | Disposable integration qualified; deployed profile not verified |
| Temporal namespace, task queue and workers | Yes | Unknown for release profile | Unknown | Unknown | Worker/task queue and workflow registration not bound to live run | Test-server integration qualified; deployment not qualified |
| Trusted local/OIDC authentication and runtime principal/scope | Yes | Unknown | Unknown | Unknown until current directory/policy read | `workspace_id`, environment, actor and grants need live binding | Contract/PostgreSQL boundaries only |
| AccessPolicy and revision access | Yes | Policy exists as contract; actual corpus policy binding unknown | Database check unknown | Unknown | Current policy generation and revocation path not observed | Not production-qualified |
| Production AnswerQuestionCapability and API/worker/observer composition | Yes; production capability/composition contracts exist | Turnkey deployment binding unknown | Unknown | Unknown | Same composition and same-execution capture not observed in deployment | Contract/controlled composition only |
| Authenticated capture channel | Yes | Runtime endpoint/transport unknown | Unknown | Requires trusted observer/worker identity | Same-run identity not verified live | Offline/contract only |
| Canonical source mapping and accepted/current revisions | Yes | Private mapping/use permission unknown | Canonical service unknown | Data owner authorization unknown | Exact current heads need readiness proof | No private benchmark qualification |
| Context Projection / physical Cognee profile | Adapter and qualification runner implemented | Candidate is identified, production configuration not attested | No release probe recorded | Workspace and provider scope unknown | #212 canonical separation `not_verified` | NO-GO |
| LLM and embedding configuration | Provider ports/config contracts implemented | Operator configuration unknown | Unknown | Key/data-use authorization unknown | Model/provider/version/dimension not bound to run | Not qualified |
| Original storage | Filesystem adapter and recovery contract implemented | Deployment root/target unknown | Unknown | Storage scope unknown | Object/checksum reconciliation not observed live | Local adapter contract qualified; deployment not qualified |
| Web/API deployment | API and React app implemented | Deployment binding unknown | Unknown | Trusted administrator access unknown | Browser/API/runtime release identity not correlated | Controlled UI qualified only |
| Approved private dataset | Version/digest/counts structurally pinned | Local runtime/data availability unknown | Not applicable until local operator checks | Use permission must be verified at runtime | Canonical mapping/readiness not established | Evaluation not run |
| Backup/restore targets | Contracts/runbooks implemented | Target and retention bindings unknown | Unknown | Operator authorization unknown | Coordinated backup/restore/reconciliation absent | Runbook schema exists; production exercise false |

## Mandatory safety invariants

- PostgreSQL remains the canonical store. No physical provider state establishes
  source, policy, activation or recovery authority.
- Cognee is replaceable and rebuildable; the physical provider never decides
  access. Never route around Context Broker or `AnswerDisclosureGate`.
- Trusted runtime context, not caller input, determines actor, workspace,
  environment and policy scope. Every tenant-owned command/read is scoped.
- Enforce access at retrieval and at each content disclosure, including citation
  inspector, stored answer/history, SSE replay and feedback/diagnostics views.
- SourceRevision and ProjectionSnapshot are immutable/versioned. Tombstones,
  stale revisions and shadow candidates remain excluded from ordinary Q&A.
- Production Q&A uses one `AnswerQuestionCapability` over PostgreSQL admission,
  transactional outbox and Temporal. Observer/benchmark reuses that exact run
  and cannot initiate a second retrieval or generation.
- A candidate answer is untrusted until current evidence, exact bundle,
  citations, semantic support, completeness/conflict and disclosure dependency
  checks succeed. Missing evidence means abstention/clarification, never an
  invented citation.
- Preserve safe outcome distinctions: answered, clarification-required,
  abstained, denied, failed, cancelled and unavailable. Non-answer terminal
  outcomes carry no protected candidate content.
- Keep privacy-safe diagnostics metadata only: no prompts, answers, excerpts,
  filenames, credentials, provider payloads or opaque private source/case IDs.
- Never weaken hard safety checks to meet quality thresholds. A zero eligible
  denied population is `not_applicable`, not a passing denial metric.
- Keep legacy `/ask`, `/ingest` and CLI compatibility until a replacement is
  tested and the retirement decision is approved.

## Production dependencies

Required profile components: PostgreSQL 17 plus approved migrations and roles;
canonical original storage; Temporal server/namespace and registered API,
outbox and Q&A workers; API and Web UI build; trusted authentication and
current authorization directory/policy; production AnswerQuestionCapability
composition shared by API/worker/observer; authenticated typed capture; a
Context Projection provider that has passed the complete #212 gate; configured
LLM/embedding provider and approved models; local approved Golden Dataset
access; trace/diagnostics read model; and authorized backup/isolated-restore
targets.

No complete deployment composition is established by this repository checkout:
the repo has profile pieces, test compositions, runbooks and one Neo4j image
Dockerfile, but no general deploy compose/orchestration manifest was found.
This is a finding about checked-in deployment artifacts, not a claim that no
external service is operating. Q16-01 must decide whether an already operator-
maintained deployment bundle is acceptable or a narrow repeatable local profile
must be added. Do not add broad cloud provisioning.

## Failure and recovery strategy

- If trusted runtime preflight fails, stop before dataset mapping, corpus
  readiness, Q&A or benchmark. Emit only a safe prerequisite reason.
- Ingestion retries are idempotent. Preserve immutable original/revision and
  ledger; partial failures remain visible and retryable. No false acceptance.
- Projection failure records safe validation/reconciliation evidence; it cannot
  modify current canonical state or active pointer. Rebuild into shadow and
  activate with expected generation/current revisions. Rollback publishes a
  valid successor; never mutate old snapshot.
- Provider outage, timeout or malformed evidence yields unavailable/failed or
  abstention according to existing contracts; never fallback to an unapproved
  provider or legacy `/ask` path.
- Ask cancellation/retry/reconnect keeps one durable run and terminal owner in
  Temporal. API wait timeout does not cancel the workflow; disclosure is
  revalidated before every later read.
- Backup runs use existing fence and coherent manifest. Restore is isolated;
  invalid manifests, missing originals, chain gaps or failed reconciliation
  leave the target untrusted and out of service. Rebuild Context Projection only
  from verified canonical state.
- Failed or partial benchmark produces no baseline and no threshold decision.
  Preserve only metadata-only failure categories and require a fresh readiness
  run after relevant runtime/policy/source/provider changes.
- Final gate is fail-closed. New code fixes are separately scoped and must not
  be hidden in a release-reporting task.

## Operator runbooks

Reuse and reconcile existing runbooks rather than duplicating their procedures:

- `docs/runbooks/issue-17-real-benchmark-preflight-checklist.md` and
  `docs/runbooks/r1-real-benchmark-qualification.md`: trusted preflight,
  production composition, canonical corpus and active projection readiness.
- `docs/runbooks/real-r1-dataset-local-validation.md`: local data preparation,
  structural validation and explicit separation of preparation, observation,
  evaluation and release qualification.
- `docs/runbooks/cognee-production-qualification.md`: isolated real Cognee
  provider profile and safe NO-GO report.
- `docs/runbooks/r1-backup-evidence.md` and
  `docs/runbooks/r1-isolated-restore.md`: coordinated backup, recovery evidence
  and isolated restore.
- `docs/runbooks/r1-projection-recovery.md`: canonical projection recovery,
  reconciliation and rebuild boundaries.
- `docs/runbooks/oidc-provider-qualification.md`: optional configured OIDC
  provider qualification; trusted local auth is acceptable only when that is
  the explicitly selected profile.

The release operator guide must state exact profile versions, safe setup order,
readiness commands, migration commands, user-access test, backup targets,
recovery fencing, benchmark invocation guard and report location. Secret values
are supplied out-of-band and never read into a report. A CLI step is allowed
only for operator/deployment functions, never as a substitute for upload, Q&A,
citation inspection, feedback or administrator diagnosis.

## Metadata-only qualification report contract

The final versioned report should contain:

- schema version; qualification ID; decision; start/end timestamps; target
  commit; release/profile identity; redacted component versions and config
  digests; operator identity reference where policy permits;
- stable criterion IDs, status, bounded reason codes, owner function, ticket
  reference, evidence artifact ID/digest, observation time, and dependency
  references;
- migration revision set, test command result summaries, component readiness
  states, workspace/environment scope references, Q&A run correlation IDs,
  projection/snapshot generations, provider/prompt/evaluator versions;
- per-dataset metric versions, aggregate numerator/denominator/eligible and
  unavailable counts, coverage, applicability, supported strata, safe failure
  clusters and threshold result; exact approved dataset version/digest;
- backup manifest digest, isolated restore verification and reconciliation
  outcome; legacy disposition; known limitations; explicit Product Owner
  approval identity/time/policy digest; final GO/NO-GO rationale and evidence
  index.

It must exclude credentials/endpoints when sensitive, document/source/case IDs,
filenames, questions, answers, excerpts/citations, raw traces, prompt/provider
payloads and any protected content. Report generation and CI fixtures must use
synthetic data; private aggregate reports remain local unless their explicit
privacy review authorizes another location.

## GO/NO-GO decision semantics

- **GO**: every mandatory criterion R1–R17 is PASS for one compatible current
  commit and deployment; real provider and browser integration are exercised;
  #212 is re-qualified GO; R14 baseline is complete; R15 active policy has
  explicit Product Owner approval and passes #502; backup/restore and legacy
  disposition are proven; safety/report scan passes; evidence is reproducible
  and attributable to the accountable release owner.
- **NO-GO**: any mandatory FAIL, any BLOCKED or NOT_VERIFIED mandatory gate,
  missing Product Owner policy approval, #212 NO-GO/not-run, real benchmark not
  run, missing restore evidence, incomplete browser path, or untested legacy
  retirement. State each blocker and owner; do not infer partial readiness.
- A scope marked `not_applicable` is allowed only when its contract permits it
  and denominator is zero with that fact shown. It does not count as a positive
  safety pass. Dataset denial quality specifically remains `not_applicable` for
  0 eligible cases.
- An old evidence record is not current after changes to code, deployment
  configuration, provider/model, policy generation, source revisions, active
  projection or dataset. Re-run affected criteria and the final aggregator.

## Risks and unresolved decisions

1. **Cognee production approval remains NO-GO.** #212's physical profile
   exercised real backends and passed nine physical gates, but
   `canonical_lifecycle_separation` was `not_run`; `production_approval=false`.
   The suggested PostgreSQL-owned physical mapping is not accepted. First wire
   and observe the canonical projection resolver in the production profile and
   rerun the gate. If that fails, stop for architecture/provider decision; do
   not silently switch backend or weaken lineage/isolation.
2. **Issue #17 release-state mismatch.** GitHub currently shows #17 closed; the
   #501 and #502 implementations are complete, and #502 is an executable
   deterministic gate. The checked-in package and #453 report still have no
   real metrics, no active numerical policy and `evaluation: not_run`. Closure
   is not the required Product Owner approval. Reopen/replace the release gate
   explicitly before a GO; this spec does not alter it.
3. **Runtime readiness is unknown, not absent.** The local qualification
   checklist says required DB/auth/Temporal settings and production factory
   were unavailable in the inspected environment; it does not prove the
   operator deployment is unavailable. Obtain the trusted readiness report
   without exposing credential values or creating access.
4. **Deployment composition remains unclear.** Runtime capability and observer
   contracts exist and #469/#478 are closed, while the issue-17 checklist says
   there is no local production qualification factory and repo has no general
   deployment manifest. Verify the current operator composition at the highest
   supported seam; only then identify real binding/code gaps.
5. **Roadmap and README status drift.** They still call #469 composition a
   blocker. Reconcile after observing current deployed composition and test
   evidence; do not update R1 checkboxes solely because issues closed.
6. **Dataset coverage has zero denied cases** and abstention examples only
   cover XLSX among the noted strata. Preserve this limitation and N/A denial
   metric; do not add fake denied cases to private data.
7. **External model/provider behavior is variable.** Pin model, embedding
   dimensions, Cognee and backend versions in every report and fail readiness
   when pins differ from the approved profile.
8. **Privacy of operational evidence.** Run and benchmark outputs can contain
   sensitive material if raw payload logging is enabled. Validate emitted
   report/log surfaces with seeded synthetic sentinel strings before private
   data use.

## Handoff instructions for `$to-tickets`

Use the six-slice minimum under “Qualification directions and proposed ticket
decomposition.” Publish no ticket until the user separately asks to proceed.
Keep tickets qualification-first and refer to completed contracts rather than
reimplementing them. Native blocking edges should follow the proposed graph;
operator setup and Product Owner threshold approval must be explicit blockers to
the evidence they enable. Do not mark a ticket complete based only on issue
state, deterministic tests, fake-backed integration, or a document. A separate
architecture decision is required only if the rerun of #212 leaves the selected
provider/profile in NO-GO or requires changing an accepted invariant.

The `$to-tickets` handoff is ready once this local specification is reviewed;
the next skill must still show the proposed breakdown and wait for approval
before publishing any new GitHub issues.
