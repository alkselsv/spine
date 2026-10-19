# Issue #17: local real benchmark preflight checklist

This checklist is for one explicitly authorized local run of dataset
`0.3.9-user-approved` after all mandatory checks pass. It does not provision
runtime services, alter authorization, create identities/grants/mappings, or
run the benchmark. Keep detailed private evidence in approved local storage.

## Current known state

| Item | State | Evidence / limit |
| --- | --- | --- |
| Approved dataset | Structurally validated | Version `0.3.9-user-approved`, SHA-256 `7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62`, 30 documents, 149 cases (136 answered, 13 abstained, 0 denied); evaluation `not_run`. |
| Local production qualification factory | Missing in this checkout | Search found the CLI factory seam and trusted-preflight issuer, but no local production factory that composes them. |
| Runtime configuration | Missing in current process | `SPINE_DATABASE_URL`, local auth configuration and Temporal address/namespace are absent; `.env` and `.env.local` are absent. Secret values were not read. |
| PostgreSQL / Temporal / workers health | Not verified | No trusted composed preflight was available; absence of configuration does not establish service process state. |
| Principal, scope, current authorization and AccessPolicy | Not verified | Cannot resolve through canonical PostgreSQL authorization without trusted configured composition. No policy change attempted. |
| Corpus permission, mapping and canonical accepted/current revisions | Not verified | Dataset validation is not corpus owner permission or canonical mapping/readiness evidence. |
| Active healthy projection and exact revision membership | Not verified | Requires the trusted scope and canonical projection readiness checks. |
| Production Q&A composition and same-execution durable capture | Not verified | ADR-0040 composition must be supplied by runtime owner; no fake or test observer can satisfy preflight. |
| Real benchmark report | Absent | Qualification record says `real_opt_in_qualification.status=not_exercised`; no observation started. |

## Why the current preflight is unavailable

The `preflight_unavailable` result means the operator cannot obtain a trusted
content-free readiness report from the local production composition. In the
current checkout, the CLI requires an explicit `--composition` factory, while
the qualification contract permits real execution only through a
`TrustedRealBenchmarkPreflight` tied to the same verified production composition
as the integrated runner. No such local production factory was found, and the
current process has no database/local-auth/Temporal configuration. The CLI
catches a preflight exception or invalid report and stops before the runner;
the qualification runbook's safe reason is `preflight_unavailable`.

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
