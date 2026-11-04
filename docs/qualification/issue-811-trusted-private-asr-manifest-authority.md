# Issue #811: trusted private ASR manifest authority

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
