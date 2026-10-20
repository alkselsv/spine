# Issue #17: local real benchmark preflight checklist

This checklist is for one explicitly authorized local run of dataset
`0.3.9-user-approved` after all mandatory checks pass. It does not provision
runtime services, alter authorization, create identities/grants/mappings, or
run the benchmark. Keep detailed private evidence in approved local storage.

## Current known state

| Item | State | Evidence / limit |
| --- | --- | --- |
| Approved dataset | Structurally validated | Version `0.3.9-user-approved`, SHA-256 `7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62`, 30 documents, 149 cases (136 answered, 13 abstained, 0 denied); evaluation `not_run`. |
| Local production qualification factory | Composition seam available | `DurableAskComposition.create_real_benchmark_qualification_runtime()` issues a CLI runtime only when the API/Temporal/observer graph passes the exact production binding checks and the runner carries the same evidence. The runtime owner still supplies deployment configuration and the real prerequisite inspector. |
| Runtime configuration | Missing in current process | `SPINE_DATABASE_URL`, local auth configuration and Temporal address/namespace are absent; `.env` and `.env.local` are absent. Secret values were not read. |
| PostgreSQL / Temporal / workers health | Not verified | No trusted composed preflight was available; absence of configuration does not establish service process state. |
| Principal, scope, current authorization and AccessPolicy | Not verified | Cannot resolve through canonical PostgreSQL authorization without trusted configured composition. No policy change attempted. |
| Corpus permission, mapping and canonical accepted/current revisions | Not verified | Dataset validation is not corpus owner permission or canonical mapping/readiness evidence. |
| Active healthy projection and exact revision membership | Not verified | Requires the trusted scope and canonical projection readiness checks. |
| Production Q&A composition and same-execution durable capture | Not verified | ADR-0040 composition must be supplied by runtime owner; no fake or test observer can satisfy preflight. |
| Real benchmark report | Absent | Qualification record says `real_opt_in_qualification.status=not_exercised`; no observation started. |

## Why a local preflight may still be unavailable

`preflight_unavailable` means the operator could not obtain a trusted
content-free readiness report. The composition method supplies the verified
API/Temporal/observer binding and ties the preflight to the same evidence as the
integrated runner. The runtime owner must still provide the typed deployment
profile, the real inspector for all 11 prerequisites, and configured service
clients. Missing local environment configuration is not evidence that a remote
service is absent. If the inspector throws or returns an invalid report, the
CLI stops before mapping and observation with `preflight_unavailable`.

The versioned `spine-production-runtime-profile:v1` record contains PostgreSQL
major and migration revision; Temporal namespace and task queue; worker roles;
authentication and original-storage profiles; Context Projection provider and
backend versions; LLM and embedding model versions plus embedding dimensions;
Web and API release identities; commit SHA; and deployment fingerprint. It has
no secret, endpoint, corpus, or credential fields. The CLI includes a
`spine-production-runtime-readiness:v1` report under
`runtime_readiness` when a typed trusted report is available. It keeps
configuration, reachability, authorization, verified bindings, and overall
qualification as separate states, with bounded reason codes and safe evidence
references. The live inspector returns the observed profile alongside its
prerequisite checks; any mismatch with the selected profile blocks
configuration, binding and qualification before benchmark mapping. A profile
does not create identity, grants, policy or mappings.

This establishes missing local composition/configuration, not that PostgreSQL,
Temporal, workers, the canonical corpus, or projection are stopped or absent.
Their live state, canonical authorization, owner permission, mapping,
projection membership, and capture health remain unverified until a runtime
owner supplies the trusted composition and it checks them.

## Operator checklist for a later attempt

Each check must be emitted by the accepted trusted preflight with a safe state
and bounded reason code. All 11 prerequisites must be `ready` for the exact
trusted workspace/environment. A missing, denied, unavailable, or absent check
stops execution before mapping, readiness observation, or Q&A.

- [ ] **PostgreSQL:** runtime owner confirms configured canonical DB connectivity
  and required application roles without printing DSN or credentials.
- [ ] **Temporal:** workflow owner confirms configured namespace, task queue,
  worker health, and terminal waiter availability; admission acknowledgement
  is not terminal completion.
- [ ] **Trusted authentication:** identity owner confirms configured principal
  resolves to an existing canonical identity; no benchmark identity is created.
- [ ] **Authorization scope:** security owner validates current membership,
  environment, role, route grant, generation, and AccessPolicy through trusted
  request context; selectors are not authority.
- [ ] **Production composition:** runtime owner supplies the same durable API /
  Temporal `AnswerQuestionCapability` composition used by the product and
  observer, with canonical terminal readback.
- [ ] **Capture channel:** authenticated, same-execution typed capture is
  available and bound to canonical execution identity; no fake observer or
  second retrieval.
- [ ] **Canonical corpus permission:** data owner has approved the selected
  corpus/revisions for this environment; do not alter permissions to pass.
- [ ] **Canonical corpus:** owner-approved mapping binds dataset source/revision
  aliases to canonical SourceRevisions; ingestion acceptance and currentness
  are confirmed through canonical lifecycle contracts.
- [ ] **Active projection:** exact required revisions are members of the active
  healthy/current publication for the trusted scope.
- [ ] **Approved dataset:** loader verifies the pinned version and digest and
  30/149/136/13/0 counts; no synthetic fallback.
- [ ] **Local inputs:** approved source root and versioned mapping manifest are
  present in permitted local storage and digests match the reviewed records.

Only after the complete preflight returns `ready` may the operator explicitly
run the existing local real benchmark once. Store its metadata-only report in
approved local private storage. Any partial, failed, denied, or unavailable
result is not a baseline and requires a complete new readiness check before a
later explicit run. Do not attach private report contents or mappings to GitHub.
