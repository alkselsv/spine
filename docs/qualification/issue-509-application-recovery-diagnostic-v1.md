# Issue #509 recovered application diagnostic v1

Status: **root cause verified; application recovery qualification remains
NOT_VERIFIED**.

The third recovered-application attempt was diagnosed offline on a disposable
clone of its stopped PostgreSQL target. The source target remained stopped and
unchanged. The diagnostic used the recorded outbox request and existing physical
metadata, made no LLM or embedding request and retained no source content.

The failure control reproduced the public
`projection.request.integrity_conflict` at the canonical mapping stage. Its
qualification composition issued `projection_result` as the trusted persistence
operation while the receipt command is bound to `projection_receipt`. The
idempotency boundary therefore rejected the command before committing a
canonical receipt.

A single-variable causal control changed only the trusted operation to
`projection_receipt`. Mapping then completed and the diagnostic clone contained
one receipt claim, one projection receipt and three source references. This
confirms a qualification-composition error rather than archive corruption,
partition drift or a Cognee physical-projection failure.

The production worker already uses the command-owned receipt operation through
`_projection_receipt_context`, with a regression test that asserts the exact
operation. No production architecture change is required. The Issue #509
runbook now requires that helper for recovered-application composition and
forbids the stale generic operation.

This diagnostic is not a provider-backed retry and does not qualify publication,
serving or the application canary. A fresh recovered target, separately approved
provider run, authorized application read and independent final evidence review
remain required.
