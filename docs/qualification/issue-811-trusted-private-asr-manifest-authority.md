# Issue #811: trusted private ASR manifest authority

## Acceptance matrix (synthetic PostgreSQL verification)

Focused run of the #811 tests in
`tests/integration/postgresql/test_postgresql_private_asr_storage.py`:
**29 passed, exit code 0**. The batch includes the trusted resolver positive
path, both withdrawal/report commit race orders, negative publication cases,
and runtime RLS/grant/immutability checks.

| Criterion | Result | Evidence |
| --- | --- | --- |
| Forged digest, wrong version/dataset, corrupt stored payload digest | VERIFIED | Negative tests deny report publication |
| Missing/duplicate membership; altered source, revision, revision digest, split or tenant scope | VERIFIED | Negative tests use PostgreSQL resolver; no report or success audit is committed |
| Wrong workspace, environment or purpose | VERIFIED | Restricted-runtime resolver tests deny |
| Missing, withdrawn, superseded or digest-mismatched current release | VERIFIED | PostgreSQL release-pointer negative cases deny publication |
| Generic denial and no unauthorized report/audit visibility | VERIFIED | Publication denial tests assert generic message and zero report/audit rows |
| Runtime `NOBYPASSRLS`, FORCE RLS, SELECT-only, no governance membership | VERIFIED | Catalog/grant integration test |
| Manifest UPDATE/DELETE immutability | VERIFIED | PostgreSQL trigger rejects both operations |
| Exact current release positive publication | VERIFIED | Existing trusted-manifest positive-path test |
| Withdrawal before report commit; report commit before withdrawal | VERIFIED | Both real PostgreSQL transaction race tests |
| Caller-pinned expected generation comparison | NOT APPLICABLE | #771/#772 request contract does not carry an expected generation; currentness is resolved from the trusted pointer |
| External approval/revocation fencing and production qualification | OUT OF SCOPE / NOT VERIFIED | External authority belongs to #813; no real release is approved |

The 29-test batch completed on the PR branch before publishing this test and
documentation update. It used only synthetic fixtures. This is not evidence of
production qualification or a real-data release.

The canonical contract owner is `application.private_asr_corpus`, specifically
the existing `PrivateASRManifestLineageVerifier` port. The PostgreSQL
implementation lives in `infrastructure.private_asr.manifest_authority` and is
injected into the existing #795 report store. This keeps corpus identity and
lineage at the existing #771/#772 boundary, uses #795's transaction, and adds
neither a dataset registry nor another runner.

Revision `20270108_107` stores an immutable JSONB snapshot and a current release
pointer keyed by workspace, environment, purpose, and dataset. The digest is
recomputed from canonical JSON at resolution; record membership is then checked
against source object, revision, revision digest, split, and scope. Reports must
name the exact manifest version and digest. The release row's generation and
manifest binding are protected by a transaction-scoped advisory fence until
report commit. The resolver takes a shared lock; database triggers on release
pointer update/delete take the matching exclusive lock. Thus every release
writer serializes with report publication without granting runtime UPDATE.
Report publication takes its existing source locks before this fence, while
release writers take no source locks. A withdrawn pointer has no manifest
binding. Missing rows deny. Runtime has SELECT only and both tables force tenant
RLS. No release rows are installed by this migration.

The existing #771 contract has one dataset `version`; this resolver treats that
as the immutable manifest version. A future contract that separates dataset
version and manifest version needs a versioned compatibility refinement before
it can be enforced independently.

Release writes are deliberately outside the application runtime role. This
change provides the persisted authority and resolver, not approval of a real
corpus or a user-facing governance workflow. External approval/revocation
fencing remains #813; production qualification and real-data release remain
NOT_VERIFIED. The #795 synthetic verifier remains test-only and is not a
fallback from this resolver.
