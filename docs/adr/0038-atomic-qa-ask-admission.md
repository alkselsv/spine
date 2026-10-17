---
status: accepted
---

# Atomic Q&A Ask admission

Issue #426 adopts a PostgreSQL-canonical admission seam for the approved
`spine.qa.ask-admission-cancellation:v1` contract. The idempotency receipt is
the command identity and is unique by workspace, environment, authenticated
actor/subject, operation/schema and key; the same transaction creates the
server-owned run, linked pending message and content-minimized dispatch intent.
The admitted run starts in the explicit `admitted` pre-dispatch state; later
dispatch may advance it through `dispatch_pending` to `running`. The outbox
remains delivery-only, while execution, cancellation and terminal arbitration
stay with the later workflow owner. The in-memory adapter is only
an offline contract fake and must not become a second authority.
