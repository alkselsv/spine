# Idempotent document-ingestion orchestration

For the first durable document-ingestion slice, the application layer coordinates the existing storage, source-observation, admission, canonical-acceptance, parser, artifact, ledger and outbox ports; it does not introduce a second source of truth. Each phase is independently repeatable, immutable artifacts are persisted by their existing repository, and the final processing handoff is a content-free transactional-outbox event so a worker can receive the request at least once without exposing document bytes.

The orchestration seam requires a trusted-context factory and a canonical-acceptance command factory. This keeps tenant authorization and policy/current-head decisions in the approved composition boundary, while making TXT/Markdown support explicit and leaving PDF/DOCX/XLSX unavailable until their parser adapters are delivered.

After a successful parsed artifact and `ProcessingReceipt` are staged, the same
Unit of Work now records both the `processed` → `projection_requested` ledger
transition and an immutable `ingestion.projection.requested` outbox intent. The
intent carries only tenant scope in its envelope, opaque run/item/revision and
receipt identities, pinned parser/configuration/locator identities, a stable
producer identity, and trace/correlation/causation metadata. Its event identity
is deterministic for one item and original digest, while its producer identity
is stable for the item so a changed digest conflicts instead of silently
replacing work.

This handoff does not create a projection run, select an ontology or projection
configuration, invoke Cognee, or claim projection processing/readiness. Those
remain downstream lifecycle decisions; `projection_requested` records only the
durable request fact.
