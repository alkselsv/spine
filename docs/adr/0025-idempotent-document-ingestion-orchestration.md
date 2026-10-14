# Idempotent document-ingestion orchestration

For the first durable document-ingestion slice, the application layer coordinates the existing storage, source-observation, admission, canonical-acceptance, parser, artifact, ledger and outbox ports; it does not introduce a second source of truth. Each phase is independently repeatable, immutable artifacts are persisted by their existing repository, and the final processing handoff is a content-free transactional-outbox event so a worker can receive the request at least once without exposing document bytes.

The orchestration seam requires a trusted-context factory and a canonical-acceptance command factory. This keeps tenant authorization and policy/current-head decisions in the approved composition boundary, while making TXT/Markdown support explicit and leaving PDF/DOCX/XLSX unavailable until their parser adapters are delivered.
