# Issue #646 — offline conversational requirements qualification

The deterministic offline functional path and overall qualification result are
**NOT_VERIFIED**. Two independent gates remain open: the browser test currently
uses intercepted API responses instead of the required existing
application/PostgreSQL/Agent Runtime fake/Temporal composition, and a
Product/Project Owner has not approved a threshold for unsupported requirements.
No numeric threshold is inferred, and WER/CER is not used as a substitute.

The browser fixture starts from the conversational composer, retries a
lost start response with the same command identity, restores the pinned run and
its canonical events, inspects evidence for an exact SourceRevision, answers a
clarification, edits and returns the artifact for rework, saves human review,
accepts artifact version 5 with its exact digest, and reconstructs the terminal
feed after reload. This proves browser behavior only: its stateful intercepted
responses are not canonical persistence and must not be treated as a second
business runtime or as the required end-to-end acceptance seam. PostgreSQL,
outbox and Temporal recovery claims point to their existing real public contract
tests in the adjacent JSON report, but those separate tests do not close the
browser-composition gate.

Default evidence is synthetic and makes no Google, pricing, connector, customer
data, paid model or production-provider claim. A future live run remains
`NOT_VERIFIED` until an approved isolated PostgreSQL target is migrated, a
reachable Temporal service has the requirements worker registered, provider and
model credentials/configuration are approved, and authorized live SourceRevisions
plus disclosure policy are available.

The executable evidence index is
[`issue-646-offline-requirements.json`](issue-646-offline-requirements.json).
