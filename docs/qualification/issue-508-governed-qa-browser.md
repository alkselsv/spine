# Issue #508 governed Q&A browser qualification

Status: **PASS**. Qualification run: `issue508-local-034`.

The repository now has a separate opt-in Chromium journey at
`web/e2e/production/governed-qa.spec.ts`. It uses the deployed Web origin and
the real `/api/v1` surface, installs no Playwright routes, starts no controlled
preview server and records no screenshot, video, trace, document, question,
answer or citation excerpt. The metadata-only result is written to
`issue-508-governed-qa-browser-report.json`.

The journey covers upload and active projection observation, one answer with a
server-protected citation tied to the uploaded source revision, feedback and
Run diagnostics, a post-dispatch browser disconnect followed by admission
retry with the same idempotency key and one canonical run, persisted SSE replay
with `Last-Event-ID`, session reload, abstention, cancellation and direct
cross-workspace reads that must be denied without disclosing the first
workspace's question, answer or source.

Two integration defects were required to make the composed path executable:
the deployed SPA had no typed runtime injection point for its existing trusted
identity/scope adapter, and the final Chat screen polled canonical messages
without observing the already implemented persisted event endpoint. The added
browser-runtime seam keeps server authorization authoritative; the event
observer is bounded, resumes by cursor and fails closed on malformed protocol
data.

The local `.env` contains the approved corpus path, questions, release metadata
and two distinct workspace/environment scopes. A text-only, anonymized input is
under ignored `data/incoming/issue508/`; it is not a repository artifact. The
repository-owned Compose deployment now applies migrations, explicitly
bootstraps the disposable local authority graph, and runs the protected
PostgreSQL-backed document query surface.

The missing production ingestion surface has been implemented. The API now
composes durable original-upload storage, validation and parsing, parsed
artifact persistence, canonical acceptance, initial access-policy activation
and transactional outbox publication. The deployable outbox worker consumes
canonical, processing, policy-acknowledgement and projection-request events and
invokes the real Cognee projection adapter against the same PostgreSQL and
Neo4j deployment. A real HTTP upload against Compose reached this
provider-backed projection boundary and retained its correlated ingestion,
source, revision and outbox identities.

The provider/canonical handoff conflict found during the first Compose exercise
was caused by the worker issuing `projection_result` as its trusted persistence
operation while `ProjectionReceiptCommand` requires `projection_receipt`. The
composition now shares the command-owned operation constant.

The final Chromium run used a fresh disposable Compose database and the
least-privilege production composition. It completed upload, active projection,
answer/citation/feedback/run diagnosis, duplicate retry, SSE reconnect, session
restoration, abstention, cancellation, two-workspace isolation, evidence
disclosure rejection and safe UI-state checks. The metadata-only JSON record
binds the exact API, Web and provider release identities used by that run.

The report schema also requires `evidence_disclosure_rejection` and
`safe_ui_states`. The live journey rejects a fabricated citation deep link,
performs an inaccessible cross-workspace read, tombstones the uploaded test
source and rechecks the now stale/revoked citation. It also records only the
named loading, empty, partial, stale, degraded reconnect, permission-denied and
terminal-failure states that were visibly exercised in that browser session.

The generated PASS record contains all required scenario IDs, two workspace
references, correlated ingestion/session/run/feedback/source identities, a
validated stable locator and exact release fingerprints. It records no content
fields, screenshot, video or Playwright trace and used neither API mocking,
route interception nor a controlled fixture. The duplicate/retry scenario
discarded one successful admission response in the browser to create an
ambiguous disconnect, then verified the deployed API returned the durable
replay for the same idempotency key and run.
